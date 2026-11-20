"""Library tests for ``loan-heat`` (transport temperature evidence)."""

import copy
import json
import tempfile
import unittest

from museum.heat import HeatError, compute

T0 = "2026-06-24T10:00:00Z"


def _source(*loans):
    return {"as_of": "2026-06-30",
            "loans": [{"id": loan_id, "object_id": object_id,
                       "borrower": "b", "start": "2026-06-01",
                       "due": "2026-06-20"}
                      for loan_id, object_id in loans],
            "events": []}


def _probe(probe_id, hold=60, calibrations=None):
    if calibrations is None:
        calibrations = [{"start": "2026-06-24T09:00:00Z",
                         "end": "2026-06-25T00:00:00Z",
                         "offset_milli": 0}]
    return {"probe_id": probe_id, "hold_seconds": hold,
            "calibrations": calibrations}


def _manifest(probes, limits, placements, start=T0,
              end="2026-06-24T10:01:00Z"):
    return {"start": start, "end": end, "probes": probes,
            "limits": limits, "placements": placements}


def _limit(loan_id, ceiling=20000, alarm=10):
    return {"loan_id": loan_id, "ceiling_milli": ceiling,
            "alarm_seconds": alarm}


def _placement(loan_id, probe_ids, start=T0, end="2026-06-24T10:01:00Z"):
    return {"loan_id": loan_id, "start": start, "end": end,
            "probe_ids": list(probe_ids)}


def _lines(*rows):
    return "".join(json.dumps(row) + "\n" for row in rows)


def _sample(probe_id, at, reading):
    return {"probe_id": probe_id, "at": at, "reading_milli": reading}


def _loan(result, loan_id):
    return {row["loan_id"]: row for row in result["loans"]}[loan_id]


def _health(result, probe_id):
    return {row["probe_id"]: row for row in result["probe_health"]}[probe_id]


def _run(source, manifest, lines):
    with tempfile.TemporaryDirectory() as scratch:
        return compute(source, manifest, lines.splitlines(True), scratch)


T1 = "2026-06-24T10:00:10Z"
T2 = "2026-06-24T10:00:20Z"
T3 = "2026-06-24T10:00:30Z"


class DuplicateAndCollisionTests(unittest.TestCase):
    def test_out_of_order_rows_and_identical_duplicates_are_one_sample(self):
        hi = 21000
        rows = [
            _sample("P2", T1, hi), _sample("P1", T2, hi),
            _sample("P1", T0, hi), _sample("P2", T0, hi),
            _sample("P1", T2, hi), _sample("P1", T0, hi),
            _sample("P2", T1, hi),
        ]
        manifest = _manifest(
            [_probe("P1"), _probe("P2")],
            [_limit("L1")],
            [_placement("L1", ["P1", "P2"])])
        result = _run(_source(("L1", "O1")), manifest, _lines(*rows))
        row = _loan(result, "L1")
        self.assertEqual(row["uncertain_seconds"], 0)
        self.assertEqual(row["longest_hot_seconds"], 60)
        self.assertEqual(row["longest_hot_start"], T0)

    def test_differing_readings_at_one_instant_are_a_collision(self):
        rows = [
            _sample("P1", T0, 21000),
            _sample("P1", T0, 19000),
            _sample("P2", T0, 21000),
        ]
        manifest = _manifest(
            [_probe("P1", hold=60), _probe("P2", hold=60)],
            [_limit("L1")],
            [_placement("L1", ["P1", "P2"])])
        result = _run(_source(("L1", "O1")), manifest, _lines(*rows))
        self.assertEqual(_health(result, "P1")["collision"], 60)
        self.assertEqual(_health(result, "P1")["usable"], 0)
        # Only one usable probe for the whole batch -> uncertain everywhere.
        row = _loan(result, "L1")
        self.assertEqual(row["uncertain_seconds"], 60)
        self.assertEqual(row["longest_hot_seconds"], 0)
        self.assertEqual(row["decision"], "review")

    def test_fault_plus_reading_at_one_instant_is_a_collision(self):
        rows = [
            _sample("P1", T0, 21000),
            _sample("P1", T0, None),
            _sample("P2", T0, 21000),
        ]
        manifest = _manifest(
            [_probe("P1"), _probe("P2")], [_limit("L1")],
            [_placement("L1", ["P1", "P2"])])
        result = _run(_source(("L1", "O1")), manifest, _lines(*rows))
        health = _health(result, "P1")
        self.assertEqual((health["collision"], health["fault"],
                          health["usable"]), (60, 0, 0))


