"""Authorized publishing of scoped loan-audit reports (``loan-share``).

Operators maintain (collection_id, recipient, loan_id) authorizations and
issue frozen report packages; partner institutions fetch exactly the packages
issued to them.  Revoking an authorization permanently withdraws every
previously issued package for that collection/recipient that contains the
loan; re-granting never revives an old package, it only allows new ones.

The store is a single JSON document committed atomically (write temp file,
fsync, ``os.replace``, fsync directory), so a crash near commit leaves either
the complete old or the complete new state.  There is no concurrency and no
network service: sequential processes share the same on-disk store.

Failure injection for tests: set ``FAILPOINTS[name]`` in-process or the
``LOAN_SHARE_FAILPOINT`` environment variable to ``"<name>:<action>"`` where
action is ``raise`` (an InjectedFault I/O error) or ``crash`` (immediate
process exit, simulating a kill).  Names: ``commit.before_write`` and
``commit.after_replace``.
"""

import copy
import hashlib
import json
import os
from pathlib import Path

from .core import audit

SCHEMA_VERSION = 1
STORE_FILENAME = "store.json"

# Test failpoints: name -> "raise" | "crash".  Library tests only; the CLI
# additionally honors the LOAN_SHARE_FAILPOINT environment variable.
FAILPOINTS = {}


class ShareError(Exception):
    """Input, permission, identity, or storage failure (CLI exit code 2)."""


class InjectedFault(OSError):
    """Test-injected I/O failure at a commit failpoint."""


def _failpoint(name):
    action = FAILPOINTS.get(name)
    if action is None:
        spec = os.environ.get("LOAN_SHARE_FAILPOINT", "")
        point, _, act = spec.partition(":")
        if point == name:
            action = act or "raise"
    if action == "raise":
        raise InjectedFault(f"injected fault at {name}")
    if action == "crash":
        os._exit(3)


def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(obj):
    """SHA-256 hex of the canonical JSON encoding, per the publish contract."""
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def _is_identifier(value):
    return isinstance(value, str) and value != ""


def _require_identifier(request, key):
    value = request.get(key)
    if not _is_identifier(value):
        raise ShareError(f"{key} must be a non-empty string")
    return value


def _require_id_list(request, key, *, allow_empty):
    value = request.get(key)
    if not isinstance(value, list):
        raise ShareError(f"{key} must be a JSON array")
    if not allow_empty and not value:
        raise ShareError(f"{key} must not be empty")
    for item in value:
        if not _is_identifier(item):
            raise ShareError(f"{key} entries must be non-empty strings")
    if len(set(value)) != len(value):
        raise ShareError(f"{key} must not contain duplicates")
    return value


def _reject_unknown_keys(request, known):
    extra = sorted(set(request) - known)
    if extra:
        raise ShareError(f"unknown request fields: {extra}")


