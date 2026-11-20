"""Post-loan transport temperature evidence (``loan-heat``).

After a loaned collection returns, conservators decide from the transport
box temperature log which loans may be released, which lack evidence and
need a human review, and which are confirmed over-temperature and must be
quarantined.  The device team separately needs per-probe health statistics
that tell a dropped probe from a dead sensor, a simultaneous-reading
collision, and a reading taken with no valid calibration.

The command is offline: it reads one existing audit input (only for loan and
object identity), one complete manifest for the transport batch, and a
possibly piped NDJSON export of probe samples.  Packing times are never
inferred from loan dates or handover events; the manifest's conservator
approved ``placements`` are the packing record.

Memory discipline: the export may hold two million lines, so it is validated
in one streaming pass and externally sorted by ``(probe, sample time)`` in a
per-run temp directory under ``--scratch``.  Only compact :class:`array`
tables are retained in memory.

Samples of the same probe and instant are one sample when their readings
agree; differing readings (including a reading and a fault) make that
instant a collision.  A sample supports the time from its timestamp until
``hold_seconds`` later or the probe's next sample, whichever is first.  Fault
and collision samples cut off earlier readings exactly like normal ones; no
reading is ever carried across a sample.  Calibration changes can split the
support of one reading into usable/uncalibrated parts and change its
corrected temperature.  All intervals are left-closed, right-open and have
positive length.  Times are integer epoch seconds from strict
``YYYY-MM-DDTHH:MM:SSZ`` values; temperatures are integer milli-degrees
Celsius and booleans are never integers.
"""

from array import array
from bisect import bisect_right
import calendar
from datetime import datetime, timedelta
import heapq
import itertools
import json
import os
import re
import shutil
import tempfile

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_EPOCH = datetime(1970, 1, 1)
READING, FAULT, COLLISION = 0, 1, 2
UNCERTAIN, HOT, COOL = 0, 1, 2
_CHUNK_ROWS = 50_000
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1

_SAMPLE_KEYS = {"probe_id", "at", "reading_milli"}
_MANIFEST_KEYS = {"start", "end", "probes", "limits", "placements"}
_PROBE_KEYS = {"probe_id", "hold_seconds", "calibrations"}
_CALIBRATION_KEYS = {"start", "end", "offset_milli"}
_LIMIT_KEYS = {"loan_id", "ceiling_milli", "alarm_seconds"}
_PLACEMENT_KEYS = {"loan_id", "start", "end", "probe_ids"}


class HeatError(Exception):
    """Structure, reference, time, path, or output failure (exit code 2)."""


# ---------------------------------------------------------------------------
# Validation helpers.


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_unknown(entry, known, where):
    unknown = sorted(set(entry) - known)
    if unknown:
        raise HeatError(f"{where} has unknown fields: {unknown}")


def _require_object(value, where):
    if not isinstance(value, dict):
        raise HeatError(f"{where} must be a JSON object")
    return value


def _require_array(value, where):
    if not isinstance(value, list):
        raise HeatError(f"{where} must be a JSON array")
    return value


def _require_id(entry, key, where):
    value = entry.get(key)
    if not isinstance(value, str) or value == "":
        raise HeatError(f"{where}.{key} must be a non-empty string")
    return value


def _require_int(entry, key, where):
    value = entry.get(key)
    if not _is_int(value):
        raise HeatError(f"{where}.{key} must be an integer")
    return value


def _require_positive_int(entry, key, where):
    value = _require_int(entry, key, where)
    if value <= 0:
        raise HeatError(f"{where}.{key} must be a positive integer")
    return value


def _parse_ts(value, where):
    if not isinstance(value, str) or not _TS_RE.match(value):
        raise HeatError(f"{where} must be a timestamp YYYY-MM-DDTHH:MM:SSZ")
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise HeatError(f"{where} is not a valid calendar time") from None
    return calendar.timegm(moment.timetuple())


