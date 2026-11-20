import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from museum import share


def _source():
    """Two loans on the same object; their dispatches conflict museum-wide."""
    return {
        "as_of": "2026-05-15",
        "loans": [
            {"id": "L1", "object_id": "OBJ-1", "borrower": "City Gallery",
             "start": "2026-05-01", "due": "2026-05-20"},
            {"id": "L2", "object_id": "OBJ-1", "borrower": "History Hall",
             "start": "2026-05-01", "due": "2026-05-20"},
        ],
        "events": [
            {"id": "E1", "loan_id": "L1", "type": "dispatch",
             "time": "2026-05-02T09:00:00+00:00", "location": "Museum"},
            {"id": "E2", "loan_id": "L2", "type": "dispatch",
             "time": "2026-05-03T09:00:00+00:00", "location": "Museum"},
            {"id": "E3", "loan_id": "L1", "type": "condition",
             "time": "2026-05-04T10:00:00+00:00", "location": "City Gallery",
             "condition_code": "stable"},
        ],
    }


def _rights(change_id, collection="M1", recipient="R1", grant=(), revoke=()):
    return {"change_id": change_id, "collection_id": collection,
            "recipient": recipient, "grant": list(grant), "revoke": list(revoke)}


def _issue(release_id, collection="M1", recipient="R1", loan_ids=("L1",),
           source=None):
    return {"release_id": release_id, "collection_id": collection,
            "recipient": recipient, "loan_ids": list(loan_ids),
            "source": source if source is not None else _source()}


