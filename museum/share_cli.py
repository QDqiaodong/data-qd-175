import argparse
import json
import sys

from . import share


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f"loan-share: {message}\n")


def _build_parser():
    parser = _Parser(
        prog="loan-share",
        description="Publish scoped loan-audit reports under local authorization.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    rights = sub.add_parser("rights", help="apply a grant/revoke change")
    rights.add_argument("--store", required=True)
    rights.add_argument("--request", required=True)

    issue = sub.add_parser("issue", help="issue a frozen release package")
    issue.add_argument("--store", required=True)
    issue.add_argument("--request", required=True)

    fetch = sub.add_parser("fetch", help="fetch a frozen release package")
    fetch.add_argument("--store", required=True)
    fetch.add_argument("--release-id", required=True)
    fetch.add_argument("--recipient", required=True)
    fetch.add_argument("--output", required=True)
    return parser


def _dispatch(args):
    if args.command == "rights":
        request = share.load_request(args.request)
        store = share.Store(args.store)
        return share.apply_rights(store, request)
    if args.command == "issue":
        request = share.load_request(args.request)
        store = share.Store(args.store)
        return share.issue_release(store, request)
    store = share.Store(args.store)
    receipt, report = share.fetch_release(store, args.release_id, args.recipient)
    share.write_create_only(args.output, json.dumps(report, indent=2) + "\n")
    return receipt


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        receipt = _dispatch(args)
    except (share.ShareError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"loan-share: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
