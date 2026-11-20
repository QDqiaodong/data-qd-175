"""Pre-departure round-trip transport planning for loan batches (``loan-plan``).

Pure planning logic, kept separate from file I/O: ``plan_round`` takes the
already-parsed source (audit input) and request (plan input) documents and
returns the result document.  Only ``loans`` is read from the source (loan ID,
object ID, start, due); ``events`` and ``as_of`` are neither used nor required
to be valid.  Structural, identifier, time, quantity, and reference problems
raise :class:`PlanError`; a round whose confirmed batches cannot all be given
complete round trips raises :class:`InfeasibleError`.  The CLI turns both into
exit code 2.

Planning rules: the outbound leg goes home_site -> venue_site arriving no
later than exhibit_start; the return leg goes venue_site -> home_site
departing no earlier than exhibit_end.  Each leg is direct or makes exactly
one transfer at a hub that differs from the leg's endpoints, with a 1..24 hour
connection gap (both ends inclusive).  Every used run's depart and arrive UTC
dates must lie inside [start, due] of every loan in the batch.  Loads of all
scheduled batches sum against each run's capacity (capacity already includes
previously booked space, so old bookings are not subtracted again).

Selection: every confirmed batch (one with a previous booking) must be
scheduled or the whole round fails.  Among complete plans, maximize the sum of
priorities of newly accepted batches, then minimize disturbance of existing
bookings (per booked batch and run, the absolute difference between old and
new occupancy, unused side counting as zero), then take the lexicographically
smallest (batch_id, outbound, return) sequence sorted by batch_id.
"""

from datetime import date, datetime, timedelta
import re

MIN_TRANSFER = timedelta(hours=1)
MAX_TRANSFER = timedelta(hours=24)

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_REQUEST_KEYS = {"hubs", "runs", "batches", "previous_bookings"}
_RUN_KEYS = {"run_id", "from_site", "to_site", "depart", "arrive", "capacity"}
_BATCH_KEYS = {"batch_id", "home_site", "venue_site", "exhibit_start",
               "exhibit_end", "priority", "lots"}
_LOT_KEYS = {"loan_id", "units"}
_BOOKING_KEYS = {"batch_id", "outbound", "return", "units", "locked"}


class PlanError(Exception):
    """Structure, identifier, time, quantity, or reference failure."""


class InfeasibleError(PlanError):
    """The confirmed batches cannot all be given complete round trips."""


def _is_id(value):
    return isinstance(value, str) and value != ""


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_unknown_keys(entry, known, where):
    unknown = sorted(set(entry) - known)
    if unknown:
        raise PlanError(f"{where} has unknown fields: {unknown}")


def _require_id(entry, key, where):
    value = entry.get(key)
    if not _is_id(value):
        raise PlanError(f"{where}.{key} must be a non-empty string")
    return value


def _require_positive_int(entry, key, where):
    value = entry.get(key)
    if not _is_int(value) or value <= 0:
        raise PlanError(f"{where}.{key} must be a positive integer")
    return value