def _format_ts(stamp):
    return (_EPOCH + timedelta(seconds=stamp)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Manifest and source validation.


class _Probe:
    """One probe's calibrations and its usable support inside the batch.

    ``ustarts``/``uends`` are the purely calibrated parts of each reading's
    support, sorted and non-overlapping; ``uvals`` holds the corrected
    temperature.  ``health`` accumulates the five mutually exclusive classes.
    """

    __slots__ = ("probe_id", "hold", "cal_starts", "cal_ends", "cal_offsets",
                 "ustarts", "uends", "uvals", "uoverflow", "health")

    HEALTH_KEYS = ("usable", "uncalibrated", "fault", "collision", "stale")

    def __init__(self, probe_id, hold, calibrations):
        self.probe_id = probe_id
        self.hold = hold
        self.cal_starts = array("q")
        self.cal_ends = array("q")
        self.cal_offsets = []
        for start, end, offset in calibrations:
            self.cal_starts.append(start)
            self.cal_ends.append(end)
            self.cal_offsets.append(offset)
        self.ustarts = array("q")
        self.uends = array("q")
        self.uvals = array("q")
        self.uoverflow = {}
        self.health = dict.fromkeys(self.HEALTH_KEYS, 0)

    def usable_value(self, index):
        return self.uoverflow.get(index, self.uvals[index])


def validate_source(doc):
    """Return {loan_id: object_id}; loan IDs must be unique.

    Only the audit input's ``loans`` identity is used.  Packing times are
    never taken from loan dates, events or the audit date.
    """
    _require_object(doc, "source")
    raw = _require_array(doc.get("loans"), "source.loans")
    loans = {}
    for index, entry in enumerate(raw):
        where = f"source.loans[{index}]"
        _require_object(entry, where)
        loan_id = _require_id(entry, "id", where)
        object_id = _require_id(entry, "object_id", where)
        if loan_id in loans:
            raise HeatError(f"{where}.id duplicates loan {loan_id}")
        loans[loan_id] = object_id
    return loans


def _parse_calibrations(raw, where, probe_id):
    calibrations = []
    for index, entry in enumerate(raw):
        spot = f"{where}.calibrations[{index}]"
        _require_object(entry, spot)
        _reject_unknown(entry, _CALIBRATION_KEYS, spot)
        start = _parse_ts(entry.get("start"), f"{spot}.start")
        end = _parse_ts(entry.get("end"), f"{spot}.end")
        if start >= end:
            raise HeatError(f"{spot}.start must be before {spot}.end")
        offset = _require_int(entry, "offset_milli", spot)
        calibrations.append((start, end, offset))
    calibrations.sort(key=lambda item: item[0])
    for index in range(1, len(calibrations)):
        if calibrations[index][0] < calibrations[index - 1][1]:
            raise HeatError(
                f"{where}.calibrations[{index}] overlaps the previous "
                f"calibration of probe {probe_id}")
    return calibrations


def validate_manifest(doc, loans):
    """Validate the whole transport manifest; return its parsed form."""
    _require_object(doc, "manifest")
    _reject_unknown(doc, _MANIFEST_KEYS, "manifest")
    batch_start = _parse_ts(doc.get("start"), "manifest.start")
    batch_end = _parse_ts(doc.get("end"), "manifest.end")
    if batch_start >= batch_end:
        raise HeatError("manifest.start must be before manifest.end")

    probes = []
    probe_index = {}
    raw_probes = _require_array(doc.get("probes"), "manifest.probes")
    for index, entry in enumerate(raw_probes):
        where = f"manifest.probes[{index}]"
        _require_object(entry, where)
        _reject_unknown(entry, _PROBE_KEYS, where)
        probe_id = _require_id(entry, "probe_id", where)
        if probe_id in probe_index:
            raise HeatError(f"{where}.probe_id duplicates probe {probe_id}")
        hold = _require_positive_int(entry, "hold_seconds", where)
        calibrations = _parse_calibrations(
            _require_array(entry.get("calibrations"),
                           f"{where}.calibrations"),
            where, probe_id)
        probe_index[probe_id] = len(probes)
        probes.append(_Probe(probe_id, hold, calibrations))
    probes.sort(key=lambda probe: probe.probe_id)
    probe_index = {probe.probe_id: index for index, probe in enumerate(probes)}

    limits = {}
    raw_limits = _require_array(doc.get("limits"), "manifest.limits")
    for index, entry in enumerate(raw_limits):
        where = f"manifest.limits[{index}]"
        _require_object(entry, where)
        _reject_unknown(entry, _LIMIT_KEYS, where)
        loan_id = _require_id(entry, "loan_id", where)
        if loan_id not in loans:
            raise HeatError(
                f"{where}.loan_id references unknown loan {loan_id}")
        if loan_id in limits:
            raise HeatError(f"{where} duplicates the limit for loan {loan_id}")
        limits[loan_id] = {
            "ceiling": _require_int(entry, "ceiling_milli", where),
            "alarm": _require_positive_int(entry, "alarm_seconds", where)}

    placements = []
    raw_placements = _require_array(doc.get("placements"),
                                    "manifest.placements")
    for index, entry in enumerate(raw_placements):
        where = f"manifest.placements[{index}]"
        _require_object(entry, where)
        _reject_unknown(entry, _PLACEMENT_KEYS, where)
        loan_id = _require_id(entry, "loan_id", where)
        if loan_id not in limits:
            raise HeatError(
                f"{where}.loan_id {loan_id} has no limit in this batch")
        start = _parse_ts(entry.get("start"), f"{where}.start")
        end = _parse_ts(entry.get("end"), f"{where}.end")
        if start >= end:
            raise HeatError(f"{where}.start must be before {where}.end")
        if start < batch_start or end > batch_end:
            raise HeatError(f"{where} lies outside the batch interval")
        raw_probe_ids = _require_array(entry.get("probe_ids"),
                                       f"{where}.probe_ids")
        probe_ids = []
        for position, value in enumerate(raw_probe_ids):
            spot = f"{where}.probe_ids[{position}]"
            if not isinstance(value, str) or value == "":
                raise HeatError(f"{spot} must be a non-empty string")
            if value not in probe_index:
                raise HeatError(f"{spot} references unknown probe {value}")
            probe_ids.append(value)
        if len(set(probe_ids)) != len(probe_ids):
            raise HeatError(f"{where}.probe_ids must not contain duplicates")
        if len(probe_ids) < 2:
            raise HeatError(f"{where}.probe_ids must list at least two probes")
        placements.append({
            "loan_id": loan_id, "start": start, "end": end,
            "probe_indices": tuple(probe_index[value] for value in probe_ids),
            "index": index})

    covered = {placement["loan_id"] for placement in placements}
    for loan_id in limits:
        if loan_id not in covered:
            raise HeatError(
                f"manifest.limits limit for loan {loan_id} has no placement")

    by_object = {}
    for placement in placements:
        by_object.setdefault(loans[placement["loan_id"]], []).append(placement)
    for object_id, group in by_object.items():
        group.sort(key=lambda placement: (placement["start"],
                                          placement["end"]))
        for previous, current in zip(group, group[1:]):
            if previous["end"] > current["start"]:
                raise HeatError(
                    f"manifest.placements[{previous['index']}] and "
                    f"[{current['index']}] overlap for object {object_id}")

    placements.sort(key=lambda placement: (placement["start"],
                                           placement["end"],
                                           placement["loan_id"]))
    return {
        "start": batch_start, "end": batch_end,
        "seconds": batch_end - batch_start,
        "probes": probes, "probe_index": probe_index,
        "limits": limits, "placements": placements}


# ---------------------------------------------------------------------------
# Streaming sample validation, external sort, and usable support.


def _parse_sample_line(line, where, probe_index):
    try:
        entry = json.loads(line)
    except ValueError as exc:
        raise HeatError(f"{where} is not valid JSON: {exc}") from None
    if not isinstance(entry, dict):
        raise HeatError(f"{where} must be a JSON object")
    _reject_unknown(entry, _SAMPLE_KEYS, where)
    missing = sorted(_SAMPLE_KEYS - set(entry))
    if missing:
        raise HeatError(f"{where} is missing fields: {missing}")
    probe_id = entry["probe_id"]
    if not isinstance(probe_id, str) or probe_id == "":
        raise HeatError(f"{where}.probe_id must be a non-empty string")
    if probe_id not in probe_index:
        raise HeatError(
            f"{where}.probe_id references unknown probe {probe_id}")
    at = _parse_ts(entry.get("at"), f"{where}.at")
    reading = entry.get("reading_milli")
    if reading is None:
        return probe_index[probe_id], at, FAULT, 0
    if not _is_int(reading):
        raise HeatError(
            f"{where}.reading_milli must be an integer or null")
    return probe_index[probe_id], at, READING, reading


def _iter_records(lines, probe_index, label):
    """Yield parsed samples from ``lines``, accepting bytes or text rows.

    Each byte row is decoded on its own so an invalid UTF-8 sequence names
    exactly the line that contains it.
    """
    for number, line in enumerate(lines, 1):
        where = f"{label} line {number}"
        if isinstance(line, bytes):
            try:
                line = line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HeatError(f"{where} is not valid UTF-8: {exc}") \
                    from None
        yield _parse_sample_line(line, where, probe_index)


def _spill(records, directory, number):
    path = os.path.join(directory, f"chunk-{number:06d}.ndjson")
    try:
        with open(path, "w", encoding="ascii", newline="\n") as handle:
            for probe, at, kind, value in records:
                handle.write(f"{probe}\t{at}\t{kind}\t{value}\n")
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _read_chunk(path):
    with open(path, "r", encoding="ascii", newline="\n") as handle:
        for line in handle:
            probe, at, kind, value = line.split("\t")
            yield int(probe), int(at), int(kind), int(value)


def _merged_samples(lines, probe_index, label, scratch_dir):
    """Yield (probe, at, kind, value) sorted, one entry per sample instant.

    Rows with the same probe and instant collapse to one sample when their
    readings are identical and to a collision sample otherwise; a fault and a
    reading at the same instant are a collision too.
    """
    directory = tempfile.mkdtemp(prefix="samples-", dir=scratch_dir)
    try:
        chunks = []
        buffer = []
        number = 0
        try:
            for record in _iter_records(lines, probe_index, label):
                buffer.append(record)
                if len(buffer) >= _CHUNK_ROWS:
                    buffer.sort()
                    chunks.append(_spill(buffer, directory, number))
                    number += 1
                    buffer = []
            if buffer:
                buffer.sort()
                chunks.append(_spill(buffer, directory, number))
                buffer = []
        except BaseException:
            for path in chunks:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            raise

        streams = [_read_chunk(path) for path in chunks]
        try:
            for (probe, at), group in itertools.groupby(
                    heapq.merge(*streams), key=lambda row: (row[0], row[1])):
                first = next(group)
                kind, value = first[2], first[3]
                conflicted = False
                for row in group:
                    if row[2] != kind or (kind == READING
                                          and row[3] != value):
                        conflicted = True
                if conflicted:
                    yield probe, at, COLLISION, 0
                else:
                    yield probe, at, kind, value
        finally:
            for stream in streams:
                stream.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _calibration_at(probe, moment):
    """Return (offset or None, next calibration boundary)."""
    index = bisect_right(probe.cal_starts, moment) - 1
    if index >= 0 and probe.cal_ends[index] > moment:
        return probe.cal_offsets[index], probe.cal_ends[index]
    if index + 1 < len(probe.cal_starts):
        return None, probe.cal_starts[index + 1]
    return None, None


def _add_reading_support(probe, start, end, value):
    """Split a reading's support at calibration boundaries."""
    moment = start
    while moment < end:
        offset, boundary = _calibration_at(probe, moment)
        stop = end if boundary is None else min(end, boundary)
        if offset is None:
            probe.health["uncalibrated"] += stop - moment
        else:
            probe.health["usable"] += stop - moment
            corrected = value + offset
            probe.ustarts.append(moment)
            probe.uends.append(stop)
            if _INT64_MIN <= corrected <= _INT64_MAX:
                probe.uvals.append(corrected)
            else:
                probe.uvals.append(0)
                probe.uoverflow[len(probe.ustarts) - 1] = corrected
        moment = stop


def _close_support(probe, start, end, kind, value, batch):
    """Clip one sample's raw support to the batch and classify it."""
    start = max(start, batch["start"])
    end = min(end, batch["end"])
    if start >= end:
        return
    if kind == READING:
        _add_reading_support(probe, start, end, value)
    elif kind == FAULT:
        probe.health["fault"] += end - start
    else:
        probe.health["collision"] += end - start


def attach_samples(batch, rows):
    """Build each probe's usable support and health counters in one pass."""
    probes = batch["probes"]
    batch_start, batch_end = batch["start"], batch["end"]
    carry = None      # last sample strictly before the batch start
    previous = None   # last sample already taken inside the batch
    current = None
    for probe_index, at, kind, value in rows:
        if probe_index != current:
            if current is not None:
                _finish_probe(probes[current], carry, previous, batch)
            current = probe_index
            carry = None
            previous = None
        if at >= batch_end:
            continue
        if at < batch_start:
            carry = (at, kind, value)
            continue
        if previous is None and carry is not None:
            # The carried sample is cut by the first sample in the batch.
            probe = probes[probe_index]
            _close_support(probe, carry[0],
                           min(carry[0] + probe.hold, at),
                           carry[1], carry[2], batch)
            carry = None
        if previous is not None:
            probe = probes[probe_index]
            _close_support(probe, previous[0],
                           min(previous[0] + probe.hold, at),
                           previous[1], previous[2], batch)
        previous = (at, kind, value)
    if current is not None:
        _finish_probe(probes[current], carry, previous, batch)

    total = batch["seconds"]
    for probe in probes:
        health = probe.health
        health["stale"] = (total - health["usable"] - health["uncalibrated"]
                           - health["fault"] - health["collision"])


def _finish_probe(probe, carry, previous, batch):
    """Close a probe's trailing support; ``previous`` is its last sample.

    ``carry`` is still set only when the probe had no sample inside the
    batch, so it runs to its hold expiry or the batch end.
    """
    if previous is not None:
        _close_support(probe, previous[0],
                       min(previous[0] + probe.hold, batch["end"]),
                       previous[1], previous[2], batch)
    elif carry is not None:
        _close_support(probe, carry[0],
                       min(carry[0] + probe.hold, batch["end"]),
                       carry[1], carry[2], batch)


# ---------------------------------------------------------------------------
# Per-placement evidence and per-loan runs.


def _probe_boundaries(probe, start, end, ceiling):
    """Yield (time, delta, hot) boundaries of usable segments in [start, end)."""
    stats = probe.ustarts
    position = bisect_right(stats, start) - 1
    if position < 0:
        position = 0
    while position < len(stats) and stats[position] < end:
        lo = max(stats[position], start)
        hi = min(probe.uends[position], end)
        if lo < hi:
            hot = probe.usable_value(position) > ceiling
            yield lo, 1, hot
            yield hi, -1, hot
        position += 1


def _placement_intervals(batch, placement, ceiling):
    """Yield (start, end, classification) for one packing segment, in order.

    Adjacent equal classes are merged.  Spans with fewer than two usable
    probes -- including spans before the first and after the last usable
    reading -- are uncertain.  A heap merges the per-probe boundaries so
    memory stays in proportion to the probe count, not to the samples.
    """
    start, end = placement["start"], placement["end"]
    heap = []
    for slot, probe_index in enumerate(placement["probe_indices"]):
        probe = batch["probes"][probe_index]
        stream = _probe_boundaries(probe, start, end, ceiling)
        first = next(stream, None)
        if first is not None:
            heapq.heappush(heap, (first[0], first[1], first[2], slot, stream))

    active = {True: 0, False: 0}
    count = 0
    pending = None

    def classify(low, high):
        if high <= low:
            return None
        if count < 2:
            return UNCERTAIN
        if active[False] == 0:
            return HOT
        if active[True] == 0:
            return COOL
        return UNCERTAIN

    def extend(low, high):
        nonlocal pending
        kind = classify(low, high)
        if kind is None:
            return None
        if pending is not None and pending[1] == low and pending[2] == kind:
            pending = (pending[0], high, kind)
            return None
        done = pending
        pending = (low, high, kind)
        return done

    moment = start
    while heap:
        at = heap[0][0]
        done = extend(moment, at)
        if done is not None:
            yield done
        while heap and heap[0][0] == at:
            _, delta, hot, slot, stream = heapq.heappop(heap)
            if delta > 0:
                active[hot] += 1
                count += 1
            else:
                active[hot] -= 1
                count -= 1
            following = next(stream, None)
            if following is not None:
                heapq.heappush(heap, (following[0], following[1],
                                      following[2], slot, stream))
        moment = at
    done = extend(moment, end)
    if done is not None:
        yield done
    if pending is not None:
        yield pending


def _loan_row(batch, loan_id, placements, limit):
    """Summarize one loan from its placements' classified intervals.

    Placements are already ordered by start and never overlap, so the
    intervals arrive in time order and nothing needs to be buffered.
    """
    ceiling = limit["ceiling"]
    monitored = 0
    uncertain = 0
    longest = 0
    longest_start = None
    run_end = None
    run_start = None
    for placement in placements:
        monitored += placement["end"] - placement["start"]
        for start, end, kind in _placement_intervals(batch, placement,
                                                     ceiling):
            if kind == UNCERTAIN:
                uncertain += end - start
                run_end = None
            elif kind == HOT:
                if run_end is not None and run_end == start:
                    run_end = end
                else:
                    run_start, run_end = start, end
                length = run_end - run_start
                if length > longest:
                    longest = length
                    longest_start = run_start
            else:
                run_end = None
    if longest == 0:
        longest_start = None
    decision = "release"
    if longest >= limit["alarm"]:
        decision = "isolate"
    elif uncertain > 0:
        decision = "review"
    return {
        "loan_id": loan_id,
        "monitored_seconds": monitored,
        "uncertain_seconds": uncertain,
        "longest_hot_seconds": longest,
        "longest_hot_start": (None if longest_start is None
                              else _format_ts(longest_start)),
        "decision": decision}


def evaluate(batch):
    """Assemble the two result sections for a validated batch."""
    flat = {}
    for placement in batch["placements"]:
        flat.setdefault(placement["loan_id"], []).append(placement)
    rows = []
    for loan_id in sorted(batch["limits"]):
        rows.append(_loan_row(batch, loan_id, flat[loan_id],
                              batch["limits"][loan_id]))
    health = []
    for probe in batch["probes"]:
        row = {"probe_id": probe.probe_id}
        row.update(probe.health)
        health.append(row)
    return {"loans": rows, "probe_health": health}


def compute(source, manifest, lines, scratch_dir=None, label="samples"):
    """Validate everything and produce the result from sample ``lines``.

    ``lines`` is an iterable of text rows (an open file, a list, or a pipe);
    it is consumed exactly once.  Temporary sort chunks go under
    ``scratch_dir`` (the system temp directory by default) and are always
    removed before returning or raising.
    """
    loans = validate_source(source)
    batch = validate_manifest(manifest, loans)
    rows = _merged_samples(lines, batch["probe_index"], label, scratch_dir)
    attach_samples(batch, rows)
    return evaluate(batch)

