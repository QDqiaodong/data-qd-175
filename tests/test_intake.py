import unittest
import tempfile
from pathlib import Path

from museum import intake
from museum.core import audit


def _source():
    return {
        "as_of": "2026-05-15",
        "loans": [
            {"id": "L1", "object_id": "OBJ-1", "borrower": "City Gallery",
             "start": "2026-05-01", "due": "2026-05-20"},
            {"id": "L2", "object_id": "OBJ-2", "borrower": "History Hall",
             "start": "2026-05-01", "due": "2026-06-30"},
        ],
        "events": [
            {"id": "E1", "loan_id": "L1", "type": "dispatch",
             "time": "2026-05-02T09:00:00+00:00", "location": "Museum"},
            {"id": "E2", "loan_id": "L2", "type": "condition",
             "time": "2026-05-03T10:00:00+00:00", "location": "History Hall",
             "condition_code": "stable"},
        ],
    }


def _accept(request_id, changes, based_on=()):
    return {"request_id": request_id, "based_on": list(based_on),
            "changes": list(changes)}


def _withdraw(request_id, targets):
    return {"request_id": request_id, "targets": list(targets)}


def _edit(kind, rid, expected, fields):
    return {"kind": kind, "id": rid, "op": "edit",
            "expected": dict(expected), "fields": dict(fields)}


def _add(kind, rid, fields):
    return {"kind": kind, "id": rid, "op": "add", "fields": dict(fields)}


def _loan_fields(**kw):
    fields = {"object_id": "OBJ-9", "borrower": "New Gallery",
              "start": "2026-05-01", "due": "2026-05-30"}
    fields.update(kw)
    return fields


def _event_fields(**kw):
    fields = {"loan_id": "L1", "type": "dispatch",
              "time": "2026-05-04T09:00:00+00:00", "location": "Museum"}
    fields.update(kw)
    return fields


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "store"
        self.store = intake.Store(self.root)
        self.addCleanup(intake.FAILPOINTS.clear)

    def init(self, source=None):
        return intake.init_store(self.store, source if source is not None else _source())

    def accept(self, request_id, changes, based_on=()):
        return intake.accept(self.store, _accept(request_id, changes, based_on))

    def accept_error(self, request_id, changes, based_on=()):
        with self.assertRaises(intake.IntakeError):
            self.accept(request_id, changes, based_on)

    def withdraw(self, request_id, targets):
        return intake.withdraw(self.store, _withdraw(request_id, targets))

    def withdraw_error(self, request_id, targets, pattern=""):
        with self.assertRaisesRegex(intake.IntakeError, pattern):
            self.withdraw(request_id, targets)

    def explain(self, kind, rid):
        return intake.explain(self.store, kind, rid)


