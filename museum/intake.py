"""Intake of approved corrections and targeted withdrawals (``loan-intake``).

Registrars digitize paper loan and handover slips in batches.  This tool
keeps a local working store initialized from an existing audit input, accepts
human-approved corrections (new records and field fixes with checked old
values), and withdraws corrections whose source material turns out to be
misfiled — cascading to the corrections that declared them as their basis.
It never modifies the original export or the source slips and never touches
the network.

The store is a single JSON document committed atomically (write temp file,
fsync, ``os.replace``, fsync directory), so a crash near commit leaves either
the complete old or the complete new state; a write and its receipt persist
together.  There is no concurrency: sequential processes share the same
on-disk store.

Failure injection for tests: set ``FAILPOINTS[name]`` in-process or the
``LOAN_INTAKE_FAILPOINT`` environment variable to ``"<name>:<action>"`` where
action is ``raise`` (an InjectedFault I/O error) or ``crash`` (immediate
process exit, simulating a kill).  Names: ``commit.before_write`` and
``commit.after_replace``.
"""

import copy
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

SCHEMA_VERSION = 1
STORE_FILENAME = "store.json"

# Test failpoints: name -> "raise" | "crash".  Library tests only; the CLI
# additionally honors the LOAN_INTAKE_FAILPOINT environment variable.
FAILPOINTS = {}

LOAN_FIELDS = ("object_id", "borrower", "start", "due")
EVENT_FIELDS = ("loan_id", "type", "time", "location")
EVENT_OPTIONAL = ("condition_code",)
EVENT_TYPES = ("dispatch", "receipt", "condition")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# In-memory marker for "field explicitly removed" (a condition_code edit to
# null); never serialized — removal is stored as JSON null in the correction.
_ABSENT = object()


class IntakeError(Exception):
    """Input, state, storage, or path failure (CLI exit code 2)."""


class InjectedFault(OSError):
    """Test-injected I/O failure at a commit failpoint."""


def _failpoint(name):
    action = FAILPOINTS.get(name)
    if action is None:
        spec = os.environ.get("LOAN_INTAKE_FAILPOINT", "")
        point, _, act = spec.partition(":")
        if point == name:
            action = act or "raise"
    if action == "raise":
        raise InjectedFault(f"injected fault at {name}")
    if action == "crash":
        os._exit(3)


def _is_identifier(value):
    return isinstance(value, str) and value != ""


def _require_text(value, what):
    if not _is_identifier(value):
        raise IntakeError(f"{what} must be a non-empty string")
    return value