class SupportTests(unittest.TestCase):
    def test_hold_expiry_and_batch_start_carry(self):
        # Last pre-batch sample 5s old when the batch opens, hold 10s.
        rows = [_sample("P1", "2026-06-24T09:59:55Z", 19000),
                _sample("P1", "2026-06-24T10:00:20Z", 19000),
                _sample("P2", "2026-06-24T09:59:50Z", 19000)]
        manifest = _manifest(
            [_probe("P1", hold=10), _probe("P2", hold=10)],
            [_limit("L1")],
            [_placement("L1", ["P1", "P2"])])
        result = _run(_source(("L1", "O1")), manifest, _lines(*rows))
        p1 = _health(result, "P1")
        # P1 supports 10:00:00-10:00:05 and 10:00:20-10:00:30; rest stale.
        self.assertEqual(p1["usable"], 15)
        self.assertEqual(p1["stale"], 45)
        # P2's only sample expired before the batch even starts.
        self.assertEqual(_health(result, "P2")["stale"], 60)
        # P1 alone is not enough evidence at any time.
        self.assertEqual(_loan(result, "L1")["uncertain_seconds"], 60)

    def test_sample_at_batch_end_is_not_counted(self):
        rows = [_sample("P1", "2026-06-24T10:01:00Z", 21000),
                _sample("P2", "2026-06-24T10:01:00Z", 21000)]
        manifest = _manifest(
            [_probe("P1"), _probe("P2")], [_limit("L1")],
            [_placement("L1", ["P1", "P2"])])
        result = _run(_source(("L1", "O1")), manifest, _lines(*rows))
        self.assertEqual(_health(result, "P1")["stale"], 60)
        self.assertEqual(_loan(result, "L1")["uncertain_seconds"], 60)

    def test_calibration_gap_and_offset_switch_split_one_reading(self):
        # One reading held 60s; calibration covers 00-10, gap 10-30,
        # second calibration 30-40, then a gap to the end.
        calibrations = [
            {"start": "2026-06-24T10:00:00Z", "end": T1,
             "offset_milli": 0},
            {"start": T3, "end": "2026-06-24T10:00:40Z",
             "offset_milli": 100},
        ]
        rows = [_sample("P1", T0, 19950), _sample("P2", T0, 19000)]
        manifest = _manifest(
            [_probe("P1", hold=60, calibrations=calibrations),
             _probe("P2", hold=60)],
            [_limit("L1", ceiling=20000)],
            [_placement("L1", ["P1", "P2"])])
        result = _run(_source(("L1", "O1")), manifest, _lines(*rows))
        p1 = _health(result, "P1")
        self.assertEqual(p1["usable"], 20)        # 00-10 and 30-40
        self.assertEqual(p1["uncalibrated"], 40)  # 10-30 and 40-60
        # :00-:10 both cool -> release-quality; :10-:30 P1 uncalibrated and
        # :30-:40 P1 hot vs P2 cool -> uncertain; :40-:60 uncalibrated.
        # 50s of insufficient evidence in total.
        row = _loan(result, "L1")
        self.assertEqual(row["uncertain_seconds"], 50)
        self.assertEqual(row["longest_hot_seconds"], 0)
        self.assertEqual(row["decision"], "review")

    def test_calibration_offset_switch_alone_turns_cool_into_hot(self):
        calibrations = [
            {"start": "2026-06-24T10:00:00Z", "end": T3,
             "offset_milli": 0},
            {"start": T3, "end": "2026-06-24T10:01:00Z",
             "offset_milli": 100},
        ]
        rows = [_sample("P1", T0, 19990), _sample("P2", T0, 19990)]
        manifest = _manifest(
            [_probe("P1", hold=60, calibrations=calibrations),
             _probe("P2", hold=60, calibrations=calibrations)],
            [_limit("L1", ceiling=20000, alarm=30)],
            [_placement("L1", ["P1", "P2"])])
        result = _run(_source(("L1", "O1")), manifest, _lines(*rows))
        row = _loan(result, "L1")
        self.assertEqual(row["longest_hot_seconds"], 30)
        self.assertEqual(row["longest_hot_start"], T3)
        self.assertEqual(row["decision"], "isolate")

    def test_each_health_class_and_the_sum(self):
        # P1: usable 00-20 then fault 20-40 then no sample -> stale 40-60.
        # P2: collision at 00 held 20s, uncalibrated reading 20-40, then
        #     a sample at 50 held to 60.
        calibrations = [{"start": "2026-06-24T10:00:40Z",
                         "end": "2026-06-25T00:00:00Z",
                         "offset_milli": 0}]
        rows = [
            _sample("P1", T0, 19000), _sample("P1", T2, None),
            _sample("P2", T0, 19000), _sample("P2", T0, 19500),
            _sample("P2", T2, 19000),
            _sample("P2", "2026-06-24T10:00:50Z", 19000),
        ]
        manifest = _manifest(
            [_probe("P1", hold=20),
             _probe("P2", hold=20, calibrations=calibrations)],
            [_limit("L1")],
            [_placement("L1", ["P1", "P2"])])
        result = _run(_source(("L1", "O1")), manifest, _lines(*rows))
        p1 = _health(result, "P1")
        self.assertEqual((p1["usable"], p1["fault"], p1["stale"]),
                         (20, 20, 20))
        p2 = _health(result, "P2")
        self.assertEqual((p2["collision"], p2["uncalibrated"],
                          p2["usable"], p2["stale"]), (20, 20, 10, 10))
        for row in result["probe_health"]:
            self.assertEqual(sum(row[key] for key in
                                 ("usable", "uncalibrated", "fault",
                                  "collision", "stale")), 60)


