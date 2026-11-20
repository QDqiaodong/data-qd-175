"""Loan deposit refund reconciliation and safe refund advice (``loan-deposit``).

Pure business logic, kept separate from file I/O: :func:`reconcile` takes the
already-parsed loans document and settlement batch and returns the result
document.  Structural, identifier, time, quantity, and reference problems
raise :class:`DepositError`; the CLI turns them into exit code 2 and never
writes a partial result.

The batch holds five arrays that together are the complete material of one
settlement run: ``assessments`` (the fee approved for retention from each
loan's deposit), ``deposits`` (money actually received), ``issued_refunds``
(instructions already handed to the bank, with or without a success receipt),
``bank_paid`` (bank-confirmed refund movements), and ``confirmed_links``
(manual finance verifications overriding the automatic matching).

Reconciliation admits every manual link first.  Among the remaining rows an
automatic link is possible only when payer, amount and the 48-hour window all
agree.  Finance's objective is the maximum number of explainable successful
bank rows; a bank row is confirmed only when every maximum explanation
settles it against the same instruction, stays ambiguous when maximum
explanations disagree (including explanations that leave it empty), and is
unmatched only when no maximum explanation can link it at all.

Settlement then counts confirmed refunds as paid cash, every other issued
instruction as reserved money, and computes top-up, in-transit excess,
blocking bank rows, and create-only refund suggestions per loan.
"""

from datetime import datetime, timedelta
import re

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
WINDOW = timedelta(hours=48)

_ASSESSMENT_KEYS = {"loan_id", "fee_cents"}
_DEPOSIT_KEYS = {"deposit_id", "loan_id", "payer_ref", "received_at", "cents"}
_REFUND_KEYS = {"refund_id", "deposit_id", "sent_at", "cents"}
_BANK_KEYS = {"bank_id", "payer_ref", "paid_at", "cents"}
_LINK_KEYS = {"bank_id", "refund_id"}
_BATCH_KEYS = {"assessments", "deposits", "issued_refunds", "bank_paid",
               "confirmed_links"}


class DepositError(Exception):
    """Structure, identifier, time, quantity, or reference failure."""


def _is_id(value):
    return isinstance(value, str) and value != ""


def _is_int(value):
    # Booleans are ints in Python but a boolean is never an amount here.
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_unknown_keys(entry, known, where):
    unknown = sorted(set(entry) - known)
    if unknown:
        raise DepositError(f"{where} has unknown fields: {unknown}")


def _require_object(value, where):
    if not isinstance(value, dict):
        raise DepositError(f"{where} must be a JSON object")
    return value


def _require_array(value, where):
    if not isinstance(value, list):
        raise DepositError(f"{where} must be a JSON array")
    return value


def _require_id(entry, key, where):
    value = entry.get(key)
    if not _is_id(value):
        raise DepositError(f"{where}.{key} must be a non-empty string")
    return value


def _require_cents(entry, key, where, non_negative):
    value = entry.get(key)
    if not _is_int(value) or (non_negative and value < 0) or \
            (not non_negative and value <= 0):
        kind = "non-negative" if non_negative else "positive"
        raise DepositError(f"{where}.{key} must be a {kind} integer")
    return value


