import json
import unittest
from pathlib import Path
from museum.core import audit


def _loan(loan_id, object_id="OBJ-1", start="2026-05-01", due="2026-05-20", borrower="b"):
    return {"id": loan_id, "object_id": object_id, "borrower": borrower,
            "start": start, "due": due}


def _event(event_id, loan_id, event_type, time, **extra):
    event = {"id": event_id, "loan_id": loan_id, "type": event_type,
             "time": time, "location": "Museum"}
    event.update(extra)
    return event


class AuditTests(unittest.TestCase):
    def test_fixture_flags_overdue_loan(self):
        result = audit(json.loads(Path("fixtures/input.json").read_text()))
        self.assertEqual(result["overdue"], 1)
        self.assertEqual(result["loans"][0]["status"], "overdue")
        self.assertEqual(result["loans"][1]["status"], "on_loan")

    def test_rejects_unknown_loan_event(self):
        with self.assertRaisesRegex(ValueError, "unknown loan"):
            audit({"as_of": "2026-01-01", "loans": [], "events": [{"id": "e", "loan_id": "x", "type": "receipt", "time": "2026-01-01T00:00:00+00:00"}]})

    def test_out_of_period_receipt_does_not_close_loan(self):
        data = {"as_of": "2026-05-15", "loans": [{"id": "l", "object_id": "o", "borrower": "b", "start": "2026-05-01", "due": "2026-05-10"}], "events": [{"id": "r", "loan_id": "l", "type": "receipt", "time": "2026-05-12T00:00:00+00:00"}]}
        result = audit(data)
        self.assertEqual(result["loans"][0]["status"], "overdue")
        self.assertEqual(result["loans"][0]["exceptions"], ["receipt_outside_loan"])