class EvidenceTests(unittest.TestCase):
    def _cool_batch(self, second_probe_calibrations):
        rows = [_sample("P1", T0, 19000), _sample("P2", T0, 19000)]
        manifest = _manifest(
            [_probe("P1"), _probe("P2", calibrations=second_probe_calibrations)],
            [_limit("L1")],
            [_placement("L1", ["P1", "P2"])])
        return _run(_source(("L1", "O1")), manifest, _lines(*rows))

    def test_release_turns_into_review_when_second_probe_loses_calibration(self):
        cool = [{"start": "2026-06-24T09:00:00Z",
                 "end": "2026-06-25T00:00:00Z", "offset_milli": 0}]
        released = self._cool_batch(cool)
        self.assertEqual(_loan(released, "L1")["decision"], "release")
        self.assertEqual(_loan(released, "L1")["uncertain_seconds"], 0)

        reviewed = self._cool_batch([])
        row = _loan(reviewed, "L1")
        self.assertEqual(row["decision"], "review")
        self.assertEqual(row["uncertain_seconds"], 60)
        self.assertEqual(_health(reviewed, "P2")["uncalibrated"], 60)

    def test_one_hot_one_cool_usable_probe_is_uncertain(self):
        rows = [_sample("P1", T0, 21000), _sample("P2", T0, 19000)]
        manifest = _manifest(
            [_probe("P1"), _probe("P2")], [_limit("L1")],
            [_placement("L1", ["P1", "P2"])])
        row = _loan(_run(_source(("L1", "O1")), manifest, _lines(*rows)), "L1")
        self.assertEqual((row["uncertain_seconds"], row["longest_hot_seconds"]),
                         (60, 0))
        self.assertEqual(row["decision"], "review")

    def test_ceiling_is_strict(self):
        rows = [_sample("P1", T0, 20000), _sample("P2", T0, 20000)]
        manifest = _manifest(
            [_probe("P1"), _probe("P2")], [_limit("L1", ceiling=20000)],
            [_placement("L1", ["P1", "P2"])])
        row = _loan(_run(_source(("L1", "O1")), manifest, _lines(*rows)), "L1")
        self.assertEqual(row["longest_hot_seconds"], 0)
        self.assertEqual(row["decision"], "release")