def _parse_timestamp(value, where):
    if not isinstance(value, str) or not _TIMESTAMP_RE.match(value):
        raise PlanError(f"{where} must be a timestamp YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise PlanError(f"{where} is not a valid calendar time") from None


def _parse_date(value, where):
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise PlanError(f"{where} must be a date YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise PlanError(f"{where} is not a valid calendar date") from None


def _validate_source(source):
    """Validate the audit input; return {loan_id: {object_id, start, due}}."""
    if not isinstance(source, dict):
        raise PlanError("source must be a JSON object")
    raw_loans = source.get("loans")
    if not isinstance(raw_loans, list):
        raise PlanError("source.loans must be a JSON array")
    loans = {}
    for index, entry in enumerate(raw_loans):
        where = f"source.loans[{index}]"
        if not isinstance(entry, dict):
            raise PlanError(f"{where} must be a JSON object")
        loan_id = _require_id(entry, "id", where)
        if loan_id in loans:
            raise PlanError(f"{where}.id duplicates loan {loan_id}")
        object_id = _require_id(entry, "object_id", where)
        start = _parse_date(entry.get("start"), f"{where}.start")
        due = _parse_date(entry.get("due"), f"{where}.due")
        if due < start:
            raise PlanError(f"{where} has start after due")
        loans[loan_id] = {"object_id": object_id, "start": start, "due": due}
    return loans


def _validate_hubs(value):
    if not isinstance(value, list):
        raise PlanError("request.hubs must be a JSON array")
    for index, hub in enumerate(value):
        if not _is_id(hub):
            raise PlanError(f"request.hubs[{index}] must be a non-empty string")
    if len(set(value)) != len(value):
        raise PlanError("request.hubs must not contain duplicates")
    return set(value)


def _validate_runs(value):
    if not isinstance(value, list):
        raise PlanError("request.runs must be a JSON array")
    runs = {}
    for index, entry in enumerate(value):
        where = f"request.runs[{index}]"
        if not isinstance(entry, dict):
            raise PlanError(f"{where} must be a JSON object")
        _reject_unknown_keys(entry, _RUN_KEYS, where)
        run_id = _require_id(entry, "run_id", where)
        if run_id in runs:
            raise PlanError(f"{where}.run_id duplicates run {run_id}")
        from_site = _require_id(entry, "from_site", where)
        to_site = _require_id(entry, "to_site", where)
        if from_site == to_site:
            raise PlanError(f"{where} has identical from_site and to_site")
        depart = _parse_timestamp(entry.get("depart"), f"{where}.depart")
        arrive = _parse_timestamp(entry.get("arrive"), f"{where}.arrive")
        if arrive <= depart:
            raise PlanError(f"{where} must arrive after it departs")
        capacity = entry.get("capacity")
        if not _is_int(capacity) or capacity < 0:
            raise PlanError(f"{where}.capacity must be a non-negative integer")
        runs[run_id] = {"run_id": run_id, "from_site": from_site,
                        "to_site": to_site, "depart": depart, "arrive": arrive,
                        "capacity": capacity}
    return runs


def _validate_batches(value, loans):
    if not isinstance(value, list):
        raise PlanError("request.batches must be a JSON array")
    batches = {}
    seen_loans = set()
    seen_objects = set()
    for index, entry in enumerate(value):
        where = f"request.batches[{index}]"
        if not isinstance(entry, dict):
            raise PlanError(f"{where} must be a JSON object")
        _reject_unknown_keys(entry, _BATCH_KEYS, where)
        batch_id = _require_id(entry, "batch_id", where)
        if batch_id in batches:
            raise PlanError(f"{where}.batch_id duplicates batch {batch_id}")
        home_site = _require_id(entry, "home_site", where)
        venue_site = _require_id(entry, "venue_site", where)
        if home_site == venue_site:
            raise PlanError(f"{where} has identical home_site and venue_site")
        exhibit_start = _parse_timestamp(entry.get("exhibit_start"),
                                         f"{where}.exhibit_start")
        exhibit_end = _parse_timestamp(entry.get("exhibit_end"),
                                       f"{where}.exhibit_end")
        if exhibit_end <= exhibit_start:
            raise PlanError(f"{where}.exhibit_end must be after exhibit_start")
        priority = _require_positive_int(entry, "priority", where)
        lots = entry.get("lots")
        if not isinstance(lots, list) or not lots:
            raise PlanError(f"{where}.lots must be a non-empty JSON array")
        total = 0
        windows = []
        for lot_index, lot in enumerate(lots):
            lot_where = f"{where}.lots[{lot_index}]"
            if not isinstance(lot, dict):
                raise PlanError(f"{lot_where} must be a JSON object")
            _reject_unknown_keys(lot, _LOT_KEYS, lot_where)
            loan_id = _require_id(lot, "loan_id", lot_where)
            if loan_id not in loans:
                raise PlanError(f"{lot_where}.loan_id references unknown loan "
                                f"{loan_id}")
            units = _require_positive_int(lot, "units", lot_where)
            if loan_id in seen_loans:
                raise PlanError(f"{lot_where}.loan_id reuses loan {loan_id} "
                                f"already present in this request")
            object_id = loans[loan_id]["object_id"]
            if object_id in seen_objects:
                raise PlanError(f"{lot_where}.loan_id reuses object "
                                f"{object_id} already present in this request")
            seen_loans.add(loan_id)
            seen_objects.add(object_id)
            total += units
            windows.append((loans[loan_id]["start"], loans[loan_id]["due"]))
        batches[batch_id] = {"batch_id": batch_id, "home_site": home_site,
                             "venue_site": venue_site,
                             "exhibit_start": exhibit_start,
                             "exhibit_end": exhibit_end, "priority": priority,
                             "units": total, "windows": tuple(windows)}
    return batches


def _validate_bookings(value, batches):
    if not isinstance(value, list):
        raise PlanError("request.previous_bookings must be a JSON array")
    bookings = {}
    for index, entry in enumerate(value):
        where = f"request.previous_bookings[{index}]"
        if not isinstance(entry, dict):
            raise PlanError(f"{where} must be a JSON object")
        _reject_unknown_keys(entry, _BOOKING_KEYS, where)
        batch_id = _require_id(entry, "batch_id", where)
        if batch_id not in batches:
            raise PlanError(f"{where}.batch_id references unknown batch "
                            f"{batch_id}")
        if batch_id in bookings:
            raise PlanError(f"{where}.batch_id duplicates the booking for "
                            f"batch {batch_id}")
        legs = {}
        for key in ("outbound", "return"):
            leg = entry.get(key)
            if not isinstance(leg, list) or not 1 <= len(leg) <= 2:
                raise PlanError(f"{where}.{key} must be a JSON array of one "
                                f"or two run IDs")
            for position, run_id in enumerate(leg):
                if not _is_id(run_id):
                    raise PlanError(f"{where}.{key}[{position}] must be a "
                                    f"non-empty string")
            legs[key] = leg
        if len(set(legs["outbound"] + legs["return"])) != \
                len(legs["outbound"]) + len(legs["return"]):
            raise PlanError(f"{where} must not repeat a run within the "
                            f"booking")
        units = _require_positive_int(entry, "units", where)
        locked = entry.get("locked")
        if not isinstance(locked, bool):
            raise PlanError(f"{where}.locked must be a boolean")
        bookings[batch_id] = {"batch_id": batch_id,
                              "outbound": legs["outbound"],
                              "return": legs["return"], "units": units,
                              "locked": locked}
    return bookings


def _validate_request(request, loans):
    if not isinstance(request, dict):
        raise PlanError("request must be a JSON object")
    _reject_unknown_keys(request, _REQUEST_KEYS, "request")
    hubs = _validate_hubs(request.get("hubs"))
    runs = _validate_runs(request.get("runs"))
    batches = _validate_batches(request.get("batches"), loans)
    bookings = _validate_bookings(request.get("previous_bookings"), batches)
    return hubs, runs, batches, bookings


def _date_ok(run, windows):
    """True if the run's depart and arrive UTC dates suit every loan window."""
    depart_day = run["depart"].date()
    arrive_day = run["arrive"].date()
    return all(start <= depart_day and arrive_day <= due
               for start, due in windows)


def _leg_routes(run_list, origin, dest, hubs, windows, first_ok, last_ok):
    """All valid routes origin -> dest: direct runs and one-hub transfers.

    ``first_ok`` constrains the first run of the route (return leg: departure
    no earlier than exhibit_end); ``last_ok`` constrains the last run
    (outbound leg: arrival no later than exhibit_start).  Routes are run ID
    tuples in departure order, sorted for determinism.
    """
    routes = []
    for run in run_list:
        if run["from_site"] == origin and run["to_site"] == dest \
                and first_ok(run) and last_ok(run) and _date_ok(run, windows):
            routes.append((run["run_id"],))
    for first in run_list:
        hub = first["to_site"]
        if first["from_site"] != origin or hub not in hubs or hub == dest:
            continue
        if not first_ok(first) or not _date_ok(first, windows):
            continue
        for second in run_list:
            if second["from_site"] != hub or second["to_site"] != dest:
                continue
            gap = second["depart"] - first["arrive"]
            if not MIN_TRANSFER <= gap <= MAX_TRANSFER:
                continue
            if not last_ok(second) or not _date_ok(second, windows):
                continue
            routes.append((first["run_id"], second["run_id"]))
    routes.sort()
    return routes


def _batch_routes(batch, run_list, hubs):
    """(outbound routes, return routes) for one batch."""
    outbound = _leg_routes(run_list, batch["home_site"], batch["venue_site"],
                           hubs, batch["windows"],
                           first_ok=lambda run: True,
                           last_ok=lambda run:
                           run["arrive"] <= batch["exhibit_start"])
    returns = _leg_routes(run_list, batch["venue_site"], batch["home_site"],
                          hubs, batch["windows"],
                          first_ok=lambda run:
                          run["depart"] >= batch["exhibit_end"],
                          last_ok=lambda run: True)
    return outbound, returns


def _disturbance(booking, new_runs, new_units):
    """Per-booking disturbance: summed |new - old| occupancy over runs."""
    old_usage = {run_id: booking["units"]
                 for run_id in booking["outbound"] + booking["return"]}
    new_usage = {run_id: new_units for run_id in new_runs}
    return sum(abs(new_usage.get(run_id, 0) - old_usage.get(run_id, 0))
               for run_id in set(old_usage) | set(new_usage))


def _build_domains(batches, bookings, run_list, hubs):
    """Per batch, the candidate round trips (plus None = waitlist for new).

    A candidate is (outbound, return, loads, disturbance) where loads is a
    tuple of (run_id, units) the batch adds to each used run.  Confirmed
    batches must have at least one candidate; locked bookings keep exactly the
    booked route and volume.
    """
    domains = {}
    for batch_id in sorted(batches):
        batch = batches[batch_id]
        booking = bookings.get(batch_id)
        outbound, returns = _batch_routes(batch, run_list, hubs)
        if booking is not None and booking["locked"]:
            booked_out = tuple(booking["outbound"])
            booked_return = tuple(booking["return"])
            if booking["units"] != batch["units"]:
                raise InfeasibleError(
                    f"locked booking for batch {batch_id} occupies "
                    f"{booking['units']} units but the batch now needs "
                    f"{batch['units']}")
            if booked_out not in outbound:
                raise InfeasibleError(
                    f"locked booking for batch {batch_id} cannot be "
                    f"preserved: its outbound route is not operable")
            if booked_return not in returns:
                raise InfeasibleError(
                    f"locked booking for batch {batch_id} cannot be "
                    f"preserved: its return route is not operable")
            pairs = [(booked_out, booked_return)]
        else:
            pairs = [(out, ret) for out in outbound for ret in returns]
        if booking is not None and not pairs:
            raise InfeasibleError(
                f"confirmed batch {batch_id} has no feasible round trip")
        candidates = []
        for out, ret in pairs:
            used = out + ret
            disturbance = (_disturbance(booking, used, batch["units"])
                           if booking is not None else 0)
            candidates.append((out, ret,
                               tuple((run_id, batch["units"])
                                      for run_id in used),
                               disturbance))
        # Low-disturbance, lexicographically early candidates first: the
        # search finds strong plans quickly.  Waitlisting comes last.
        candidates.sort(key=lambda cand: (cand[3], cand[0], cand[1]))
        if booking is None:
            candidates.append(None)
        domains[batch_id] = candidates
    return domains


def _lex_key(assignment):
    """(batch_id, outbound, return) sequence of scheduled batches, by ID."""
    return tuple((batch_id, assignment[batch_id][0], assignment[batch_id][1])
                 for batch_id in sorted(assignment)
                 if assignment[batch_id] is not None)


def _search(batches, bookings, runs, domains):
    """Best assignment batch_id -> (outbound, return) | None, or None.

    Objective, in order: maximize newly accepted priority sum, minimize
    booking disturbance, minimize the lexicographic plan key.
    """
    confirmed = sorted(batch_id for batch_id in batches
                       if batch_id in bookings)
    new = sorted((batch_id for batch_id in batches
                  if batch_id not in bookings),
                 key=lambda batch_id: (-batches[batch_id]["priority"],
                                       batch_id))
    order = confirmed + new
    suffix_priority = [0] * (len(order) + 1)
    for index in range(len(order) - 1, -1, -1):
        batch_id = order[index]
        gain = (batches[batch_id]["priority"] if batch_id not in bookings
                else 0)
        suffix_priority[index] = suffix_priority[index + 1] + gain

    best = {"score": None, "key": None, "assignment": None}
    loads = {}
    assignment = {}

    def dfs(index, priority, disturbance):
        if index == len(order):
            score = (priority, -disturbance)
            key = _lex_key(assignment)
            if best["score"] is None or score > best["score"] \
                    or (score == best["score"] and key < best["key"]):
                best["score"] = score
                best["key"] = key
                best["assignment"] = dict(assignment)
            return
        if best["score"] is not None:
            if priority + suffix_priority[index] < best["score"][0]:
                return
            if priority + suffix_priority[index] == best["score"][0] \
                    and disturbance > -best["score"][1]:
                return
        batch_id = order[index]
        is_new = batch_id not in bookings
        for candidate in domains[batch_id]:
            if candidate is None:
                assignment[batch_id] = None
                dfs(index + 1, priority, disturbance)
                del assignment[batch_id]
                continue
            out, ret, delta, added_disturbance = candidate
            if all(loads.get(run_id, 0) + units <= runs[run_id]["capacity"]
                   for run_id, units in delta):
                for run_id, units in delta:
                    loads[run_id] = loads.get(run_id, 0) + units
                assignment[batch_id] = (out, ret)
                dfs(index + 1,
                    priority + (batches[batch_id]["priority"] if is_new
                                else 0),
                    disturbance + added_disturbance)
                del assignment[batch_id]
                for run_id, units in delta:
                    loads[run_id] -= units

    dfs(0, 0, 0)
    return best["assignment"]


def _build_result(batches, bookings, assignment):
    scheduled = sorted(batch_id for batch_id in assignment
                       if assignment[batch_id] is not None)
    result_bookings = []
    changes = []
    for batch_id in scheduled:
        out, ret = assignment[batch_id]
        units = batches[batch_id]["units"]
        result_bookings.append({"batch_id": batch_id, "outbound": list(out),
                                "return": list(ret), "units": units})
        new_usage = {run_id: units for run_id in out + ret}
        booking = bookings.get(batch_id)
        old_usage = ({run_id: booking["units"]
                      for run_id in booking["outbound"] + booking["return"]}
                     if booking is not None else {})
        for run_id in sorted(set(old_usage) | set(new_usage)):
            delta = new_usage.get(run_id, 0) - old_usage.get(run_id, 0)
            if delta:
                changes.append({"batch_id": batch_id, "run_id": run_id,
                                "delta_units": delta})
    changes.sort(key=lambda change: (change["batch_id"], change["run_id"]))
    waitlisted = sorted(batch_id for batch_id in assignment
                        if assignment[batch_id] is None
                        and batch_id not in bookings)
    return {"bookings": result_bookings, "waitlisted": waitlisted,
            "changes": changes}


def plan_round(source, request):
    """Plan one round; return {bookings, waitlisted, changes}.

    Raises PlanError for invalid input and InfeasibleError when the confirmed
    batches cannot all be scheduled; no partial plan is ever returned.
    """
    loans = _validate_source(source)
    hubs, runs, batches, bookings = _validate_request(request, loans)
    run_list = sorted(runs.values(), key=lambda run: run["run_id"])
    domains = _build_domains(batches, bookings, run_list, hubs)
    assignment = _search(batches, bookings, runs, domains)
    if assignment is None:
        raise InfeasibleError(
            "confirmed batches cannot all be scheduled within run capacities")
    return _build_result(batches, bookings, assignment)