def _check_date(value, what):
    _require_text(value, what)
    if not _DATE_RE.match(value):
        raise IntakeError(f"{what} must be a YYYY-MM-DD date: {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise IntakeError(f"{what} is not a valid calendar date: {value!r}") from None
    return value


def _check_time(value, what):
    _require_text(value, what)
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise IntakeError(f"{what} must be an ISO 8601 timestamp: {value!r}") from None
    if stamp.tzinfo is None:
        raise IntakeError(f"{what} must carry a timezone offset: {value!r}")
    return value


def _fields_for(kind):
    if kind == "loan":
        return LOAN_FIELDS, ()
    return EVENT_FIELDS, EVENT_OPTIONAL


def _check_field_value(kind, field, value, what):
    if field in ("start", "due"):
        return _check_date(value, what)
    if field == "time":
        return _check_time(value, what)
    if field == "type":
        _require_text(value, what)
        if value not in EVENT_TYPES:
            raise IntakeError(f"{what} must be one of {list(EVENT_TYPES)}: {value!r}")
        return value
    return _require_text(value, what)


def _reject_unknown_keys(mapping, known, what):
    if not isinstance(mapping, dict):
        raise IntakeError(f"{what} must be a JSON object")
    extra = sorted(set(mapping) - known)
    if extra:
        raise IntakeError(f"{what} has unknown fields: {extra}")


def _require_id_list(request, key, *, allow_empty):
    value = request.get(key)
    if not isinstance(value, list):
        raise IntakeError(f"{key} must be a JSON array")
    if not allow_empty and not value:
        raise IntakeError(f"{key} must not be empty")
    for item in value:
        if not _is_identifier(item):
            raise IntakeError(f"{key} entries must be non-empty strings")
    if len(set(value)) != len(value):
        raise IntakeError(f"{key} must not contain duplicates")
    return list(value)


def load_json(path, what):
    """Read a JSON object request/source file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise IntakeError(f"cannot read {what} file: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise IntakeError(f"{what} file is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise IntakeError(f"{what} file must contain a JSON object")
    return data


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _empty_state():
    return {"version": SCHEMA_VERSION, "as_of": None, "seq": 0,
            "base_loans": {}, "base_events": {},
            "corrections": {}, "requests": {},
            "added_ids": {"loan": [], "event": []}}


def _check_state(state):
    if not isinstance(state, dict) or state.get("version") != SCHEMA_VERSION:
        raise IntakeError("store has an unsupported or corrupted format")
    for key, kind in (("seq", int), ("base_loans", dict), ("base_events", dict),
                      ("corrections", dict), ("requests", dict), ("added_ids", dict)):
        if not isinstance(state.get(key), kind):
            raise IntakeError("store has an unsupported or corrupted format")
    if not _is_identifier(state.get("as_of")):
        raise IntakeError("store has an unsupported or corrupted format")
    return state


class Store:
    """A file-backed intake store rooted at a directory."""

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / STORE_FILENAME

    def exists(self):
        return self.path.exists()

    def load(self):
        """Open an existing store; a missing store is an error here."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise IntakeError(f"store does not exist: {self.root}") from None
        except OSError as exc:
            raise IntakeError(f"cannot read store: {exc}") from exc
        try:
            state = json.loads(text)
        except ValueError as exc:
            raise IntakeError(f"store is corrupted: {exc}") from exc
        return _check_state(state)

    def commit(self, state):
        """Atomically replace the on-disk state."""
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f".{STORE_FILENAME}.{os.getpid()}.tmp"
        _failpoint("commit.before_write")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(state, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")))
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
        _failpoint("commit.after_replace")


# ---------------------------------------------------------------------------
# Current-state resolution
# ---------------------------------------------------------------------------

def _resolve(state):
    """Compute current records and per-field sources.

    Returns ``{"loan": {id: {"record": {...}, "sources": {field: cid|None}}},
    "event": ...}``.  Each field's value comes from the newest in-effect
    correction that explicitly supplied it, falling back to the base record
    (source ``None``).  A record added by a correction exists only while that
    correction is in effect.
    """
    corrections = state["corrections"]
    active = {cid for cid, c in corrections.items() if c["status"] == "active"}
    ordered = sorted(corrections.items(), key=lambda kv: kv[1]["seq"])
    return {"loan": _resolve_table(state, "loan", ordered, active),
            "event": _resolve_table(state, "event", ordered, active)}


def _resolve_table(state, kind, ordered, active):
    base = state["base_loans"] if kind == "loan" else state["base_events"]
    # rid -> field -> list of (seq, correction_id|None, value|_ABSENT)
    contribs = {}
    added_by = {}
    for rid, rec in base.items():
        contribs[rid] = {field: [(0, None, value)] for field, value in rec.items()}
    for cid, corr in ordered:
        for change in corr["changes"]:
            if change["kind"] != kind:
                continue
            rid = change["id"]
            if change["op"] == "add":
                added_by[rid] = cid
                contribs[rid] = {field: [(corr["seq"], cid, value)]
                                 for field, value in change["fields"].items()}
            else:
                fields = contribs.setdefault(rid, {})
                for field, value in change["fields"].items():
                    fields.setdefault(field, []).append(
                        (corr["seq"], cid, _ABSENT if value is None else value))
    table = {}
    for rid, fcontribs in contribs.items():
        if rid not in base and added_by.get(rid) not in active:
            continue  # never existed, or its add was withdrawn
        record, sources = {}, {}
        for field, entries in fcontribs.items():
            best = None
            for seq, cid, value in entries:
                if cid is None or cid in active:
                    if best is None or seq > best[0]:
                        best = (seq, cid, value)
            if best is None or best[2] is _ABSENT:
                continue
            record[field] = best[2]
            sources[field] = best[1]
        table[rid] = {"record": record, "sources": sources}
    return table


def _row(kind, rid, record):
    """One exported record, fields in schema order, id first."""
    required, optional = _fields_for(kind)
    row = {"id": rid}
    for field in (*required, *optional):
        if field in record:
            row[field] = record[field]
    return row


def _check_invariants(resolved):
    """Rules the current data must satisfy; returns a list of problems."""
    problems = []
    loans = resolved["loan"]
    for rid, entry in sorted(loans.items()):
        rec = entry["record"]
        if date.fromisoformat(rec["due"]) < date.fromisoformat(rec["start"]):
            problems.append(
                f"loan {rid} has start {rec['start']} after due {rec['due']}")
    for rid, entry in sorted(resolved["event"].items()):
        loan_id = entry["record"]["loan_id"]
        if loan_id not in loans:
            problems.append(f"event {rid} references unknown loan {loan_id}")
    return problems


def _check_correction_refs(state, resolved):
    """In-effect corrections must not point at records that no longer exist."""
    problems = []
    for cid, corr in sorted(state["corrections"].items(), key=lambda kv: kv[1]["seq"]):
        if corr["status"] != "active":
            continue
        for change in corr["changes"]:
            if change["op"] == "edit" and change["id"] not in resolved[change["kind"]]:
                problems.append(
                    f"correction {cid} edits {change['kind']} {change['id']}"
                    " which would no longer exist")
    return problems


# ---------------------------------------------------------------------------
# init / snapshot / explain
# ---------------------------------------------------------------------------

def _parse_record(kind, rec, what):
    required, optional = _fields_for(kind)
    _reject_unknown_keys(rec, {"id", *required, *optional}, what)
    rid = _require_text(rec.get("id"), f"{what} id")
    missing = [field for field in required if field not in rec]
    if missing:
        raise IntakeError(f"{what} is missing fields: {missing}")
    stored = {}
    for field in (*required, *optional):
        if field in rec:
            stored[field] = _check_field_value(kind, field, rec[field],
                                               f"{what} field {field}")
    return rid, stored


def _parse_source(source):
    _reject_unknown_keys(source, {"as_of", "loans", "events"}, "source")
    for key in ("as_of", "loans", "events"):
        if key not in source:
            raise IntakeError(f"source is missing {key}")
    as_of = _check_date(source["as_of"], "source as_of")
    if not isinstance(source["loans"], list) or not isinstance(source["events"], list):
        raise IntakeError("source loans and events must be JSON arrays")
    base_loans, base_events = {}, {}
    for index, rec in enumerate(source["loans"]):
        rid, stored = _parse_record("loan", rec, f"source loans[{index}]")
        if rid in base_loans:
            raise IntakeError(f"duplicate loan id {rid} in source")
        base_loans[rid] = stored
    for index, rec in enumerate(source["events"]):
        rid, stored = _parse_record("event", rec, f"source events[{index}]")
        if rid in base_events:
            raise IntakeError(f"duplicate event id {rid} in source")
        base_events[rid] = stored
    return as_of, base_loans, base_events


def init_store(store, source):
    """Initialize a new store from an existing audit input."""
    as_of, base_loans, base_events = _parse_source(source)
    if store.exists():
        raise IntakeError(f"store already exists: {store.root}")
    state = _empty_state()
    state["as_of"] = as_of
    state["base_loans"] = base_loans
    state["base_events"] = base_events
    problems = _check_invariants(_resolve(state))
    if problems:
        raise IntakeError("source violates current-data rules: " + "; ".join(problems))
    store.commit(state)
    return {"initialized": True}


def snapshot(store):
    """Current data as a plain audit input (as_of, loans, events only)."""
    state = store.load()
    resolved = _resolve(state)
    return {
        "as_of": state["as_of"],
        "loans": [_row("loan", rid, resolved["loan"][rid]["record"])
                  for rid in sorted(resolved["loan"])],
        "events": [_row("event", rid, resolved["event"][rid]["record"])
                   for rid in sorted(resolved["event"])],
    }


def explain(store, kind, rid):
    """One current record plus the correction currently supplying each field."""
    if kind not in ("loan", "event"):
        raise IntakeError("kind must be 'loan' or 'event'")
    _require_text(rid, "id")
    state = store.load()
    entry = _resolve(state)[kind].get(rid)
    if entry is None:
        base = state["base_loans"] if kind == "loan" else state["base_events"]
        if rid in base or rid in state["added_ids"][kind]:
            raise IntakeError(f"{kind} {rid} no longer exists")
        raise IntakeError(f"unknown {kind} {rid}")
    row = _row(kind, rid, entry["record"])
    sources = {field: entry["sources"][field] for field in row if field != "id"}
    return {"row": row, "field_sources": sources}


# ---------------------------------------------------------------------------
# accept
# ---------------------------------------------------------------------------

ACCEPT_KEYS = {"request_id", "based_on", "changes"}


def _check_edit_value(kind, field, value, what):
    if value is None:
        if field == "condition_code":
            return
        raise IntakeError(f"{what} must not be null")
    _check_field_value(kind, field, value, what)


def _normalize_change(change, index):
    """Structural validation of one change; returns a canonical copy."""
    what = f"changes[{index}]"
    if not isinstance(change, dict):
        raise IntakeError(f"{what} must be a JSON object")
    kind = change.get("kind")
    if kind not in ("loan", "event"):
        raise IntakeError(f"{what} kind must be 'loan' or 'event'")
    rid = _require_text(change.get("id"), f"{what} id")
    op = change.get("op")
    required, optional = _fields_for(kind)
    if op == "add":
        _reject_unknown_keys(change, {"kind", "id", "op", "fields"}, what)
        fields = change.get("fields")
        if not isinstance(fields, dict):
            raise IntakeError(f"{what} fields must be a JSON object")
        extra = sorted(set(fields) - set(required) - set(optional))
        if extra:
            raise IntakeError(f"{what} fields have unknown names: {extra}")
        missing = [field for field in required if field not in fields]
        if missing:
            raise IntakeError(f"{what} fields are missing: {missing}")
        checked = {field: _check_field_value(kind, field, value,
                                             f"{what} field {field}")
                   for field, value in fields.items()}
        return {"kind": kind, "id": rid, "op": "add", "fields": checked}
    if op == "edit":
        _reject_unknown_keys(change, {"kind", "id", "op", "expected", "fields"}, what)
        expected, fields = change.get("expected"), change.get("fields")
        if not isinstance(expected, dict) or not isinstance(fields, dict):
            raise IntakeError(f"{what} expected and fields must be JSON objects")
        if set(expected) != set(fields):
            raise IntakeError(f"{what} expected and fields must name the same fields")
        if not expected:
            raise IntakeError(f"{what} expected and fields must not be empty")
        editable = set(required) | set(optional)
        bad = sorted(set(expected) - editable)
        if bad:
            raise IntakeError(f"{what} cannot edit fields: {bad}")
        for field in expected:
            _check_edit_value(kind, field, expected[field], f"{what} expected {field}")
            _check_edit_value(kind, field, fields[field], f"{what} fields {field}")
        return {"kind": kind, "id": rid, "op": "edit",
                "expected": dict(expected), "fields": dict(fields)}
    raise IntakeError(f"{what} op must be 'add' or 'edit'")


def _check_retry(state, request_id, op, normalized):
    """Return the stored receipt for an equivalent retry; reject conflicts."""
    existing = state["requests"].get(request_id)
    if existing is None:
        return None
    if existing["op"] != op or existing["request"] != normalized:
        raise IntakeError(
            f"request_id {request_id} was already used for a different request")
    return copy.deepcopy(existing["receipt"])


def accept(store, request):
    """Validate and accept one approved correction; return the receipt.

    Idempotent on ``request_id``: an equivalent retry returns the first
    receipt without re-checking the current data; the same id with a
    different operation or content is rejected.  A failed accept consumes
    nothing.
    """
    state = store.load()
    _reject_unknown_keys(request, ACCEPT_KEYS, "request")
    request_id = _require_text(request.get("request_id"), "request_id")
    based_on = _require_id_list(request, "based_on", allow_empty=True)
    changes_raw = request.get("changes")
    if not isinstance(changes_raw, list) or not changes_raw:
        raise IntakeError("changes must be a non-empty JSON array")
    changes = [_normalize_change(change, index)
               for index, change in enumerate(changes_raw)]
    seen = set()
    for change in changes:
        key = (change["kind"], change["id"])
        if key in seen:
            raise IntakeError(
                f"duplicate change for {change['kind']} {change['id']}")
        seen.add(key)
    changes = sorted(changes, key=lambda c: (c["kind"], c["id"]))
    normalized = {"based_on": sorted(based_on), "changes": changes}

    receipt = _check_retry(state, request_id, "accept", normalized)
    if receipt is not None:
        return receipt

    corrections = state["corrections"]
    for cid in sorted(based_on):
        basis = corrections.get(cid)
        if basis is None:
            raise IntakeError(f"basis {cid} is not an accepted correction")
        if basis["status"] != "active":
            raise IntakeError(f"basis {cid} is no longer in effect")

    resolved = _resolve(state)
    for change in changes:
        kind, rid = change["kind"], change["id"]
        if change["op"] == "add":
            base = state["base_loans"] if kind == "loan" else state["base_events"]
            if rid in base or rid in state["added_ids"][kind]:
                raise IntakeError(f"{kind} id {rid} must not be reassigned")
        else:
            entry = resolved[kind].get(rid)
            if entry is None:
                raise IntakeError(f"cannot edit {kind} {rid}: not currently present")
            for field, want in change["expected"].items():
                current = entry["record"].get(field, _ABSENT)
                if want is None:
                    if current is not _ABSENT:
                        raise IntakeError(
                            f"edit of {kind} {rid}: expected {field} to be absent,"
                            f" found {current!r}")
                elif current is _ABSENT or current != want:
                    raise IntakeError(
                        f"edit of {kind} {rid}: expected {field} to be {want!r},"
                        f" found {'absent' if current is _ABSENT else current!r}")

    new_state = copy.deepcopy(state)
    new_state["seq"] += 1
    new_state["corrections"][request_id] = {
        "seq": new_state["seq"], "based_on": sorted(based_on),
        "changes": changes, "status": "active"}
    for change in changes:
        if change["op"] == "add":
            new_state["added_ids"][change["kind"]].append(change["id"])
    problems = _check_invariants(_resolve(new_state))
    if problems:
        raise IntakeError("correction rejected: " + "; ".join(problems))
    receipt = {"request_id": request_id, "status": "accepted"}
    new_state["requests"][request_id] = {
        "op": "accept", "request": normalized, "receipt": receipt}
    store.commit(new_state)
    return receipt


# ---------------------------------------------------------------------------
# withdraw
# ---------------------------------------------------------------------------

WITHDRAW_KEYS = {"request_id", "targets"}


def withdraw(store, request):
    """Withdraw in-effect corrections plus their dependents; permanent.

    The whole withdrawal is rejected (naming the records) if it would leave
    an in-effect correction pointing at a gone record, an event without its
    loan, or a loan with start after due; the set is never widened silently.
    Idempotent on ``request_id`` like ``accept``.
    """
    state = store.load()
    _reject_unknown_keys(request, WITHDRAW_KEYS, "request")
    request_id = _require_text(request.get("request_id"), "request_id")
    targets = _require_id_list(request, "targets", allow_empty=False)
    normalized = {"targets": sorted(targets)}

    receipt = _check_retry(state, request_id, "withdraw", normalized)
    if receipt is not None:
        return receipt

    corrections = state["corrections"]
    for cid in sorted(targets):
        correction = corrections.get(cid)
        if correction is None:
            raise IntakeError(f"unknown correction {cid}")
        if correction["status"] != "active":
            raise IntakeError(f"correction {cid} is not in effect")

    withdrawn = set(targets)
    grew = True
    while grew:
        grew = False
        for cid, corr in corrections.items():
            if (corr["status"] == "active" and cid not in withdrawn
                    and any(basis in withdrawn for basis in corr["based_on"])):
                withdrawn.add(cid)
                grew = True

    new_state = copy.deepcopy(state)
    for cid in withdrawn:
        new_state["corrections"][cid]["status"] = "withdrawn"
    resolved = _resolve(new_state)
    problems = _check_correction_refs(new_state, resolved) + _check_invariants(resolved)
    if problems:
        raise IntakeError("withdrawal rejected: " + "; ".join(problems))
    receipt = {"request_id": request_id, "withdrawn": sorted(withdrawn)}
    new_state["requests"][request_id] = {
        "op": "withdraw", "request": normalized, "receipt": receipt}
    store.commit(new_state)
    return receipt


def write_create_only(path, text):
    """Write ``text`` to a new file only; never overwrite, never leave halves."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IntakeError(f"cannot prepare output directory: {exc}") from exc
    try:
        fh = target.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise IntakeError(f"output already exists: {target}") from exc
    except OSError as exc:
        raise IntakeError(f"cannot create output: {exc}") from exc
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
            raise IntakeError(f"cannot write output: {exc}") from exc
        raise