def _report_digest(report):
    blob = json.dumps(report, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "store"
        self.store = share.Store(self.root)
        self.addCleanup(share.FAILPOINTS.clear)

    def _grant(self, change_id, *loan_ids, **kw):
        return share.apply_rights(self.store, _rights(change_id, grant=loan_ids, **kw))

    def _revoke(self, change_id, *loan_ids, **kw):
        return share.apply_rights(self.store, _rights(change_id, revoke=loan_ids, **kw))


class RightsValidationTests(_StoreCase):
    def test_both_lists_must_exist(self):
        for key in ("grant", "revoke"):
            request = _rights("C1")
            del request[key]
            with self.assertRaises(share.ShareError):
                share.apply_rights(self.store, request)

    def test_duplicates_and_overlap_rejected(self):
        with self.assertRaises(share.ShareError):
            share.apply_rights(self.store, _rights("C1", grant=["L1", "L1"]))
        with self.assertRaises(share.ShareError):
            share.apply_rights(self.store, _rights("C1", revoke=["L2", "L2"]))
        with self.assertRaises(share.ShareError):
            share.apply_rights(self.store, _rights("C1", grant=["L1"], revoke=["L1"]))

    def test_identifiers_must_be_non_empty_strings(self):
        for key in ("change_id", "collection_id", "recipient"):
            for bad in ("", None, 7):
                request = _rights("C1", grant=["L1"])
                request[key] = bad
                with self.assertRaises(share.ShareError):
                    share.apply_rights(self.store, request)
        with self.assertRaises(share.ShareError):
            share.apply_rights(self.store, _rights("C1", grant=["L1", ""]))
        with self.assertRaises(share.ShareError):
            share.apply_rights(self.store, _rights("C1", grant=["L1", 7]))
        not_a_list = _rights("C1")
        not_a_list["grant"] = "L1"
        with self.assertRaises(share.ShareError):
            share.apply_rights(self.store, not_a_list)

    def test_unknown_fields_rejected(self):
        request = _rights("C1", grant=["L1"])
        request["comment"] = "typo field"
        with self.assertRaises(share.ShareError):
            share.apply_rights(self.store, request)

    def test_empty_lists_allowed(self):
        receipt = share.apply_rights(self.store, _rights("C1"))
        self.assertEqual(receipt, {"change_id": "C1", "collection_id": "M1",
                                   "recipient": "R1", "grant": [], "revoke": []})


class RightsSemanticsTests(_StoreCase):
    def test_receipt_sorts_ids_and_records_the_request(self):
        receipt = share.apply_rights(
            self.store, _rights("C1", grant=["L2", "L1"], revoke=["L4", "L3"]))
        self.assertEqual(receipt, {"change_id": "C1", "collection_id": "M1",
                                   "recipient": "R1", "grant": ["L1", "L2"],
                                   "revoke": ["L3", "L4"]})

    def test_same_change_id_same_request_returns_first_receipt(self):
        first = share.apply_rights(self.store, _rights("C1", grant=["L2", "L1"]))
        replay = share.apply_rights(self.store, _rights("C1", grant=["L1", "L2"]))
        self.assertEqual(replay, first)

    def test_same_change_id_different_request_rejected(self):
        share.apply_rights(self.store, _rights("C1", grant=["L1"]))
        variants = [
            _rights("C1", collection="M2", grant=["L1"]),
            _rights("C1", recipient="R2", grant=["L1"]),
            _rights("C1", grant=["L2"]),
            _rights("C1", grant=["L1"], revoke=["L9"]),
        ]
        for variant in variants:
            with self.assertRaises(share.ShareError):
                share.apply_rights(self.store, variant)

    def test_noop_revoke_records_no_revocation(self):
        share.apply_rights(self.store, _rights("C1", revoke=["L1"]))
        self.assertEqual(self.store.state["revocations"], [])

    def test_regrant_is_idempotent_and_revoke_drops_grant(self):
        self._grant("C1", "L1")
        self._grant("C2", "L1")
        self.assertEqual(self.store.state["revocations"], [])
        self._revoke("C3", "L1")
        self.assertEqual(len(self.store.state["revocations"]), 1)
        with self.assertRaises(share.ShareError):
            share.issue_release(self.store, _issue("P1", loan_ids=["L1"]))

    def test_grant_may_precede_the_loan_in_any_source(self):
        self._grant("C1", "L9")
        source = {"as_of": "2026-01-03",
                  "loans": [{"id": "L9", "object_id": "O", "borrower": "b",
                             "start": "2026-01-01", "due": "2026-01-02"}],
                  "events": []}
        receipt = share.issue_release(self.store, _issue("P1", loan_ids=["L9"],
                                                         source=source))
        self.assertEqual(receipt["loan_ids"], ["L9"])

    def test_grants_are_per_collection_and_recipient(self):
        self._grant("C1", "L1")
        with self.assertRaises(share.ShareError):
            share.issue_release(self.store, _issue("P1", collection="M2"))
        with self.assertRaises(share.ShareError):
            share.issue_release(self.store, _issue("P1", recipient="R2"))


class IssueTests(_StoreCase):
    def test_loan_ids_must_be_non_empty_unique_and_known(self):
        self._grant("C1", "L1")
        with self.assertRaises(share.ShareError):
            share.issue_release(self.store, _issue("P1", loan_ids=[]))
        with self.assertRaises(share.ShareError):
            share.issue_release(self.store, _issue("P1", loan_ids=["L1", "L1"]))
        with self.assertRaises(share.ShareError):
            share.issue_release(self.store, _issue("P1", loan_ids=["L9"]))

    def test_every_loan_must_be_authorized(self):
        self._grant("C1", "L1")
        # L2 shares the object and borrower set but is not authorized.
        with self.assertRaises(share.ShareError):
            share.issue_release(self.store, _issue("P1", loan_ids=["L1", "L2"]))
        # Nothing was consumed by the failed attempt.
        self.assertEqual(self.store.state["releases"], {})

    def test_invalid_source_record_outside_scope_still_fails(self):
        self._grant("C1", "L1")
        bad_dates = _source()
        bad_dates["loans"][1]["due"] = "2026-04-01"  # L2 broken, out of scope
        with self.assertRaises(share.ShareError):
            share.issue_release(self.store, _issue("P1", source=bad_dates))
        bad_ref = _source()
        bad_ref["events"].append({"id": "E9", "loan_id": "LX", "type": "dispatch",
                                  "time": "2026-05-03T09:00:00+00:00",
                                  "location": "Museum"})
        with self.assertRaises(share.ShareError):
            share.issue_release(self.store, _issue("P1", source=bad_ref))
        naive = _source()
        naive["events"][0]["time"] = "2026-05-02T09:00:00"  # L1 event, no offset
        with self.assertRaises(share.ShareError):
            share.issue_release(self.store, _issue("P1", source=naive))

    def test_scoped_report_matches_contract_and_hides_other_loan(self):
        self._grant("C1", "L1")
        receipt = share.issue_release(self.store, _issue("P1", loan_ids=["L1"]))
        report = self.store.state["releases"]["P1"]["report"]
        self.assertEqual(report, {
            "loans": [{"loan_id": "L1", "object_id": "OBJ-1",
                       "borrower": "City Gallery", "status": "on_loan",
                       "last_event_id": "E3", "condition_codes": ["stable"],
                       "exceptions": [], "timeline": ["E1", "E3"]}],
            "overdue": 0,
            "exceptions": 0,
            "object_custody": [{"object_id": "OBJ-1", "active_loan_ids": ["L1"],
                                "conflicts": []}],
        })
        blob = json.dumps(report, ensure_ascii=False)
        for leaked in ("L2", "E2", "History Hall"):
            self.assertNotIn(leaked, blob)
        self.assertEqual(receipt, {"release_id": "P1", "collection_id": "M1",
                                   "recipient": "R1", "loan_ids": ["L1"],
                                   "report_sha256": _report_digest(report)})

    def test_combined_report_keeps_cross_loan_conflict(self):
        self._grant("C1", "L1", "L2")
        share.issue_release(self.store, _issue("P12", loan_ids=["L1", "L2"]))
        report = self.store.state["releases"]["P12"]["report"]
        self.assertEqual(report["object_custody"], [
            {"object_id": "OBJ-1", "active_loan_ids": ["L1", "L2"],
             "conflicts": [{"object_id": "OBJ-1", "new_dispatch_event_id": "E2",
                            "earlier_dispatch_event_id": "E1"}]}])

    def test_release_id_identity_rules(self):
        self._grant("C1", "L1", "L2")
        first = share.issue_release(self.store, _issue("P1", loan_ids=["L1"]))
        self.assertEqual(share.issue_release(self.store, _issue("P1")), first)
        variants = [
            _issue("P1", collection="M2"),
            _issue("P1", recipient="R2"),
            _issue("P1", loan_ids=["L1", "L2"]),
            _issue("P1", source={**_source(), "as_of": "2026-05-16"}),
        ]
        for variant in variants:
            with self.assertRaises(share.ShareError):
                share.issue_release(self.store, variant)

    def test_source_identity_ignores_key_order_but_not_array_order(self):
        self._grant("C1", "L1")
        source = _source()
        first = share.issue_release(self.store, _issue("P1", source=source))
        shuffled = {
            "events": [
                {**({"condition_code": e["condition_code"]}
                    if "condition_code" in e else {}),
                 "location": e["location"], "time": e["time"], "type": e["type"],
                 "loan_id": e["loan_id"], "id": e["id"]}
                for e in source["events"]
            ],
            "loans": [
                {"due": loan["due"], "start": loan["start"],
                 "borrower": loan["borrower"], "object_id": loan["object_id"],
                 "id": loan["id"]}
                for loan in source["loans"]
            ],
            "as_of": source["as_of"],
        }
        self.assertEqual(
            share.issue_release(self.store, _issue("P1", source=shuffled)), first)
        reordered = dict(source)
        reordered["events"] = list(reversed(source["events"]))
        with self.assertRaises(share.ShareError):
            share.issue_release(self.store, _issue("P1", source=reordered))

    def test_body_is_frozen_against_source_changes(self):
        self._grant("C1", "L1")
        first = share.issue_release(self.store, _issue("P1", loan_ids=["L1"]))
        changed = _source()
        changed["events"].append({"id": "E4", "loan_id": "L1", "type": "receipt",
                                  "time": "2026-05-05T09:00:00+00:00",
                                  "location": "Museum"})
        second = share.issue_release(self.store, _issue("P2", loan_ids=["L1"],
                                                        source=changed))
        self.assertNotEqual(first["report_sha256"], second["report_sha256"])
        receipt, report = share.fetch_release(self.store, "P1", "R1")
        self.assertEqual(receipt["report_sha256"], first["report_sha256"])
        self.assertEqual(report["loans"][0]["status"], "on_loan")
        _, report2 = share.fetch_release(self.store, "P2", "R1")
        self.assertEqual(report2["loans"][0]["status"], "returned")


class WithdrawalScenarioTests(_StoreCase):
    def test_full_revocation_lifecycle(self):
        self._grant("C1", "L1", "L2")
        p12 = share.issue_release(self.store, _issue("P12", loan_ids=["L1", "L2"]))
        receipt, _ = share.fetch_release(self.store, "P12", "R1")
        self.assertEqual(receipt["report_sha256"], p12["report_sha256"])

        # A real revocation kills the whole combined package.
        self._revoke("C2", "L2")
        with self.assertRaises(share.ShareError):
            share.fetch_release(self.store, "P12", "R1")

        # A scoped L1-only package is still issuable and carries no L2 trace.
        share.issue_release(self.store, _issue("P1", loan_ids=["L1"]))
        _, p1_report = share.fetch_release(self.store, "P1", "R1")
        self.assertNotIn("L2", json.dumps(p1_report, ensure_ascii=False))
        self.assertEqual(p1_report["object_custody"][0]["conflicts"], [])

        # Re-granting L2 does not revive P12; new packages can be issued.
        self._grant("C3", "L2")
        with self.assertRaises(share.ShareError):
            share.fetch_release(self.store, "P12", "R1")
        share.issue_release(self.store, _issue("P2", loan_ids=["L1", "L2"]))
        share.fetch_release(self.store, "P2", "R1")

        # Retrying the old revocation change_id is a receipt-only no-op.
        replay = self._revoke("C2", "L2")
        self.assertEqual(replay["revoke"], ["L2"])
        share.fetch_release(self.store, "P2", "R1")

        # A fresh change_id revokes for real and kills P2.
        self._revoke("C4", "L2")
        with self.assertRaises(share.ShareError):
            share.fetch_release(self.store, "P2", "R1")

        # Retrying the original issue returns the first receipt but no access.
        self.assertEqual(
            share.issue_release(self.store, _issue("P12", loan_ids=["L2", "L1"])),
            p12)
        with self.assertRaises(share.ShareError):
            share.fetch_release(self.store, "P12", "R1")

        # P1 never contained L2 and stays readable throughout.
        share.fetch_release(self.store, "P1", "R1")

    def test_revocation_never_crosses_collections_or_recipients(self):
        self._grant("C1", "L1")
        self._grant("C2", "L1", collection="M2")
        self._grant("C3", "L1", recipient="R2")
        share.issue_release(self.store, _issue("PA", loan_ids=["L1"]))
        share.issue_release(self.store, _issue("PB", collection="M2",
                                               loan_ids=["L1"]))
        share.issue_release(self.store, _issue("PC", recipient="R2",
                                               loan_ids=["L1"]))
        self._revoke("C4", "L1")
        with self.assertRaises(share.ShareError):
            share.fetch_release(self.store, "PA", "R1")
        share.fetch_release(self.store, "PB", "R1")
        share.fetch_release(self.store, "PC", "R2")

    def test_fetch_identity_and_unknown_release(self):
        self._grant("C1", "L1")
        share.issue_release(self.store, _issue("P1", loan_ids=["L1"]))
        with self.assertRaises(share.ShareError):
            share.fetch_release(self.store, "P1", "R2")
        with self.assertRaises(share.ShareError):
            share.fetch_release(self.store, "NOPE", "R1")
        with self.assertRaises(share.ShareError):
            share.fetch_release(self.store, "", "R1")


class PersistenceTests(_StoreCase):
    def test_state_survives_reopen(self):
        self._grant("C1", "L1")
        share.issue_release(self.store, _issue("P1", loan_ids=["L1"]))
        receipt, report = share.fetch_release(share.Store(self.root), "P1", "R1")
        self.assertEqual(receipt["release_id"], "P1")
        self.assertEqual(report["loans"][0]["loan_id"], "L1")

    def test_failed_write_leaves_no_trace(self):
        share.FAILPOINTS["commit.before_write"] = "raise"
        with self.assertRaises(share.InjectedFault):
            share.apply_rights(self.store, _rights("C1", grant=["L1"]))
        share.FAILPOINTS.clear()
        reopened = share.Store(self.root)
        self.assertEqual(reopened.state["changes"], {})
        # The change_id was not consumed; the same request succeeds as new.
        receipt = share.apply_rights(reopened, _rights("C1", grant=["L1"]))
        self.assertEqual(receipt["grant"], ["L1"])

    def test_crash_after_commit_is_durable_and_retry_safe(self):
        share.FAILPOINTS["commit.after_replace"] = "raise"
        with self.assertRaises(share.InjectedFault):
            share.apply_rights(self.store, _rights("C1", grant=["L1"]))
        share.FAILPOINTS.clear()
        # The change committed despite the lost receipt; retry is safe.
        receipt = share.apply_rights(share.Store(self.root),
                                     _rights("C1", grant=["L1"]))
        self.assertEqual(receipt["grant"], ["L1"])

        share.FAILPOINTS["commit.after_replace"] = "raise"
        with self.assertRaises(share.InjectedFault):
            share.issue_release(self.store, _issue("P1", loan_ids=["L1"]))
        share.FAILPOINTS.clear()
        reopened = share.Store(self.root)
        # Credential and frozen body committed together; retry returns both.
        receipt = share.issue_release(reopened, _issue("P1", loan_ids=["L1"]))
        self.assertEqual(receipt["release_id"], "P1")
        share.fetch_release(reopened, "P1", "R1")

    def test_revocation_and_package_invalidation_commit_together(self):
        self._grant("C1", "L1", "L2")
        share.issue_release(self.store, _issue("P12", loan_ids=["L1", "L2"]))
        share.FAILPOINTS["commit.after_replace"] = "raise"
        with self.assertRaises(share.InjectedFault):
            share.apply_rights(self.store, _rights("C2", revoke=["L2"]))
        share.FAILPOINTS.clear()
        # The revocation and the invalidation of P12 are one atomic fact.
        with self.assertRaises(share.ShareError):
            share.fetch_release(share.Store(self.root), "P12", "R1")

        # A failed revocation write leaves the old world fully intact.
        share.FAILPOINTS["commit.before_write"] = "raise"
        with self.assertRaises(share.InjectedFault):
            share.apply_rights(self.store, _rights("C3", revoke=["L1"]))
        share.FAILPOINTS.clear()
        reopened = share.Store(self.root)
        self.assertNotIn("C3", reopened.state["changes"])
        receipt = share.issue_release(reopened, _issue("P1", loan_ids=["L1"]))
        self.assertEqual(receipt["loan_ids"], ["L1"])

    def test_corrupted_store_rejected(self):
        self.root.mkdir(parents=True)
        (self.root / share.STORE_FILENAME).write_text("{not json", encoding="utf-8")
        with self.assertRaises(share.ShareError):
            share.Store(self.root)


class WriteCreateOnlyTests(_StoreCase):
    def test_creates_parent_directories(self):
        target = Path(self._tmp.name) / "a" / "b" / "report.json"
        share.write_create_only(target, "{}")
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {})

    def test_never_overwrites_existing_file(self):
        target = Path(self._tmp.name) / "report.json"
        target.write_text("original", encoding="utf-8")
        with self.assertRaises(share.ShareError):
            share.write_create_only(target, "new")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_directory_target_fails_without_side_effects(self):
        with self.assertRaises(share.ShareError):
            share.write_create_only(Path(self._tmp.name), "x")


if __name__ == "__main__":
    unittest.main()