class InitTests(_StoreCase):
    def test_init_receipt_and_snapshot_round_trip(self):
        self.assertEqual(self.init(), {"initialized": True})
        data = intake.snapshot(self.store)
        self.assertEqual(data, _source())
        audit(data)  # the export is a valid audit input as-is

    def test_init_only_on_a_fresh_store(self):
        self.init()
        with self.assertRaisesRegex(intake.IntakeError, "already exists"):
            self.init()
        # The failed second init left the store usable.
        self.assertEqual(intake.snapshot(self.store)["as_of"], "2026-05-15")

    def test_init_validates_the_source(self):
        bad_sources = []
        dup = _source()
        dup["loans"].append(dict(dup["loans"][0]))
        bad_sources.append(dup)  # duplicate loan id
        dup = _source()
        dup["events"].append(dict(dup["events"][0]))
        bad_sources.append(dup)  # duplicate event id
        bad = _source()
        bad["loans"][0]["start"] = "2026-05-21"  # start after due
        bad_sources.append(bad)
        bad = _source()
        bad["loans"][0]["due"] = "2026-02-30"  # not a real date
        bad_sources.append(bad)
        bad = _source()
        bad["events"][0]["time"] = "2026-05-02T09:00:00"  # no offset
        bad_sources.append(bad)
        bad = _source()
        bad["events"][0]["type"] = "transfer"  # unknown type
        bad_sources.append(bad)
        bad = _source()
        bad["events"][0]["loan_id"] = "LX"  # dangling reference
        bad_sources.append(bad)
        bad = _source()
        bad["loans"][0]["borrower"] = ""  # empty text
        bad_sources.append(bad)
        bad = _source()
        bad["loans"][0]["extra"] = 1  # unknown record field
        bad_sources.append(bad)
        bad = _source()
        del bad["events"][0]["location"]  # missing field
        bad_sources.append(bad)
        bad = _source()
        bad["comment"] = "hi"  # unknown top-level field
        bad_sources.append(bad)
        for source in bad_sources:
            store = intake.Store(Path(self._tmp.name) / "fresh")
            with self.assertRaises(intake.IntakeError, msg=source):
                intake.init_store(store, source)
            self.assertFalse(store.exists())

    def test_loan_and_event_may_share_an_id(self):
        source = _source()
        source["events"][0]["id"] = "L1"  # same string, different table
        self.init(source)
        self.assertEqual(self.explain("loan", "L1")["row"]["object_id"], "OBJ-1")
        self.assertEqual(self.explain("event", "L1")["row"]["loan_id"], "L1")


class AcceptValidationTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_request_shape(self):
        for mutate in (
            lambda r: r.update(comment="x"),               # unknown field
            lambda r: r.update(request_id=""),             # empty id
            lambda r: r.update(request_id=7),              # non-string id
            lambda r: r.update(based_on="C1"),             # not a list
            lambda r: r.update(based_on=["C1", "C1"]),     # duplicate basis
            lambda r: r.update(based_on=[""]),             # bad basis entry
            lambda r: r.update(changes=[]),                # empty changes
            lambda r: r.update(changes="x"),               # not a list
            lambda r: r.pop("based_on"),                   # missing field
            lambda r: r.pop("changes"),
        ):
            request = _accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                           {"due": "2026-05-21"})])
            mutate(request)
            with self.assertRaises(intake.IntakeError, msg=request):
                intake.accept(self.store, request)
        # None of the failures consumed the id.
        self.accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                 {"due": "2026-05-21"})])

    def test_change_structure(self):
        edit = _edit("loan", "L1", {"due": "2026-05-20"}, {"due": "2026-05-21"})
        bad_changes = [
            "not-an-object",
            dict(edit, kind="object"),                     # unknown kind
            dict(edit, id=""),                             # empty id
            dict(edit, op="replace"),                      # unknown op
            dict(edit, note=1),                            # unknown change field
            _edit("loan", "L1", {"due": "2026-05-20"}, {"due": "2026-05-21",
                                                        "id": "L2"}),
            _edit("loan", "L1", {"id": "L1"}, {"id": "L2"}),  # cannot edit id
            _edit("loan", "L1", {"due": "2026-05-20"}, {}),   # key sets differ
            _edit("loan", "L1", {}, {}),                   # empty key set
            _edit("loan", "L1", {"due": None}, {"due": "2026-05-21"}),
            _edit("loan", "L1", {"due": "2026-05-20"}, {"due": None}),
            _edit("loan", "L1", {"start": "2026-05-01"}, {"start": "2026-5-1"}),
            _edit("loan", "L1", {"start": "2026-05-01"}, {"start": "20260502"}),
            _edit("event", "E1", {"time": "2026-05-02T09:00:00+00:00"},
                  {"time": "2026-05-03T09:00:00"}),        # missing offset
            _edit("event", "E1", {"type": "dispatch"}, {"type": "transfer"}),
            _edit("event", "E1", {"location": "Museum"}, {"location": ""}),
            _add("event", "E9", {"loan_id": "L1", "type": "dispatch",
                                 "time": "2026-05-04T09:00:00+00:00"}),  # missing
            _add("event", "E9", _event_fields(condition_code=None)),     # null text
            _add("event", "E9", _event_fields(weight=3)),                # unknown
            _add("loan", "L9", _loan_fields(due="2026-04-30")),          # inverted
        ]
        for change in bad_changes:
            self.accept_error("C1", [change])

    def test_duplicate_change_in_one_correction(self):
        change = _edit("loan", "L1", {"due": "2026-05-20"}, {"due": "2026-05-21"})
        self.accept_error("C1", [change, dict(change)])
        # Same id in different tables is fine.
        self.accept("C1", [_add("loan", "L9", _loan_fields()),
                           _add("event", "L9", _event_fields(loan_id="L9"))])

    def test_basis_must_exist_and_be_in_effect(self):
        self.accept_error("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                       {"due": "2026-05-21"})], based_on=["C9"])
        self.accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                 {"due": "2026-05-21"})])
        self.withdraw("W1", ["C1"])
        self.accept_error("C2", [_edit("loan", "L1", {"due": "2026-05-21"},
                                       {"due": "2026-05-22"})], based_on=["C1"])

    def test_add_ids_are_never_reassigned(self):
        self.accept_error("C1", [_add("loan", "L1", _loan_fields())])   # base id
        self.accept_error("C1", [_add("event", "E1", _event_fields())])  # base id
        self.accept("C1", [_add("event", "E9", _event_fields())])
        self.accept_error("C2", [_add("event", "E9", _event_fields())])
        self.withdraw("W1", ["C1"])
        # Even after the add was withdrawn, the id stays burned.
        self.accept_error("C3", [_add("event", "E9", _event_fields())])
        self.assertEqual(intake.snapshot(self.store)["events"], _source()["events"])

    def test_edit_requires_a_current_record(self):
        self.accept_error("C1", [_edit("loan", "L9", {"due": "2026-05-20"},
                                       {"due": "2026-05-21"})])
        self.accept("C1", [_add("loan", "L9", _loan_fields())])
        self.withdraw("W1", ["C1"])
        # The record is gone again; edits of it are rejected.
        self.accept_error("C2", [_edit("loan", "L9", {"due": "2026-05-30"},
                                       {"due": "2026-06-01"})])

    def test_expected_checked_exactly_against_current_value(self):
        self.accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                 {"due": "2026-05-25"})])
        # A stale old value rejects the whole correction, other changes too.
        self.accept_error("C2", [_edit("loan", "L1", {"due": "2026-05-20"},
                                       {"due": "2026-05-26"}),
                                 _edit("event", "E1", {"location": "Museum"},
                                       {"location": "Vault"})])
        self.assertEqual(self.explain("event", "E1")["row"]["location"], "Museum")
        # The failed request id was not consumed.
        self.accept("C2", [_edit("loan", "L1", {"due": "2026-05-25"},
                                 {"due": "2026-05-26"})])
        self.assertEqual(self.explain("loan", "L1")["row"]["due"], "2026-05-26")

    def test_event_reference_checked_after_the_whole_change(self):
        # One correction may add a loan and its events, in any array order.
        self.accept("C1", [_add("event", "E9", _event_fields(loan_id="L9")),
                           _add("loan", "L9", _loan_fields())])
        self.assertEqual(self.explain("event", "E9")["row"]["loan_id"], "L9")
        # An event for a missing loan is rejected even alongside valid edits.
        self.accept_error("C2", [_add("event", "E10", _event_fields(loan_id="LX")),
                                 _edit("loan", "L1", {"due": "2026-05-20"},
                                       {"due": "2026-05-21"})])
        self.assertEqual(self.explain("loan", "L1")["row"]["due"], "2026-05-20")

    def test_out_of_interval_event_time_is_accepted(self):
        # Handover slips outside the loan interval still enter the store; the
        # audit is what flags them.
        self.accept("C1", [_add("event", "E9",
                                _event_fields(type="receipt",
                                              time="2026-06-01T09:00:00+00:00"))])
        data = intake.snapshot(self.store)
        report = audit(data)
        self.assertEqual(report["loans"][0]["exceptions"], ["receipt_outside_loan"])


class ConditionCodeTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_add_and_remove_optional_field(self):
        # E1 has no condition_code at base: expected null states that.
        self.accept_error("C1", [_edit("event", "E1", {"condition_code": "stable"},
                                       {"condition_code": "fragile"})])
        self.accept("C1", [_edit("event", "E1", {"condition_code": None},
                                 {"condition_code": "fragile"})])
        shown = self.explain("event", "E1")
        self.assertEqual(shown["row"]["condition_code"], "fragile")
        self.assertEqual(shown["field_sources"]["condition_code"], "C1")
        # Removing it again: fields null drops the field.
        self.accept("C2", [_edit("event", "E1", {"condition_code": "fragile"},
                                 {"condition_code": None})])
        shown = self.explain("event", "E1")
        self.assertNotIn("condition_code", shown["row"])
        self.assertNotIn("condition_code", shown["field_sources"])
        # Withdrawing the removal falls back to the earlier supplier.
        self.withdraw("W1", ["C2"])
        shown = self.explain("event", "E1")
        self.assertEqual(shown["row"]["condition_code"], "fragile")
        self.assertEqual(shown["field_sources"]["condition_code"], "C1")
        # Withdrawing the only supplier removes the optional field again.
        self.withdraw("W2", ["C1"])
        self.assertNotIn("condition_code", self.explain("event", "E1")["row"])

    def test_base_condition_code_survives_unrelated_withdrawals(self):
        shown = self.explain("event", "E2")
        self.assertEqual(shown["row"]["condition_code"], "stable")
        self.assertIsNone(shown["field_sources"]["condition_code"])
        self.accept("C1", [_edit("event", "E2", {"condition_code": "stable"},
                                 {"condition_code": "fragile"})])
        self.withdraw("W1", ["C1"])
        shown = self.explain("event", "E2")
        self.assertEqual(shown["row"]["condition_code"], "stable")
        self.assertIsNone(shown["field_sources"]["condition_code"])


class WithdrawalSemanticsTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_two_level_cascade_and_independent_same_value(self):
        # C1 fixes the handover time and the location of E1 in one correction.
        self.accept("C1", [_edit("event", "E1",
                                 {"time": "2026-05-02T09:00:00+00:00",
                                  "location": "Museum"},
                                 {"time": "2026-05-03T09:00:00+00:00",
                                  "location": "Vault"})])
        # Two corrections build on it, one level each.
        self.accept("C2", [_edit("loan", "L1", {"borrower": "City Gallery"},
                                 {"borrower": "City Gallery Annex"})],
                    based_on=["C1"])
        self.accept("C3", [_edit("event", "E2", {"location": "History Hall"},
                                 {"location": "History Hall Annex"})],
                    based_on=["C2"])
        # An independent registrar re-checked the same handover and wrote the
        # same time after verifying the old value: that is new evidence.
        self.accept("C4", [_edit("event", "E1",
                                 {"time": "2026-05-03T09:00:00+00:00"},
                                 {"time": "2026-05-03T09:00:00+00:00"})])
        shown = self.explain("event", "E1")
        self.assertEqual(shown["field_sources"]["time"], "C4")
        self.assertEqual(shown["field_sources"]["location"], "C1")

        receipt = self.withdraw("W1", ["C1"])
        self.assertEqual(receipt, {"request_id": "W1", "withdrawn": ["C1", "C2", "C3"]})
        shown = self.explain("event", "E1")
        # The time stays: it now rests on the independent correction C4.
        self.assertEqual(shown["row"]["time"], "2026-05-03T09:00:00+00:00")
        self.assertEqual(shown["field_sources"]["time"], "C4")
        # The location falls back to the base value (no correction behind it).
        self.assertEqual(shown["row"]["location"], "Museum")
        self.assertIsNone(shown["field_sources"]["location"])
        # The dependent corrections' fields fell back too.
        self.assertEqual(self.explain("loan", "L1")["row"]["borrower"], "City Gallery")
        self.assertEqual(self.explain("event", "E2")["row"]["location"], "History Hall")

        # Retrying the original accept credential returns its old receipt but
        # does not bring the withdrawn correction back.
        retry = self.accept("C1", [_edit("event", "E1",
                                         {"time": "2026-05-02T09:00:00+00:00",
                                          "location": "Museum"},
                                         {"time": "2026-05-03T09:00:00+00:00",
                                          "location": "Vault"})])
        self.assertEqual(retry, {"request_id": "C1", "status": "accepted"})
        shown = self.explain("event", "E1")
        self.assertEqual(shown["row"]["location"], "Museum")
        self.assertIsNone(shown["field_sources"]["location"])

    def test_withdrawal_is_permanent_for_later_same_values(self):
        self.accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                 {"due": "2026-05-25"})])
        self.withdraw("W1", ["C1"])
        # A fresh correction may state the same value again; that neither
        # revives C1 nor its dependents.
        self.accept("C2", [_edit("loan", "L1", {"due": "2026-05-20"},
                                 {"due": "2026-05-25"})])
        shown = self.explain("loan", "L1")
        self.assertEqual(shown["row"]["due"], "2026-05-25")
        self.assertEqual(shown["field_sources"]["due"], "C2")
        self.withdraw("W2", ["C2"])
        self.assertEqual(self.explain("loan", "L1")["row"]["due"], "2026-05-20")

    def test_added_loan_and_event_must_withdraw_together(self):
        self.accept("C1", [_add("loan", "L9", _loan_fields())])
        # The event correction declares no basis on C1.
        self.accept("C2", [_add("event", "E9", _event_fields(loan_id="L9"))])
        self.withdraw_error("W1", ["C1"], "E9")
        # Nothing moved: both records are still there.
        self.assertEqual(self.explain("loan", "L9")["row"]["object_id"], "OBJ-9")
        self.assertEqual(self.explain("event", "E9")["row"]["loan_id"], "L9")
        receipt = self.withdraw("W2", ["C1", "C2"])
        self.assertEqual(receipt["withdrawn"], ["C1", "C2"])
        with self.assertRaisesRegex(intake.IntakeError, "no longer exists"):
            self.explain("loan", "L9")
        with self.assertRaisesRegex(intake.IntakeError, "no longer exists"):
            self.explain("event", "E9")

    def test_withdrawal_leaving_dangling_edit_is_rejected(self):
        self.accept("C1", [_add("loan", "L9", _loan_fields())])
        self.accept("C2", [_edit("loan", "L9", {"borrower": "New Gallery"},
                                 {"borrower": "Renamed Gallery"})])
        self.withdraw_error("W1", ["C1"], "C2")
        self.assertEqual(self.explain("loan", "L9")["row"]["borrower"],
                         "Renamed Gallery")
        receipt = self.withdraw("W2", ["C1", "C2"])
        self.assertEqual(receipt["withdrawn"], ["C1", "C2"])
        with self.assertRaises(intake.IntakeError):
            self.explain("loan", "L9")

    def test_withdrawal_causing_inverted_dates_is_rejected(self):
        self.accept("C1", [_edit("loan", "L2", {"due": "2026-06-30"},
                                 {"due": "2026-09-30"})])
        # Independent correction: valid against the then-current due date.
        self.accept("C2", [_edit("loan", "L2", {"start": "2026-05-01"},
                                 {"start": "2026-08-01"})])
        # Withdrawing C1 alone would invert L2 (2026-08-01 > 2026-06-30); the
        # old-value check of C2 must not re-filter it out of the way.
        self.withdraw_error("W1", ["C1"], "L2")
        shown = self.explain("loan", "L2")
        self.assertEqual((shown["row"]["start"], shown["row"]["due"]),
                         ("2026-08-01", "2026-09-30"))
        receipt = self.withdraw("W2", ["C1", "C2"])
        self.assertEqual(receipt["withdrawn"], ["C1", "C2"])
        shown = self.explain("loan", "L2")
        self.assertEqual((shown["row"]["start"], shown["row"]["due"]),
                         ("2026-05-01", "2026-06-30"))

    def test_withdraw_validation(self):
        self.accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                 {"due": "2026-05-21"})])
        self.withdraw_error("W1", ["C9"], "unknown correction")
        self.withdraw_error("W1", [])  # empty targets
        self.withdraw_error("W1", ["C1", "C1"])  # duplicates
        request = _withdraw("W1", ["C1"])
        request["note"] = 1
        with self.assertRaises(intake.IntakeError):
            intake.withdraw(self.store, request)
        self.withdraw("W1", ["C1"])
        self.withdraw_error("W2", ["C1"], "not in effect")  # already withdrawn
        # Failed attempts consumed nothing.
        self.accept("C2", [_edit("loan", "L1", {"due": "2026-05-20"},
                                 {"due": "2026-05-22"})])
        receipt = self.withdraw("W2", ["C2"])
        self.assertEqual(receipt["withdrawn"], ["C2"])

    def test_withdrawn_set_is_sorted_and_deduplicated(self):
        self.accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                 {"due": "2026-05-21"})])
        self.accept("C10", [_edit("loan", "L1", {"due": "2026-05-21"},
                                  {"due": "2026-05-22"})], based_on=["C1"])
        self.accept("C2", [_edit("loan", "L2", {"due": "2026-06-30"},
                                 {"due": "2026-07-01"})], based_on=["C10"])
        receipt = self.withdraw("W1", ["C2", "C1"])
        self.assertEqual(receipt["withdrawn"], ["C1", "C10", "C2"])


class RequestIdTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_equivalent_retry_returns_first_receipt(self):
        changes = [_edit("loan", "L1", {"due": "2026-05-20"}, {"due": "2026-05-21"}),
                   _edit("event", "E1", {"location": "Museum"}, {"location": "Vault"})]
        first = self.accept("C1", changes)
        # Reordered arrays and object keys are the same business request.
        retry = intake.accept(
            self.store,
            {"changes": [changes[1], changes[0]], "based_on": [],
             "request_id": "C1"})
        self.assertEqual(retry, first)
        self.assertEqual(self.explain("loan", "L1")["field_sources"]["due"], "C1")

    def test_same_id_different_content_or_operation_rejected(self):
        self.accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                 {"due": "2026-05-21"})])
        self.accept_error("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                       {"due": "2026-05-22"})])
        self.accept_error("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                       {"due": "2026-05-21"})], based_on=["C1"])
        self.withdraw_error("C1", ["C1"])  # same id, other operation
        self.withdraw("W1", ["C1"])
        self.accept_error("W1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                       {"due": "2026-05-23"})])

    def test_withdraw_retry_returns_first_receipt(self):
        self.accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                 {"due": "2026-05-21"})])
        first = self.withdraw("W1", ["C1"])
        # C1 is no longer in effect, but the equivalent retry must not look.
        self.assertEqual(self.withdraw("W1", ["C1"]), first)
        self.assertEqual(intake.withdraw(self.store, _withdraw("W1", ["C1"])), first)

    def test_failed_requests_do_not_consume_ids(self):
        self.accept_error("C1", [_edit("loan", "L9", {"due": "2026-05-20"},
                                       {"due": "2026-05-21"})])
        self.accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                 {"due": "2026-05-21"})])
        self.withdraw_error("W1", ["C9"])
        self.withdraw("W1", ["C1"])


class ExplainAndSnapshotTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_explain_base_record(self):
        shown = self.explain("loan", "L1")
        self.assertEqual(shown, {"row": _source()["loans"][0],
                                 "field_sources": {"object_id": None, "borrower": None,
                                                   "start": None, "due": None}})

    def test_explain_unknown_and_gone_records_fail(self):
        with self.assertRaisesRegex(intake.IntakeError, "unknown loan"):
            self.explain("loan", "L9")
        with self.assertRaisesRegex(intake.IntakeError, "unknown event"):
            self.explain("event", "E9")
        self.accept("C1", [_add("loan", "L9", _loan_fields())])
        self.explain("loan", "L9")
        self.withdraw("W1", ["C1"])
        with self.assertRaisesRegex(intake.IntakeError, "no longer exists"):
            self.explain("loan", "L9")

    def test_explain_added_record_sources(self):
        self.accept("C1", [_add("event", "E9", _event_fields(condition_code="good"))])
        self.accept("C2", [_edit("event", "E9", {"location": "Museum"},
                                 {"location": "Depot"})])
        shown = self.explain("event", "E9")
        self.assertEqual(shown["field_sources"],
                         {"loan_id": "C1", "type": "C1", "time": "C1",
                          "location": "C2", "condition_code": "C1"})

    def test_snapshot_is_sorted_and_carries_no_correction_data(self):
        self.accept("C1", [_add("loan", "L0", _loan_fields()),
                           _add("event", "E0", _event_fields(loan_id="L0"))])
        data = intake.snapshot(self.store)
        self.assertEqual(set(data), {"as_of", "loans", "events"})
        self.assertEqual(data["as_of"], "2026-05-15")
        self.assertEqual([row["id"] for row in data["loans"]], ["L0", "L1", "L2"])
        self.assertEqual([row["id"] for row in data["events"]], ["E0", "E1", "E2"])
        for row in data["loans"]:
            self.assertEqual(list(row), ["id", "object_id", "borrower", "start", "due"])
        report = audit(data)
        self.assertEqual(report["overdue"], 0)


class DurabilityTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_injected_write_failure_is_all_or_nothing(self):
        intake.FAILPOINTS["commit.before_write"] = "raise"
        with self.assertRaises(intake.InjectedFault):
            self.accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                     {"due": "2026-05-21"})])
        intake.FAILPOINTS.clear()
        # Nothing committed and the id is free again.
        self.assertEqual(self.explain("loan", "L1")["row"]["due"], "2026-05-20")
        self.accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                 {"due": "2026-05-21"})])

    def test_failure_after_commit_keeps_request_and_receipt_together(self):
        intake.FAILPOINTS["commit.after_replace"] = "raise"
        with self.assertRaises(intake.InjectedFault):
            self.accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                     {"due": "2026-05-21"})])
        intake.FAILPOINTS.clear()
        # The commit landed before the failure: the retry returns the receipt
        # instead of re-applying or rejecting.
        receipt = self.accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                           {"due": "2026-05-21"})])
        self.assertEqual(receipt, {"request_id": "C1", "status": "accepted"})
        self.assertEqual(self.explain("loan", "L1")["row"]["due"], "2026-05-21")

    def test_withdraw_commit_failure_is_atomic(self):
        self.accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                 {"due": "2026-05-21"})])
        intake.FAILPOINTS["commit.before_write"] = "raise"
        with self.assertRaises(intake.InjectedFault):
            self.withdraw("W1", ["C1"])
        intake.FAILPOINTS.clear()
        self.assertEqual(self.explain("loan", "L1")["row"]["due"], "2026-05-21")
        self.assertEqual(self.withdraw("W1", ["C1"])["withdrawn"], ["C1"])

    def test_state_survives_reopening(self):
        self.accept("C1", [_add("loan", "L9", _loan_fields())])
        self.withdraw("W1", ["C1"])
        reopened = intake.Store(self.root)
        with self.assertRaisesRegex(intake.IntakeError, "no longer exists"):
            intake.explain(reopened, "loan", "L9")
        self.assertEqual(intake.snapshot(reopened), _source())


if __name__ == "__main__":
    unittest.main()
