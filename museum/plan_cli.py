"""``loan-plan`` command line: file I/O around :mod:`museum.plan`.

Reads the audit source and the plan request, writes the complete schedule and
the carrier change list.  Exit code 0 on success; exit code 2 with a
``loan-plan:`` stderr prefix on any input, planning, or output failure —
failures never create output that could be mistaken for a complete schedule
and never damage existing files.
"""

import argparse
import json
import os
from pathlib import Path
import sys

from . import plan


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f"loan-plan: {message}\n")


def _load_json(path, label):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise plan.PlanError(f"cannot read {label} file: {exc}") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise plan.PlanError(f"{label} file is not valid JSON: {exc}") from exc


def _write_create_only(path, text):
    """Write ``text`` to a new file only; never overwrite, never leave halves."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise plan.PlanError(f"cannot prepare output directory: {exc}") from exc
    try:
        fh = target.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise plan.PlanError(f"output already exists: {target}") from exc
    except OSError as exc:
        raise plan.PlanError(f"cannot create output: {exc}") from exc
    try:
        with fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException as exc:
        try:
            target.unlink()
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise plan.PlanError(f"cannot write output: {exc}") from exc
        raise


def main(argv=None):
    parser = _Parser(
        prog="loan-plan",
        description="Plan pre-departure round trips for loan batches and "
                    "emit the carrier booking changes.")
    parser.add_argument("--source", required=True,
                        help="existing audit input JSON (its loans are used)")
    parser.add_argument("--request", required=True,
                        help="plan input JSON (hubs, runs, batches, "
                             "previous_bookings)")
    parser.add_argument("--output", required=True,
                        help="result JSON path (create-only)")
    args = parser.parse_args(argv)
    try:
        source = _load_json(args.source, "source")
        request = _load_json(args.request, "request")
        result = plan.plan_round(source, request)
        _write_create_only(args.output,
                           json.dumps(result, indent=2, sort_keys=True) + "\n")
    except (plan.PlanError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"loan-plan: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"bookings": len(result["bookings"]),
                      "changes": len(result["changes"]),
                      "waitlisted": len(result["waitlisted"])},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