class ObjectCustodyTests(unittest.TestCase):
    def test_later_dispatch_conflicts_with_active_loan(self):
        data = {"as_of": "2026-05-06",
                "loans": [_loan("L1", due="2026-05-10"), _loan("L2")],
                "events": [_event("d1", "L1", "dispatch", "2026-05-02T09:00:00+00:00"),
                           _event("d2", "L2", "dispatch", "2026-05-05T09:00:00+00:00")]}
        result = audit(data)
        self.assertEqual([row["status"] for row in result["loans"]], ["on_loan", "on_loan"])
        self.assertEqual([row["exceptions"] for row in result["loans"]], [[], []])
        self.assertEqual(result["overdue"], 0)
        self.assertEqual(result["exceptions"], 0)
        self.assertEqual(result["object_custody"], [
            {"object_id": "OBJ-1", "active_loan_ids": ["L1", "L2"],
             "conflicts": [{"object_id": "OBJ-1", "new_dispatch_event_id": "d2",
                            "earlier_dispatch_event_id": "d1"}]}])

    def test_receipt_removes_only_its_own_loan(self):
        data = {"as_of": "2026-05-08",
                "loans": [_loan("L1", due="2026-05-10"), _loan("L2")],
                "events": [_event("d1", "L1", "dispatch", "2026-05-02T09:00:00+00:00"),
                           _event("d2", "L2", "dispatch", "2026-05-05T09:00:00+00:00"),
                           _event("r1", "L1", "receipt", "2026-05-07T09:00:00+00:00")]}
        result = audit(data)
        statuses = {row["loan_id"]: row["status"] for row in result["loans"]}
        self.assertEqual(statuses, {"L1": "returned", "L2": "on_loan"})
        entry = result["object_custody"][0]
        self.assertEqual(entry["active_loan_ids"], ["L2"])
        self.assertEqual(entry["conflicts"], [
            {"object_id": "OBJ-1", "new_dispatch_event_id": "d2",
             "earlier_dispatch_event_id": "d1"}])

    def test_early_receipt_and_condition_events_do_not_change_custody(self):
        data = {"as_of": "2026-05-08",
                "loans": [_loan("L1", due="2026-05-10")],
                "events": [_event("r1", "L1", "receipt", "2026-05-02T09:00:00+00:00"),
                           _event("d1", "L1", "dispatch", "2026-05-03T09:00:00+00:00"),
                           _event("c1", "L1", "condition", "2026-05-04T10:00:00+00:00",
                                  condition_code="stable")]}
        result = audit(data)
        row = result["loans"][0]
        self.assertEqual(row["status"], "returned")
        self.assertEqual(row["exceptions"], [])
        self.assertEqual(row["timeline"], ["r1", "d1", "c1"])
        self.assertEqual(row["condition_codes"], ["stable"])
        self.assertEqual(result["object_custody"], [
            {"object_id": "OBJ-1", "active_loan_ids": ["L1"], "conflicts": []}])

    def test_events_outside_loan_range_ignore_custody_keep_exceptions(self):
        data = {"as_of": "2026-05-15",
                "loans": [_loan("L1", due="2026-05-10"), _loan("L2")],
                "events": [_event("d0", "L1", "dispatch", "2026-04-30T09:00:00+00:00"),
                           _event("d2", "L2", "dispatch", "2026-05-02T09:00:00+00:00"),
                           _event("r0", "L1", "receipt", "2026-05-12T09:00:00+00:00")]}
        result = audit(data)
        rows = {row["loan_id"]: row for row in result["loans"]}
        self.assertEqual(rows["L1"]["status"], "overdue")
        self.assertEqual(rows["L1"]["exceptions"],
                         ["dispatch_outside_loan", "receipt_outside_loan"])
        self.assertEqual(rows["L2"]["status"], "on_loan")
        self.assertEqual(rows["L2"]["exceptions"], [])
        self.assertEqual(result["object_custody"], [
            {"object_id": "OBJ-1", "active_loan_ids": ["L2"], "conflicts": []}])

    def test_equal_timestamps_follow_event_id_order(self):
        data = {"as_of": "2026-05-08",
                "loans": [_loan("L1"), _loan("L2")],
                "events": [_event("eb", "L1", "dispatch", "2026-05-03T10:00:00+00:00"),
                           _event("ea", "L2", "dispatch", "2026-05-03T10:00:00+00:00")]}
        result = audit(data)
        self.assertEqual(result["object_custody"], [
            {"object_id": "OBJ-1", "active_loan_ids": ["L1", "L2"],
             "conflicts": [{"object_id": "OBJ-1", "new_dispatch_event_id": "eb",
                            "earlier_dispatch_event_id": "ea"}]}])

    def test_conflict_ordering_is_deterministic_with_many_active_loans(self):
        data = {"as_of": "2026-05-08",
                "loans": [_loan("L1"), _loan("L2"), _loan("L3")],
                "events": [_event("d1", "L1", "dispatch", "2026-05-02T09:00:00+00:00"),
                           _event("d2", "L2", "dispatch", "2026-05-03T09:00:00+00:00"),
                           _event("d3", "L3", "dispatch", "2026-05-04T09:00:00+00:00")]}
        result = audit(data)
        entry = result["object_custody"][0]
        self.assertEqual(entry["active_loan_ids"], ["L1", "L2", "L3"])
        self.assertEqual([(c["new_dispatch_event_id"], c["earlier_dispatch_event_id"])
                          for c in entry["conflicts"]],
                         [("d2", "d1"), ("d3", "d1"), ("d3", "d2")])

    def test_objects_without_active_loans_still_listed_with_empty_arrays(self):
        data = {"as_of": "2026-05-15",
                "loans": [_loan("L1", object_id="OBJ-2", due="2026-05-10"),
                          _loan("L2", object_id="OBJ-1")],
                "events": [_event("d1", "L1", "dispatch", "2026-05-02T09:00:00+00:00"),
                           _event("r1", "L1", "receipt", "2026-05-03T09:00:00+00:00")]}
        result = audit(data)
        self.assertEqual(result["object_custody"], [
            {"object_id": "OBJ-1", "active_loan_ids": [], "conflicts": []},
            {"object_id": "OBJ-2", "active_loan_ids": [], "conflicts": []}])

    def test_fixture_report_adds_projection_without_changing_other_sections(self):
        result = audit(json.loads(Path("fixtures/input.json").read_text()))
        self.assertEqual(result["overdue"], 1)
        self.assertEqual(result["exceptions"], 0)
        self.assertEqual([row["loan_id"] for row in result["loans"]], ["L1", "L2"])
        self.assertEqual(result["object_custody"], [
            {"object_id": "OBJ-1", "active_loan_ids": ["L1"], "conflicts": []},
            {"object_id": "OBJ-2", "active_loan_ids": ["L2"], "conflicts": []}])


