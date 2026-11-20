"""``loan-deposit`` command line: file I/O around :mod:`museum.deposit`.

Reads the loans document and one settlement batch, writes the refund
reconciliation and the safe new refund suggestions.  The command never
contacts the network and never sends or revokes a refund.  Exit code 0 on
success; exit code 2 with a ``loan-deposit:`` stderr prefix on any input,
validation, or output failure — failures never create output that could be
mistaken for a complete result and never damage existing files.
"""

import argparse
import json
import os
from pathlib import Path
import sys

from . import deposit


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f"loan-deposit: {message}\n")


def _load_json(path, label):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise deposit.DepositError(
            f"cannot read {label} file: {exc}") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise deposit.DepositError(
            f"{label} file is not valid JSON: {exc}") from exc


def _write_create_only(path, text):
    """Write ``text`` to a new file only; never overwrite, never leave halves."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise deposit.DepositError(
            f"cannot prepare output directory: {exc}") from exc
    try:
        fh = target.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise deposit.DepositError(
            f"output already exists: {target}") from exc
    except OSError as exc:
        raise deposit.DepositError(f"cannot create output: {exc}") from exc
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
            raise deposit.DepositError(
                f"cannot write output: {exc}") from exc
        raise


def main(argv=None):
    parser = _Parser(
        prog="loan-deposit",
        description="Reconcile loan deposit refunds and advise safe new "
                    "refunds from one settlement batch. Local only: no "
                    "refund is sent or revoked.")
    parser.add_argument("--loans", required=True,
                        help="loans input JSON (only loan ids are used)")
    parser.add_argument("--batch", required=True,
                        help="settlement batch JSON (assessments, deposits, "
                             "issued_refunds, bank_paid, confirmed_links)")
    parser.add_argument("--output", required=True,
                        help="result JSON path (create-only)")
    args = parser.parse_args(argv)
    try:
        loans_doc = _load_json(args.loans, "loans")
        batch = _load_json(args.batch, "batch")
        result = deposit.reconcile(loans_doc, batch)
        _write_create_only(args.output,
                           json.dumps(result, indent=2, sort_keys=True) + "\n")
    except (deposit.DepositError, OSError, ValueError, KeyError,
            TypeError) as exc:
        print(f"loan-deposit: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(
        {"bank_rows": len(result["bank_reconciliation"]),
         "matched": sum(1 for row in result["bank_reconciliation"]
                        if row["status"] == "matched"),
         "ambiguous": sum(1 for row in result["bank_reconciliation"]
                          if row["status"] == "ambiguous"),
         "unmatched": sum(1 for row in result["bank_reconciliation"]
                          if row["status"] == "unmatched"),
         "balances": len(result["balances"]),
         "refund_suggestions": len(result["refund_suggestions"])},
        sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
