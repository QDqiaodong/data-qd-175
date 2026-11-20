"""``loan-heat`` command line: file I/O around :mod:`museum.heat`.

Reads the existing audit input (for loan/object identity), one complete
transport manifest and a probe-sample NDJSON export (a file or ``-`` for
standard input, read exactly once).  Writes the conservator decisions and the
device-team probe health report.  The command never contacts the network and
never modifies its inputs.  Exit code 0 on success; exit code 2 with a
``loan-heat:`` stderr prefix on any input, validation, path, or output
failure.  Failures never leave a partial result and never overwrite an
existing file.
"""

import argparse
import json
import os
from pathlib import Path
import sys

from . import heat


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f"loan-heat: {message}\n")


def _load_json(path, label):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise heat.HeatError(f"cannot read {label} file: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise heat.HeatError(f"{label} file is not valid UTF-8: {exc}") \
            from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise heat.HeatError(
            f"{label} file is not valid JSON: {exc}") from exc


def _open_samples(path):
    """Return (binary stream, label) for ``path``; ``-`` reads stdin once."""
    if path == "-":
        return sys.stdin.buffer, "samples stdin"
    try:
        stream = open(path, "rb")
    except OSError as exc:
        raise heat.HeatError(f"cannot read samples file: {exc}") from exc
    return stream, "samples file"


def _write_create_only(path, text):
    """Write ``text`` to a new file only; never overwrite, never leave halves."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise heat.HeatError(
            f"cannot prepare output directory: {exc}") from exc
    try:
        handle = target.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise heat.HeatError(f"output already exists: {target}") from exc
    except OSError as exc:
        raise heat.HeatError(f"cannot create output: {exc}") from exc
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException as exc:
        try:
            target.unlink()
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise heat.HeatError(f"cannot write output: {exc}") from exc
        raise


def _prepare_scratch(path):
    if path is None:
        return None
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise heat.HeatError(f"cannot use scratch directory: {exc}") from exc
    if not os.path.isdir(path):
        raise heat.HeatError(f"scratch path is not a directory: {path}")
    if not os.access(path, os.W_OK | os.X_OK):
        raise heat.HeatError(f"scratch directory is not writable: {path}")
    return path


def run(args):
    loans = heat.validate_source(_load_json(args.source, "source"))
    batch = heat.validate_manifest(_load_json(args.manifest, "manifest"),
                                   loans)
    stream, label = _open_samples(args.samples)
    try:
        scratch = _prepare_scratch(args.scratch)
        rows = heat._merged_samples(stream, batch["probe_index"], label,
                                    scratch)
        heat.attach_samples(batch, rows)
    finally:
        if stream is not sys.stdin.buffer:
            stream.close()
    result = heat.evaluate(batch)
    _write_create_only(args.output,
                       json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def main(argv=None):
    parser = _Parser(
        prog="loan-heat",
        description="Decide loan release/review/quarantine from transport "
                    "temperature evidence and report probe health. Local "
                    "only: no network, inputs are never modified.")
    parser.add_argument("--source", required=True,
                        help="existing audit input JSON (loan/object ids)")
    parser.add_argument("--manifest", required=True,
                        help="complete transport manifest JSON")
    parser.add_argument("--samples", required=True,
                        help="probe sample NDJSON (a file, or - for stdin, "
                             "read once)")
    parser.add_argument("--scratch", default=None,
                        help="directory for this run's temporary files "
                             "(default: system temp)")
    parser.add_argument("--output", required=True,
                        help="result JSON path (create-only)")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (heat.HeatError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"loan-heat: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(
        {"loans": len(result["loans"]),
         "isolate": sum(1 for row in result["loans"]
                        if row["decision"] == "isolate"),
         "review": sum(1 for row in result["loans"]
                       if row["decision"] == "review"),
         "release": sum(1 for row in result["loans"]
                        if row["decision"] == "release"),
         "probes": len(result["probe_health"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