class RunTests(unittest.TestCase):
    def _two_box_manifest(self, gap=False):
        first_end = T3
        second_start = "2026-06-24T10:00:40Z" if gap else T3
        return _manifest(
            [_probe("P1", hold=10), _probe("P2", hold=10)],
            [_limit("L1", ceiling=20000, alarm=45)],
            [_placement("L1", ["P1", "P2"], T0, first_end),
             _placement("L1", ["P1", "P2"], second_start,
                        "2026-06-24T10:01:00Z")])

    def _hot_rows(self):
        rows = []
        for offset in (0, 10, 20, 30, 40, 50):
            at = f"2026-06-24T10:00:{offset:02d}Z"
            rows.append(_sample("P1", at, 21000))
            rows.append(_sample("P2", at, 21000))
        return rows

    def test_box_change_keeps_a_contiguous_hot_run(self):
        result = _run(_source(("L1", "O1")), self._two_box_manifest(),
                      _lines(*self._hot_rows()))
        row = _loan(result, "L1")
        self.assertEqual(row["uncertain_seconds"], 0)
        self.assertEqual(row["longest_hot_seconds"], 60)
        self.assertEqual(row["longest_hot_start"], T0)
        self.assertEqual(row["decision"], "isolate")

    def test_evidence_gap_truncates_the_contiguous_run(self):
        # Same two adjacent boxes, but P2 stops reporting after 10:00:20 so
        # only P1 is left from :30 to :40.  That gap splits the hot run.
        rows = [_sample("P1", at, 21000)
                for at in (T0, T1, T2, T3, "2026-06-24T10:00:40Z",
                           "2026-06-24T10:00:50Z")]
        rows += [_sample("P2", at, 21000) for at in
                 (T0, T1, T2, "2026-06-24T10:00:40Z",
                  "2026-06-24T10:00:50Z")]
        result = _run(_source(("L1", "O1")), self._two_box_manifest(),
                      _lines(*rows))
        row = _loan(result, "L1")
        self.assertEqual(row["uncertain_seconds"], 10)
        self.assertEqual(row["longest_hot_seconds"], 30)
        self.assertEqual(row["longest_hot_start"], T0)
        # Alarm 45 is not reached and evidence is missing -> review.
        self.assertEqual(row["decision"], "review")

    def test_unpacked_gap_also_interrupts(self):
        result = _run(_source(("L1", "O1")), self._two_box_manifest(gap=True),
                      _lines(*self._hot_rows()))
        row = _loan(result, "L1")
        self.assertEqual(row["monitored_seconds"], 50)
        self.assertEqual(row["uncertain_seconds"], 0)
        self.assertEqual(row["longest_hot_seconds"], 30)
        self.assertEqual(row["longest_hot_start"], T0)

    def test_uncertain_beside_confirmed_hot_still_isolates(self):
        # Both probes hot all along, but P2 has a calibration hole from :20
        # to :40, leaving only P1 usable there (insufficient evidence).  The
        # two confirmed-hot runs are 20s each; a 15s alarm isolates anyway.
        calibrations = [
            {"start": "2026-06-24T09:00:00Z", "end": T2, "offset_milli": 0},
            {"start": "2026-06-24T10:00:40Z",
             "end": "2026-06-25T00:00:00Z", "offset_milli": 0}]
        rows = []
        for offset in (0, 10, 20, 30, 40, 50):
            at = f"2026-06-24T10:00:{offset:02d}Z"
            rows.append(_sample("P1", at, 21000))
            rows.append(_sample("P2", at, 21000))
        manifest = _manifest(
            [_probe("P1", hold=10),
             _probe("P2", hold=10, calibrations=calibrations)],
            [_limit("L1", ceiling=20000, alarm=15)],
            [_placement("L1", ["P1", "P2"])])
        result = _run(_source(("L1", "O1")), manifest, _lines(*rows))
        row = _loan(result, "L1")
        self.assertEqual(row["monitored_seconds"], 60)
        self.assertEqual(row["uncertain_seconds"], 20)
        self.assertEqual(row["longest_hot_seconds"], 20)
        self.assertEqual(row["longest_hot_start"], T0)
        self.assertEqual(row["decision"], "isolate")

    def test_loan_with_no_usable_evidence_is_review(self):
        manifest = _manifest(
            [_probe("P1"), _probe("P2")], [_limit("L1")],
            [_placement("L1", ["P1", "P2"])])
        result = _run(_source(("L1", "O1")), manifest, "")
        row = _loan(result, "L1")
        self.assertEqual(row["uncertain_seconds"], 60)
        self.assertEqual(row["longest_hot_start"], None)
        self.assertEqual(row["decision"], "review")

    def test_only_participating_loans_are_reported(self):
        manifest = _manifest(
            [_probe("P1"), _probe("P2")], [_limit("L1")],
            [_placement("L1", ["P1", "P2"])])
        result = _run(_source(("L1", "O1"), ("L9", "O9")), manifest, "")
        self.assertEqual([row["loan_id"] for row in result["loans"]], ["L1"])

    def test_input_reordering_does_not_change_the_result(self):
        manifest = self._two_box_manifest()
        rows = self._hot_rows()
        base = _run(_source(("L1", "O1")), manifest, _lines(*rows))
        shuffled = copy.deepcopy(manifest)
        shuffled["probes"].reverse()
        shuffled["limits"].append(_limit("L9"))
        shuffled["limits"].pop()
        shuffled["placements"].reverse()
        rearranged = _run(_source(("L1", "O1")), shuffled,
                          _lines(*reversed(rows)))
        self.assertEqual(base, rearranged)