class ConditionHoldViolationTests(unittest.TestCase):
    def test_hold_persists_across_loans_for_same_object(self):
        data = {"as_of": "2026-05-08",
                "loans": [_loan("L1", due="2026-05-10"), _loan("L2")],
                "events": [_event("h1", "L1", "condition", "2026-05-02T09:00:00+00:00",
                                  condition_code="conservation_hold"),
                           _event("d2", "L2", "dispatch", "2026-05-05T09:00:00+00:00")]}
        result = audit(data)
        self.assertEqual(result["condition_hold_violations"], [
            {"object_id": "OBJ-1", "dispatch_event_id": "d2", "hold_event_id": "h1"}])

    def test_clear_lifts_hold(self):
        data = {"as_of": "2026-05-08",
                "loans": [_loan("L1")],
                "events": [_event("h1", "L1", "condition", "2026-05-02T09:00:00+00:00",
                                  condition_code="conservation_hold"),
                           _event("c1", "L1", "condition", "2026-05-03T09:00:00+00:00",
                                  condition_code="conservation_clear"),
                           _event("d1", "L1", "dispatch", "2026-05-04T09:00:00+00:00")]}
        result = audit(data)
        self.assertEqual(result["condition_hold_violations"], [])

    def test_other_condition_codes_do_not_change_hold_state(self):
        data = {"as_of": "2026-05-08",
                "loans": [_loan("L1")],
                "events": [_event("h1", "L1", "condition", "2026-05-02T09:00:00+00:00",
                                  condition_code="conservation_hold"),
                           _event("c1", "L1", "condition", "2026-05-03T09:00:00+00:00",
                                  condition_code="stable"),
                           _event("d1", "L1", "dispatch", "2026-05-04T09:00:00+00:00")]}
        result = audit(data)
        self.assertEqual(result["condition_hold_violations"], [
            {"object_id": "OBJ-1", "dispatch_event_id": "d1", "hold_event_id": "h1"}])

    def test_condition_outside_loan_range_still_affects_hold(self):
        data = {"as_of": "2026-05-15",
                "loans": [_loan("L1", start="2026-05-05", due="2026-05-10")],
                "events": [_event("h1", "L1", "condition", "2026-05-01T09:00:00+00:00",
                                  condition_code="conservation_hold"),
                           _event("d1", "L1", "dispatch", "2026-05-06T09:00:00+00:00")]}
        result = audit(data)
        self.assertEqual(result["condition_hold_violations"], [
            {"object_id": "OBJ-1", "dispatch_event_id": "d1", "hold_event_id": "h1"}])
        self.assertEqual(result["loans"][0]["exceptions"], [])
        self.assertEqual(result["loans"][0]["condition_codes"], ["conservation_hold"])

    def test_invalid_dispatch_does_not_produce_violation(self):
        data = {"as_of": "2026-05-15",
                "loans": [_loan("L1", start="2026-05-05", due="2026-05-10")],
                "events": [_event("h1", "L1", "condition", "2026-05-01T09:00:00+00:00",
                                  condition_code="conservation_hold"),
                           _event("d0", "L1", "dispatch", "2026-05-02T09:00:00+00:00")]}
        result = audit(data)
        self.assertEqual(result["condition_hold_violations"], [])
        self.assertEqual(result["loans"][0]["exceptions"], ["dispatch_outside_loan"])

    def test_dispatch_before_hold_same_timestamp_is_not_violation(self):
        # d1 sorts before h1 by event ID at the same timestamp.
        data = {"as_of": "2026-05-08",
                "loans": [_loan("L1")],
                "events": [_event("d1", "L1", "dispatch", "2026-05-02T09:00:00+00:00"),
                           _event("h1", "L1", "condition", "2026-05-02T09:00:00+00:00",
                                  condition_code="conservation_hold")]}
        result = audit(data)
        self.assertEqual(result["condition_hold_violations"], [])

    def test_hold_before_dispatch_same_timestamp_is_violation(self):
        # a1 sorts before b1 by event ID at the same timestamp.
        data = {"as_of": "2026-05-08",
                "loans": [_loan("L1")],
                "events": [_event("b1", "L1", "dispatch", "2026-05-02T09:00:00+00:00"),
                           _event("a1", "L1", "condition", "2026-05-02T09:00:00+00:00",
                                  condition_code="conservation_hold")]}
        result = audit(data)
        self.assertEqual(result["condition_hold_violations"], [
            {"object_id": "OBJ-1", "dispatch_event_id": "b1", "hold_event_id": "a1"}])

    def test_clear_before_dispatch_same_timestamp_is_not_violation(self):
        data = {"as_of": "2026-05-08",
                "loans": [_loan("L1")],
                "events": [_event("h0", "L1", "condition", "2026-05-01T09:00:00+00:00",
                                  condition_code="conservation_hold"),
                           _event("c1", "L1", "condition", "2026-05-02T09:00:00+00:00",
                                  condition_code="conservation_clear"),
                           _event("d2", "L1", "dispatch", "2026-05-02T09:00:00+00:00")]}
        result = audit(data)
        self.assertEqual(result["condition_hold_violations"], [])

    def test_multiple_holds_use_latest_hold_as_source(self):
        data = {"as_of": "2026-05-08",
                "loans": [_loan("L1")],
                "events": [_event("h1", "L1", "condition", "2026-05-02T09:00:00+00:00",
                                  condition_code="conservation_hold"),
                           _event("h2", "L1", "condition", "2026-05-03T09:00:00+00:00",
                                  condition_code="conservation_hold"),
                           _event("d1", "L1", "dispatch", "2026-05-04T09:00:00+00:00")]}
        result = audit(data)
        self.assertEqual(result["condition_hold_violations"], [
            {"object_id": "OBJ-1", "dispatch_event_id": "d1", "hold_event_id": "h2"}])

    def test_hold_is_per_object_not_global(self):
        data = {"as_of": "2026-05-08",
                "loans": [_loan("L1", object_id="OBJ-1"),
                          _loan("L2", object_id="OBJ-2")],
                "events": [_event("h1", "L1", "condition", "2026-05-02T09:00:00+00:00",
                                  condition_code="conservation_hold"),
                           _event("d2", "L2", "dispatch", "2026-05-03T09:00:00+00:00"),
                           _event("d1", "L1", "dispatch", "2026-05-04T09:00:00+00:00")]}
        result = audit(data)
        self.assertEqual(result["condition_hold_violations"], [
            {"object_id": "OBJ-1", "dispatch_event_id": "d1", "hold_event_id": "h1"}])

    def test_violations_sorted_by_object_then_dispatch_time(self):
        data = {"as_of": "2026-05-08",
                "loans": [_loan("L1", object_id="OBJ-2"),
                          _loan("L2", object_id="OBJ-1"),
                          _loan("L3", object_id="OBJ-1")],
                "events": [_event("h1", "L1", "condition", "2026-05-01T09:00:00+00:00",
                                  condition_code="conservation_hold"),
                           _event("h2", "L2", "condition", "2026-05-01T09:00:00+00:00",
                                  condition_code="conservation_hold"),
                           _event("d1", "L3", "dispatch", "2026-05-03T09:00:00+00:00"),
                           _event("d2", "L2", "dispatch", "2026-05-02T09:00:00+00:00"),
                           _event("d3", "L1", "dispatch", "2026-05-04T09:00:00+00:00")]}
        result = audit(data)
        self.assertEqual(result["condition_hold_violations"], [
            {"object_id": "OBJ-1", "dispatch_event_id": "d2", "hold_event_id": "h2"},
            {"object_id": "OBJ-1", "dispatch_event_id": "d1", "hold_event_id": "h2"},
            {"object_id": "OBJ-2", "dispatch_event_id": "d3", "hold_event_id": "h1"}])

    def test_empty_violations_when_no_hold(self):
        data = {"as_of": "2026-05-08",
                "loans": [_loan("L1")],
                "events": [_event("d1", "L1", "dispatch", "2026-05-02T09:00:00+00:00")]}
        result = audit(data)
        self.assertEqual(result["condition_hold_violations"], [])

    def test_fixture_has_no_hold_violations(self):
        result = audit(json.loads(Path("fixtures/input.json").read_text()))
        self.assertEqual(result["condition_hold_violations"], [])


if __name__ == "__main__":
    unittest.main()
