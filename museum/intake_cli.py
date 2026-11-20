import argparse
import json
import sys

from . import intake


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f"loan-intake: {message}\n")


def _build_parser():
    parser = _Parser(
        prog="loan-intake",
        description="Intake of approved loan-data corrections and targeted withdrawals.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    init = sub.add_parser("init", help="initialize a new store from an audit input")
    init.add_argument("--store", required=True)
    init.add_argument("--source", required=True)

    snap = sub.add_parser("snapshot", help="export current data as an audit input")
    snap.add_argument("--store", required=True)
    snap.add_argument("--output", required=True)

    explain = sub.add_parser("explain", help="show a record and its field sources")
    explain.add_argument("--store", required=True)
    explain.add_argument("--kind", required=True, choices=("loan", "event"))
    explain.add_argument("--id", required=True)

    accept = sub.add_parser("accept", help="accept an approved correction")
    accept.add_argument("--store", required=True)
    accept.add_argument("--request", required=True)

    withdraw = sub.add_parser("withdraw", help="withdraw corrections and dependents")
    withdraw.add_argument("--store", required=True)
    withdraw.add_argument("--request", required=True)
    return parser


def _dispatch(args):
    if args.command == "init":
        source = intake.load_json(args.source, "source")
        return intake.init_store(intake.Store(args.store), source)
    if args.command == "snapshot":
        data = intake.snapshot(intake.Store(args.store))
        intake.write_create_only(args.output,
                                 json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return None
    if args.command == "explain":
        return intake.explain(intake.Store(args.store), args.kind, args.id)
    if args.command == "accept":
        request = intake.load_json(args.request, "request")
        return intake.accept(intake.Store(args.store), request)
    request = intake.load_json(args.request, "request")
    return intake.withdraw(intake.Store(args.store), request)


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        receipt = _dispatch(args)
    except (intake.IntakeError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"loan-intake: {exc}", file=sys.stderr)
        return 2
    if receipt is not None:
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