class ChunkingTests(unittest.TestCase):
    def test_reordering_across_sort_chunks_is_stable(self):
        import museum.heat as heat
        rows = []
        for offset in (0, 10, 20, 30, 40, 50):
            at = f"2026-06-24T10:00:{offset:02d}Z"
            rows.append(_sample("P1", at, 21000))
            rows.append(_sample("P2", at, 19000))
        manifest = _manifest(
            [_probe("P1", hold=10), _probe("P2", hold=10,
                                          calibrations=[])],
            [_limit("L1")],
            [_placement("L1", ["P1", "P2"])])
        original = heat._CHUNK_ROWS
        heat._CHUNK_ROWS = 3
        try:
            base = _run(_source(("L1", "O1")), manifest, _lines(*rows))
            shuffled = _run(_source(("L1", "O1")), manifest,
                            _lines(*reversed(rows)))
        finally:
            heat._CHUNK_ROWS = original
        self.assertEqual(base, shuffled)
        # P2 has no calibration: every second is uncalibrated, so only P1
        # is usable and no time has enough evidence.
        row = _loan(base, "L1")
        self.assertEqual(row["uncertain_seconds"], 60)
        self.assertEqual(_health(base, "P2")["uncalibrated"], 60)


class BruteForceCrossCheckTests(unittest.TestCase):
    """Compare against a second, second-by-second model on small batches."""

    @staticmethod
    def _model(source, manifest, rows):
        import calendar
        from datetime import datetime

        def parse(value):
            return calendar.timegm(
                datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").timetuple())

        def format_time(stamp):
            return datetime.utcfromtimestamp(stamp).strftime(
                "%Y-%m-%dT%H:%M:%SZ")

        start, end = parse(manifest["start"]), parse(manifest["end"])
        probes = {probe["probe_id"]: probe for probe in manifest["probes"]}
        grouped = {}
        for row in rows:
            key = (row["probe_id"], parse(row["at"]))
            grouped.setdefault(key, set()).add(row["reading_milli"])
        samples = {}
        for (probe_id, at), values in grouped.items():
            samples.setdefault(probe_id, {})[at] = (
                "collision" if len(values) > 1 else next(iter(values)))

        def offset(probe, at):
            current = None
            for cal in probes[probe]["calibrations"]:
                if parse(cal["start"]) <= at < parse(cal["end"]):
                    current = cal["offset_milli"]
            return current

        def classify(probe_id, at):
            track = samples.get(probe_id, {})
            earliest = [stamp for stamp in sorted(track) if stamp <= at]
            if not earliest:
                return "stale", None
            origin = earliest[-1]
            if at >= origin + probes[probe_id]["hold_seconds"]:
                return "stale", None
            later = [stamp for stamp in sorted(track) if stamp > origin]
            if later and at >= later[0]:
                return "stale", None
            value = track[origin]
            if value == "collision":
                return "collision", None
            if value is None:
                return "fault", None
            shift = offset(probe_id, at)
            if shift is None:
                return "uncalibrated", None
            return "usable", value + shift

        health = {}
        for probe_id in probes:
            counts = dict.fromkeys(("usable", "uncalibrated", "fault",
                                    "collision", "stale"), 0)
            for at in range(start, end):
                counts[classify(probe_id, at)[0]] += 1
            health[probe_id] = counts

        limits = {limit["loan_id"]: limit for limit in manifest["limits"]}
        by_loan = {}
        for placement in manifest["placements"]:
            by_loan.setdefault(placement["loan_id"], []).append(placement)
        loans = []
        for loan_id in sorted(by_loan):
            ceiling = limits[loan_id]["ceiling_milli"]
            states = {}
            for placement in sorted(by_loan[loan_id],
                                    key=lambda item: parse(item["start"])):
                for at in range(parse(placement["start"]),
                                parse(placement["end"])):
                    usable = [value for probe_id in placement["probe_ids"]
                              if classify(probe_id, at)[0] == "usable"
                              for value in [classify(probe_id, at)[1]]]
                    if len(usable) < 2:
                        states[at] = "u"
                    elif all(value > ceiling for value in usable):
                        states[at] = "h"
                    elif all(value <= ceiling for value in usable):
                        states[at] = "c"
                    else:
                        states[at] = "u"
            monitored = len(states)
            uncertain = sum(1 for state in states.values() if state == "u")
            longest = 0
            start_at = None
            run = 0
            preview = None
            for at in sorted(states):
                if states[at] == "h":
                    run = run + 1 if preview == at - 1 and run else 1
                    if run > longest:
                        longest = run
                        start_at = at - run + 1
                else:
                    run = 0
                preview = at
            if longest >= limits[loan_id]["alarm_seconds"]:
                decision = "isolate"
            elif uncertain:
                decision = "review"
            else:
                decision = "release"
            loans.append({
                "loan_id": loan_id, "monitored_seconds": monitored,
                "uncertain_seconds": uncertain,
                "longest_hot_seconds": longest,
                "longest_hot_start": (None if start_at is None
                                      else format_time(start_at)),
                "decision": decision})
        return {"loans": loans,
                "probe_health": [dict(probe_id=name, **health[name])
                                 for name in sorted(health)]}

    def test_random_small_batches_match_the_second_model(self):
        import random
        rng = random.Random(20260624)
        for _ in range(40):
            base = 1782295200
            span = rng.randint(20, 40)
            names = ["P0", "P1", "P2"][:rng.randint(2, 3)]
            probes = []
            for name in names:
                calibrations = []
                moment = base - rng.randint(0, 12)
                while moment < base + span:
                    length = rng.randint(1, 10)
                    calibrations.append({
                        "start": self._fmt(moment),
                        "end": self._fmt(moment + length),
                        "offset_milli": rng.choice([0, 0, 100])})
                    moment += length + rng.randint(0, 6)
                probes.append({"probe_id": name,
                               "hold_seconds": rng.choice([5, 10, 15]),
                               "calibrations": calibrations})
            loans = [{"id": f"L{i}", "object_id": f"O{i}", "borrower": "b",
                      "start": "2026-06-01", "due": "2026-06-20"}
                     for i in range(rng.randint(1, 2))]
            limits, placements = [], []
            for loan in loans:
                limits.append({"loan_id": loan["id"],
                               "ceiling_milli": rng.choice([19900, 20000]),
                               "alarm_seconds": rng.choice([5, 10, 15])})
                cursor = base + rng.randint(0, 4)
                for _ in range(rng.randint(1, 2)):
                    end_at = cursor + rng.randint(8, 15)
                    if end_at > base + span:
                        end_at = base + span
                    if end_at <= cursor:
                        break
                    placements.append({
                        "loan_id": loan["id"],
                        "start": self._fmt(cursor),
                        "end": self._fmt(end_at),
                        "probe_ids": rng.sample(
                            names, rng.randint(2, len(names)))})
                    cursor = end_at + rng.choice([0, 0, 1, 3])
            manifest = {"start": self._fmt(base),
                        "end": self._fmt(base + span), "probes": probes,
                        "limits": limits, "placements": placements}
            rows = []
            for name in names:
                at = base - rng.randint(0, 8)
                while at < base + span + rng.randint(0, 6):
                    reading = rng.choice([None, 18000, 19900, 20000, 20500])
                    rows.append({"probe_id": name, "at": self._fmt(at),
                                 "reading_milli": reading})
                    if rng.random() < 0.15:
                        rows.append({"probe_id": name, "at": self._fmt(at),
                                     "reading_milli": rng.choice(
                                         [None, 19999, 20001])})
                    at += rng.randint(1, 8)
            rng.shuffle(rows)
            source = {"as_of": "2026-06-30", "loans": loans, "events": []}
            got = _run(source, manifest, _lines(*rows))
            expected = self._model(source, manifest, rows)
            self.assertEqual(got, expected)

    @staticmethod
    def _fmt(stamp):
        from datetime import datetime
        return datetime.utcfromtimestamp(stamp).strftime(
            "%Y-%m-%dT%H:%M:%SZ")


class ValidationTests(unittest.TestCase):
    def _base(self):
        return (_source(("L1", "O1")),
                _manifest([_probe("P1"), _probe("P2")], [_limit("L1")],
                          [_placement("L1", ["P1", "P2"])]))

    def _assert_rejected(self, source, manifest, rows=""):
        with self.assertRaises(HeatError):
            _run(source, manifest, rows)

    def test_manifest_shape_and_reference_errors(self):
        source, manifest = self._base()
        broken = copy.deepcopy(manifest)
        broken["start"] = manifest["end"]
        self._assert_rejected(source, broken)

        broken = copy.deepcopy(manifest)
        broken["placements"][0]["probe_ids"] = ["P1"]
        self._assert_rejected(source, broken)

        broken = copy.deepcopy(manifest)
        broken["placements"][0]["probe_ids"] = ["P1", "P9"]
        self._assert_rejected(source, broken)

        broken = copy.deepcopy(manifest)
        broken["placements"][0]["end"] = "2026-06-24T11:00:00Z"
        self._assert_rejected(source, broken)

        broken = copy.deepcopy(manifest)
        broken["limits"].append(copy.deepcopy(broken["limits"][0]))
        self._assert_rejected(source, broken)

    def test_overlapping_placements_for_one_object_are_rejected(self):
        source = _source(("L1", "O1"), ("L2", "O1"))
        manifest = _manifest(
            [_probe("P1"), _probe("P2")],
            [_limit("L1"), _limit("L2")],
            [_placement("L1", ["P1", "P2"], T0, "2026-06-24T10:00:40Z"),
             _placement("L2", ["P1", "P2"], "2026-06-24T10:00:30Z",
                        T3 and "2026-06-24T10:01:00Z")])
        self._assert_rejected(source, manifest)

    def test_adjacent_placements_for_one_object_are_fine(self):
        source = _source(("L1", "O1"), ("L2", "O1"))
        manifest = _manifest(
            [_probe("P1"), _probe("P2")],
            [_limit("L1"), _limit("L2")],
            [_placement("L1", ["P1", "P2"], T0, T3),
             _placement("L2", ["P1", "P2"], T3, "2026-06-24T10:01:00Z")])
        result = _run(source, manifest, "")
        self.assertEqual({row["loan_id"] for row in result["loans"]},
                         {"L1", "L2"})

    def test_bad_calibration_and_boolean_values(self):
        source, manifest = self._base()
        broken = copy.deepcopy(manifest)
        broken["probes"][0]["calibrations"] = [
            {"start": T0, "end": T3, "offset_milli": 0},
            {"start": T2, "end": T3, "offset_milli": 1}]
        self._assert_rejected(source, broken)

        broken = copy.deepcopy(manifest)
        broken["probes"][0]["hold_seconds"] = True
        self._assert_rejected(source, broken)

        broken = copy.deepcopy(manifest)
        broken["limits"][0]["ceiling_milli"] = False
        self._assert_rejected(source, broken)

    def test_calibration_may_be_empty_or_have_gaps(self):
        source = _source(("L1", "O1"))
        manifest = _manifest(
            [_probe("P1", calibrations=[]), _probe("P2")],
            [_limit("L1")], [_placement("L1", ["P1", "P2"])])
        result = _run(source, manifest, "")
        self.assertEqual(_health(result, "P1")["stale"], 60)

    def test_sample_validation(self):
        source, manifest = self._base()
        bad_rows = [
            '{"probe_id":"P1","at":"2026-06-24T10:00:00Z"}\n',
            '{"probe_id":"P1","at":"2026-06-24T10:00:00Z","reading_milli":true}\n',
            '{"probe_id":"P9","at":"2026-06-24T10:00:00Z","reading_milli":1}\n',
            '{"probe_id":"P1","at":"2026-02-30T10:00:00Z","reading_milli":1}\n',
            '{"probe_id":"P1","at":"2026-06-24T10:00:00Z","reading_milli":1,"x":2}\n',
            'not json\n',
        ]
        for rows in bad_rows:
            self._assert_rejected(source, manifest, rows)

    def test_samples_outside_the_batch_are_still_validated(self):
        source, manifest = self._base()
        rows = ('{"probe_id":"P9","at":"2020-01-01T00:00:00Z",'
                '"reading_milli":1}\n')
        self._assert_rejected(source, manifest, rows)

    def test_empty_sample_stream_is_valid(self):
        source, manifest = self._base()
        result = _run(source, manifest, "")
        self.assertEqual(len(result["probe_health"]), 2)
        self.assertEqual(_loan(result, "L1")["uncertain_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