def _parse_timestamp(value, where):
    if not isinstance(value, str) or not _TIMESTAMP_RE.match(value):
        raise DepositError(f"{where} must be a timestamp YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise DepositError(f"{where} is not a valid calendar time") from None


def _validate_loans(loans_doc):
    """Only loan ``id`` is used; finance facts never come from other fields."""
    _require_object(loans_doc, "loans file")
    raw = _require_array(loans_doc.get("loans"), "loans file.loans")
    loans = {}
    for index, entry in enumerate(raw):
        where = f"loans file.loans[{index}]"
        _require_object(entry, where)
        # Only ``id`` is read; the other audit fields are tolerated so the
        # existing loans input format can be reused unchanged.
        loan_id = _require_id(entry, "id", where)
        if loan_id in loans:
            raise DepositError(f"{where}.id duplicates loan {loan_id}")
        loans[loan_id] = loan_id
    return loans


def _validate_batch(batch, loans):
    _require_object(batch, "batch file")
    _reject_unknown_keys(batch, _BATCH_KEYS, "batch file")
    for key in _BATCH_KEYS:
        _require_array(batch.get(key), f"batch file.{key}")

    # assessments: exactly one fee row per settled loan.
    fees = {}
    for index, entry in enumerate(batch["assessments"]):
        where = f"batch file.assessments[{index}]"
        _require_object(entry, where)
        _reject_unknown_keys(entry, _ASSESSMENT_KEYS, where)
        loan_id = _require_id(entry, "loan_id", where)
        if loan_id not in loans:
            raise DepositError(f"{where}.loan_id references unknown loan "
                               f"{loan_id}")
        if loan_id in fees:
            raise DepositError(f"{where} duplicates the assessment for loan "
                               f"{loan_id}")
        fees[loan_id] = _require_cents(entry, "fee_cents", where, True)

    # deposits: unique IDs, each belongs to one assessed loan.
    deposits = {}
    for index, entry in enumerate(batch["deposits"]):
        where = f"batch file.deposits[{index}]"
        _require_object(entry, where)
        _reject_unknown_keys(entry, _DEPOSIT_KEYS, where)
        deposit_id = _require_id(entry, "deposit_id", where)
        if deposit_id in deposits:
            raise DepositError(f"{where}.deposit_id duplicates deposit "
                               f"{deposit_id}")
        loan_id = _require_id(entry, "loan_id", where)
        if loan_id not in fees:
            raise DepositError(f"{where}.loan_id {loan_id} has no assessment "
                               f"in this batch")
        payer_ref = _require_id(entry, "payer_ref", where)
        received_at = _parse_timestamp(entry.get("received_at"),
                                       f"{where}.received_at")
        cents = _require_cents(entry, "cents", where, False)
        deposits[deposit_id] = {"deposit_id": deposit_id, "loan_id": loan_id,
                                "payer_ref": payer_ref,
                                "received_at": received_at, "cents": cents}

    # issued refunds: unique IDs, referencing a real deposit, never before the
    # deposit, and the whole instructions on one deposit never exceed receipt.
    refunds = {}
    issued_by_deposit = {deposit_id: 0 for deposit_id in deposits}
    for index, entry in enumerate(batch["issued_refunds"]):
        where = f"batch file.issued_refunds[{index}]"
        _require_object(entry, where)
        _reject_unknown_keys(entry, _REFUND_KEYS, where)
        refund_id = _require_id(entry, "refund_id", where)
        if refund_id in refunds:
            raise DepositError(f"{where}.refund_id duplicates refund "
                               f"{refund_id}")
        deposit_id = _require_id(entry, "deposit_id", where)
        if deposit_id not in deposits:
            raise DepositError(f"{where}.deposit_id references unknown "
                               f"deposit {deposit_id}")
        sent_at = _parse_timestamp(entry.get("sent_at"), f"{where}.sent_at")
        cents = _require_cents(entry, "cents", where, False)
        if sent_at < deposits[deposit_id]["received_at"]:
            raise DepositError(f"{where} is sent before its deposit "
                               f"{deposit_id} was received")
        issued_by_deposit[deposit_id] += cents
        if issued_by_deposit[deposit_id] > deposits[deposit_id]["cents"]:
            raise DepositError(
                f"{where} makes issued refunds on deposit {deposit_id} exceed "
                f"the {deposits[deposit_id]['cents']} cents received")
        refunds[refund_id] = {"refund_id": refund_id,
                              "deposit_id": deposit_id, "sent_at": sent_at,
                              "cents": cents}

    # bank rows: unique IDs, kept even when nothing can explain them.
    banks = {}
    for index, entry in enumerate(batch["bank_paid"]):
        where = f"batch file.bank_paid[{index}]"
        _require_object(entry, where)
        _reject_unknown_keys(entry, _BANK_KEYS, where)
        bank_id = _require_id(entry, "bank_id", where)
        if bank_id in banks:
            raise DepositError(f"{where}.bank_id duplicates bank row "
                               f"{bank_id}")
        payer_ref = _require_id(entry, "payer_ref", where)
        paid_at = _parse_timestamp(entry.get("paid_at"), f"{where}.paid_at")
        cents = _require_cents(entry, "cents", where, False)
        banks[bank_id] = {"bank_id": bank_id, "payer_ref": payer_ref,
                          "paid_at": paid_at, "cents": cents}

    # manual links: take precedence, but must still be sound, and neither a
    # bank row nor an instruction may appear in two links.
    manual_bank = {}
    manual_refund = set()
    for index, entry in enumerate(batch["confirmed_links"]):
        where = f"batch file.confirmed_links[{index}]"
        _require_object(entry, where)
        _reject_unknown_keys(entry, _LINK_KEYS, where)
        bank_id = _require_id(entry, "bank_id", where)
        if bank_id not in banks:
            raise DepositError(f"{where}.bank_id references unknown bank row "
                               f"{bank_id}")
        if bank_id in manual_bank:
            raise DepositError(f"{where}: bank row {bank_id} appears in more "
                               f"than one confirmed link")
        refund_id = _require_id(entry, "refund_id", where)
        if refund_id not in refunds:
            raise DepositError(f"{where}.refund_id references unknown refund "
                               f"instruction {refund_id}")
        if refund_id in manual_refund:
            raise DepositError(f"{where}: refund instruction {refund_id} "
                               f"appears in more than one confirmed link")
        bank = banks[bank_id]
        refund = refunds[refund_id]
        deposit = deposits[refund["deposit_id"]]
        if bank["payer_ref"] != deposit["payer_ref"]:
            raise DepositError(
                f"{where}: bank row {bank_id} payer {bank['payer_ref']} does "
                f"not match deposit {deposit['deposit_id']} payer "
                f"{deposit['payer_ref']}")
        if bank["cents"] != refund["cents"]:
            raise DepositError(
                f"{where}: bank row {bank_id} amount {bank['cents']} does not "
                f"match instruction {refund_id} amount {refund['cents']}")
        if bank["paid_at"] < refund["sent_at"]:
            raise DepositError(
                f"{where}: bank row {bank_id} is paid before instruction "
                f"{refund_id} was sent")
        manual_bank[bank_id] = refund_id
        manual_refund.add(refund_id)

    return fees, deposits, refunds, banks, manual_bank


def _auto_candidates(banks, refunds, deposits, manual_bank):
    """Surface candidates for every non-manual bank row.

    A candidate instruction must share payer (via its deposit) and amount and
    be paid within 48 hours of being sent, both ends included.  Manually
    linked rows and instructions never participate in automatic matching.
    Returns {bank_id: [refund_id, ...]} sorted by instruction ID.
    """
    refund_refs = {refund_id: deposits[refund["deposit_id"]]["payer_ref"]
                   for refund_id, refund in refunds.items()}
    used_refunds = set(manual_bank.values())
    candidates = {}
    for bank_id, bank in banks.items():
        if bank_id in manual_bank:
            continue
        options = []
        for refund_id, refund in refunds.items():
            if refund_id in used_refunds:
                continue
            if refund_refs[refund_id] != bank["payer_ref"]:
                continue
            if refund["cents"] != bank["cents"]:
                continue
            if refund["sent_at"] <= bank["paid_at"] <= \
                    refund["sent_at"] + WINDOW:
                options.append(refund_id)
        candidates[bank_id] = sorted(options)
    return candidates


def _maximum_matching(bank_ids, adjacency):
    """One maximum bipartite matching via Kuhn augmenting paths.

    Returns {bank_id: refund_id}; banks left unmatched are absent.
    """
    matched_refund = {}
    matched_bank = {}

    def augment(bank_id, seen):
        for refund_id in adjacency[bank_id]:
            if refund_id in seen:
                continue
            seen.add(refund_id)
            holder = matched_bank.get(refund_id)
            if holder is None or augment(holder, seen):
                matched_refund[bank_id] = refund_id
                matched_bank[refund_id] = bank_id
                return True
        return False

    for bank_id in bank_ids:
        augment(bank_id, set())
    return matched_refund


def _strong_components(nodes, outgoing):
    """Kosaraju SCCs; returns {node: component index} on the directed graph."""
    visited = set()
    order = []
    for start in nodes:
        if start in visited:
            continue
        stack = [(start, iter(outgoing.get(start, ())))]
        visited.add(start)
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append((nxt, iter(outgoing.get(nxt, ()))))
                    advanced = True
                    break
            if not advanced:
                order.append(node)
                stack.pop()
    reverse = {}
    for node in nodes:
        for nxt in outgoing.get(node, ()):
            reverse.setdefault(nxt, []).append(node)
    component = {}
    index = 0
    for start in reversed(order):
        if start in component:
            continue
        stack = [start]
        component[start] = index
        while stack:
            node = stack.pop()
            for nxt in reverse.get(node, ()):
                if nxt not in component:
                    component[nxt] = index
                    stack.append(nxt)
        index += 1
    return component


def _dfs_reachable(seeds, outgoing):
    reachable = set()
    stack = list(seeds)
    while stack:
        node = stack.pop()
        if node in reachable:
            continue
        reachable.add(node)
        stack.extend(outgoing.get(node, ()))
    return reachable


def _analyze_candidates(candidates):
    """Classify bank rows against *all* maximum-cardinality matchings.

    Uses the Dulmage-Mendelsohn decomposition instead of enumerating
    explanations (a batch of forty interchangeable rows has 40! maximum
    matchings).  Build one maximum matching M and the directed graph D with
    matched edges refund -> bank and non-matching edges bank -> refund (the
    alternating-path orientation), plus its reverse:

    * E: vertices reachable in D from the banks M leaves unmatched;
    * O: vertices reachable in reverse D from the refunds M leaves unmatched;
    * SCCs of D capture alternating cycles.

    A non-matching edge (bank, refund) is allowed — present in some maximum
    matching — iff its ends share an SCC, or both ends lie in E (alternating
    path from an unmatched bank), or both lie in O (alternating path to an
    unmatched refund).  A bank can be left unmatched by some maximum
    matching iff it belongs to E or its matched refund does.  Matched edges
    are allowed by definition.

    Returns ``(matched, possibilities, can_be_empty)`` where ``matched`` maps
    only rows settled by one identical instruction in every maximum matching.
    """
    bank_ids = sorted(candidates)
    adjacency = {bank_id: list(candidates[bank_id]) for bank_id in bank_ids}
    matching = _maximum_matching(bank_ids, adjacency)
    bank_of_refund = {refund_id: bank_id
                      for bank_id, refund_id in matching.items()}

    nodes = set(bank_ids)
    outgoing = {}
    reverse = {}
    for bank_id in bank_ids:
        for refund_id in adjacency[bank_id]:
            nodes.add(refund_id)
            if matching.get(bank_id) == refund_id:
                outgoing.setdefault(refund_id, []).append(bank_id)
                reverse.setdefault(bank_id, []).append(refund_id)
            else:
                outgoing.setdefault(bank_id, []).append(refund_id)
                reverse.setdefault(refund_id, []).append(bank_id)
    component = _strong_components(nodes, outgoing)

    unmatched_banks = [bank_id for bank_id in bank_ids
                       if bank_id not in matching]
    unmatched_refunds = [node for node in nodes
                         if node not in bank_ids
                         and node not in bank_of_refund]
    e_set = _dfs_reachable(unmatched_banks, outgoing)
    o_set = _dfs_reachable(unmatched_refunds, reverse)

    possibilities = {bank_id: set() for bank_id in bank_ids}
    can_be_empty = {}
    for bank_id in bank_ids:
        can_be_empty[bank_id] = (
            bank_id in e_set or matching.get(bank_id) in e_set)
        for refund_id in adjacency[bank_id]:
            if matching.get(bank_id) == refund_id or \
                    component[bank_id] == component[refund_id] or \
                    (bank_id in e_set and refund_id in e_set) or \
                    (bank_id in o_set and refund_id in o_set):
                possibilities[bank_id].add(refund_id)

    matched = {}
    for bank_id in bank_ids:
        possible = possibilities[bank_id]
        if len(possible) == 1 and not can_be_empty[bank_id]:
            matched[bank_id] = next(iter(possible))
    return matched, possibilities, can_be_empty


def _reconcile_banks(banks, refunds, deposits, manual_bank):
    candidates = _auto_candidates(banks, refunds, deposits, manual_bank)
    matched, possibilities, can_be_empty = _analyze_candidates(candidates)

    rows = []
    for bank_id in sorted(banks):
        if bank_id in manual_bank:
            refund_id = manual_bank[bank_id]
            rows.append({"bank_id": bank_id, "status": "matched",
                         "refund_id": refund_id,
                         "possible_refund_ids": [refund_id],
                         "can_be_unmatched": False})
            continue
        possible = sorted(possibilities[bank_id])
        if bank_id in matched:
            rows.append({"bank_id": bank_id, "status": "matched",
                         "refund_id": matched[bank_id],
                         "possible_refund_ids": possible,
                         "can_be_unmatched": False})
        elif possible:
            rows.append({"bank_id": bank_id, "status": "ambiguous",
                         "refund_id": None,
                         "possible_refund_ids": possible,
                         "can_be_unmatched": can_be_empty[bank_id]})
        else:
            rows.append({"bank_id": bank_id, "status": "unmatched",
                         "refund_id": None,
                         "possible_refund_ids": [],
                         "can_be_unmatched": True})
    # Map bank_id -> confirmed refund_id (manual and automatic together).
    confirmed = dict(manual_bank)
    confirmed.update(matched)
    return rows, confirmed


def _build_balances_and_suggestions(fees, deposits, refunds, banks,
                                    bank_rows, confirmed):
    # Payer refs that have at least one unsettled (unmatched/ambiguous) bank
    # row.  Such a payer blocks every loan that received money from them.
    blocking_by_ref = {}
    for row in bank_rows:
        if row["status"] != "matched":
            payer_ref = banks[row["bank_id"]]["payer_ref"]
            blocking_by_ref.setdefault(payer_ref, set()).add(row["bank_id"])

    deposits_by_loan = {loan_id: [] for loan_id in fees}
    for deposit_id in sorted(deposits):
        deposits_by_loan[deposits[deposit_id]["loan_id"]].append(deposit_id)

    refunds_by_deposit = {}
    for refund_id, refund in refunds.items():
        refunds_by_deposit.setdefault(refund["deposit_id"], []).append(refund)
    confirmed_refunds = set(confirmed.values())

    balances = []
    suggestions = []
    for loan_id in sorted(fees):
        loan_deposits = deposits_by_loan[loan_id]
        deposited = sum(deposits[d]["cents"] for d in loan_deposits)

        paid = 0
        reserved = 0
        for deposit_id in loan_deposits:
            for refund in refunds_by_deposit.get(deposit_id, ()):
                if refund["refund_id"] in confirmed_refunds:
                    paid += refund["cents"]
                else:
                    reserved += refund["cents"]

        fee = fees[loan_id]
        # Cash actually still available after confirmed refunds must cover the
        # approved fee; any shortfall is a top-up the partner owes now.
        top_up = max(0, fee - (deposited - paid))
        # In-transit instructions deepen that shortfall beyond the cash gap;
        # they are not cash already missing, just money earmarked in flight.
        excess_reserved = max(0, reserved - max(0, deposited - paid - fee))

        blocking = set()
        loan_refs = {deposits[d]["payer_ref"] for d in loan_deposits}
        for payer_ref in loan_refs:
            blocking.update(blocking_by_ref.get(payer_ref, ()))
        balances.append({
            "loan_id": loan_id,
            "deposited_cents": deposited,
            "fee_cents": fee,
            "paid_cents": paid,
            "reserved_cents": reserved,
            "top_up_cents": top_up,
            "excess_reserved_cents": excess_reserved,
            "blocking_bank_ids": sorted(blocking),
        })

        if blocking:
            continue
        # Safe-to-suggest remainder: receipt minus fee, confirmed refunds and
        # every instruction still in transit.  Suggestions only draw from the
        # loan's own deposits and never past a deposit's unreserved remainder.
        ordered = sorted(loan_deposits,
                         key=lambda d: (deposits[d]["received_at"], d))
        loan_remaining = deposited - fee - paid - reserved
        for deposit_id in ordered:
            if loan_remaining <= 0:
                break
            already = sum(r["cents"]
                          for r in refunds_by_deposit.get(deposit_id, ()))
            deposit_remaining = deposits[deposit_id]["cents"] - already
            cents = min(loan_remaining, deposit_remaining)
            if cents > 0:
                suggestions.append({"loan_id": loan_id,
                                    "deposit_id": deposit_id,
                                    "cents": cents})
                loan_remaining -= cents
    return balances, suggestions


def reconcile(loans_doc, batch):
    """Reconcile one settlement batch; return the complete result document.

    Raises :class:`DepositError` for any invalid material; no partial result
    is ever returned.
    """
    loans = _validate_loans(loans_doc)
    fees, deposits, refunds, banks, manual_bank = _validate_batch(batch,
                                                                  loans)
    bank_rows, confirmed = _reconcile_banks(banks, refunds, deposits,
                                            manual_bank)
    balances, suggestions = _build_balances_and_suggestions(
        fees, deposits, refunds, banks, bank_rows, confirmed)
    return {"bank_reconciliation": bank_rows,
            "balances": balances,
            "refund_suggestions": suggestions}