def load_request(path):
    """Read and minimally shape-check a JSON request file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ShareError(f"cannot read request file: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ShareError(f"request is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ShareError("request must be a JSON object")
    return data


def _empty_state():
    return {"version": SCHEMA_VERSION, "seq": 0, "changes": {}, "grants": {},
            "revocations": [], "releases": {}}


def _check_state(state):
    if not isinstance(state, dict) or state.get("version") != SCHEMA_VERSION:
        raise ShareError("store has an unsupported or corrupted format")
    for key, kind in (("seq", int), ("changes", dict), ("grants", dict),
                      ("revocations", list), ("releases", dict)):
        if not isinstance(state.get(key), kind):
            raise ShareError("store has an unsupported or corrupted format")
    return state


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Store:
    """A file-backed publishing store rooted at a directory."""

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / STORE_FILENAME
        self.state = self._load()

    def _load(self):
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _empty_state()
        except OSError as exc:
            raise ShareError(f"cannot read store: {exc}") from exc
        try:
            state = json.loads(text)
        except ValueError as exc:
            raise ShareError(f"store is corrupted: {exc}") from exc
        return _check_state(state)

    def commit(self, new_state):
        """Atomically replace the on-disk state; adopt it only on success."""
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f".{STORE_FILENAME}.{os.getpid()}.tmp"
        _failpoint("commit.before_write")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(_canonical(new_state))
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        os.replace(tmp, self.path)
        _fsync_dir(self.root)
        self.state = new_state
        _failpoint("commit.after_replace")


def _grants(state, collection_id, recipient):
    return state["grants"].get(collection_id, {}).get(recipient, {})


RIGHTS_KEYS = {"change_id", "collection_id", "recipient", "grant", "revoke"}


def apply_rights(store, request):
    """Validate and apply a rights change; return the receipt.

    Idempotent on ``change_id``: an identical retry returns the first receipt
    without re-applying anything; the same id with a different business
    request is rejected.
    """
    _reject_unknown_keys(request, RIGHTS_KEYS)
    change_id = _require_identifier(request, "change_id")
    collection_id = _require_identifier(request, "collection_id")
    recipient = _require_identifier(request, "recipient")
    grant = _require_id_list(request, "grant", allow_empty=True)
    revoke = _require_id_list(request, "revoke", allow_empty=True)
    if set(grant) & set(revoke):
        raise ShareError("grant and revoke must not overlap")
    normalized = {"collection_id": collection_id, "recipient": recipient,
                  "grant": sorted(grant), "revoke": sorted(revoke)}

    existing = store.state["changes"].get(change_id)
    if existing is not None:
        if existing != normalized:
            raise ShareError(
                f"change_id {change_id} was already used for a different request")
        return {"change_id": change_id, **copy.deepcopy(existing)}

    state = copy.deepcopy(store.state)
    state["seq"] += 1
    grants = state["grants"].setdefault(collection_id, {}).setdefault(recipient, {})
    for loan_id in grant:
        grants[loan_id] = True
    for loan_id in revoke:
        if loan_id in grants:
            # A real revocation: permanently withdraws every earlier package
            # for this collection/recipient containing the loan.
            del grants[loan_id]
            state["revocations"].append({"collection_id": collection_id,
                                         "recipient": recipient,
                                         "loan_id": loan_id,
                                         "seq": state["seq"]})
    state["changes"][change_id] = normalized
    store.commit(state)
    return {"change_id": change_id, **normalized}


ISSUE_KEYS = {"release_id", "collection_id", "recipient", "loan_ids", "source"}


def _issue_receipt(record):
    return {"release_id": record["release_id"],
            "collection_id": record["collection_id"],
            "recipient": record["recipient"],
            "loan_ids": list(record["loan_ids"]),
            "report_sha256": record["report_sha256"]}


def _scoped_report(source, loan_ids):
    """Audit only the selected loans and the events that reference them."""
    wanted = set(loan_ids)
    scoped = {"as_of": source["as_of"],
              "loans": [loan for loan in source["loans"] if loan["id"] in wanted],
              "events": [event for event in source["events"]
                         if event["loan_id"] in wanted]}
    return audit(scoped)


def issue_release(store, request):
    """Validate and issue a frozen release package; return the receipt.

    The full source must pass the original audit validation, every requested
    loan must exist in it and be authorized for the recipient.  Idempotent on
    ``release_id`` for identical business requests.
    """
    _reject_unknown_keys(request, ISSUE_KEYS)
    release_id = _require_identifier(request, "release_id")
    collection_id = _require_identifier(request, "collection_id")
    recipient = _require_identifier(request, "recipient")
    loan_ids = _require_id_list(request, "loan_ids", allow_empty=False)
    source = request.get("source")
    if not isinstance(source, dict):
        raise ShareError("source must be a JSON object")

    existing = store.state["releases"].get(release_id)
    if existing is not None:
        if (existing["collection_id"] != collection_id
                or existing["recipient"] != recipient
                or existing["loan_ids"] != sorted(loan_ids)
                or existing["source_sha256"] != _digest(source)):
            raise ShareError(
                f"release_id {release_id} was already used for a different request")
        return _issue_receipt(existing)

    # The complete source must pass the original audit validation; errors
    # outside the shared scope are not swallowed.
    try:
        audit(source)
    except (ValueError, KeyError, TypeError) as exc:
        raise ShareError(f"source failed audit validation: {exc}") from exc
    known_loans = {loan["id"] for loan in source["loans"]}
    missing = sorted(set(loan_ids) - known_loans)
    if missing:
        raise ShareError(f"loans not present in source: {missing}")
    granted = _grants(store.state, collection_id, recipient)
    denied = sorted(loan_id for loan_id in set(loan_ids) if loan_id not in granted)
    if denied:
        raise ShareError(f"loans not authorized for {recipient}: {denied}")

    try:
        report = _scoped_report(source, loan_ids)
    except (ValueError, KeyError, TypeError) as exc:  # pragma: no cover
        raise ShareError(f"scoped audit failed: {exc}") from exc

    state = copy.deepcopy(store.state)
    state["seq"] += 1
    record = {"release_id": release_id, "collection_id": collection_id,
              "recipient": recipient, "loan_ids": sorted(loan_ids),
              "source_sha256": _digest(source),
              "report_sha256": _digest(report),
              "issued_seq": state["seq"], "report": report}
    state["releases"][release_id] = record
    store.commit(state)
    return _issue_receipt(record)


def _is_withdrawn(state, record):
    """True if any real revocation after issue covers a loan in the package."""
    loans = set(record["loan_ids"])
    for rev in state["revocations"]:
        if (rev["collection_id"] == record["collection_id"]
                and rev["recipient"] == record["recipient"]
                and rev["loan_id"] in loans
                and rev["seq"] > record["issued_seq"]):
            return True
    return False


def fetch_release(store, release_id, recipient):
    """Return (receipt, frozen report) if the package may be delivered."""
    if not _is_identifier(release_id):
        raise ShareError("release-id must be a non-empty string")
    if not _is_identifier(recipient):
        raise ShareError("recipient must be a non-empty string")
    record = store.state["releases"].get(release_id)
    if record is None:
        raise ShareError(f"unknown release {release_id}")
    if record["recipient"] != recipient:
        raise ShareError(f"release {release_id} was issued to a different recipient")
    if _is_withdrawn(store.state, record):
        raise ShareError(f"release {release_id} has been permanently withdrawn")
    report = copy.deepcopy(record["report"])
    digest = _digest(report)
    if digest != record["report_sha256"]:
        raise ShareError(f"release {release_id} failed the integrity check")
    return {"release_id": release_id, "report_sha256": digest}, report


def write_create_only(path, text):
    """Write ``text`` to a new file only; never overwrite, never leave halves."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ShareError(f"cannot prepare output directory: {exc}") from exc
    try:
        fh = target.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ShareError(f"output already exists: {target}") from exc
    except OSError as exc:
        raise ShareError(f"cannot create output: {exc}") from exc
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
            raise ShareError(f"cannot write output: {exc}") from exc
        raise
