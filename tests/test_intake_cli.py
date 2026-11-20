import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _run_cli(*args, env_extra=None):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "museum.intake_cli", *args],
        cwd=ROOT, env=env, capture_output=True, text=True)


def _run_audit(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run(
        [sys.executable, "-m", "museum.cli", *args],
        cwd=ROOT, env=env, capture_output=True, text=True)


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


def _edit(kind, rid, expected, fields):
    return {"kind": kind, "id": rid, "op": "edit",
            "expected": dict(expected), "fields": dict(fields)}


def _add(kind, rid, fields):
    return {"kind": kind, "id": rid, "op": "add", "fields": dict(fields)}


class IntakeCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store = self.tmp / "store"
        self._files = 0
        self.source = self._write("source", _source())

    def _write(self, name, payload):
        self._files += 1
        path = self.tmp / f"{name}-{self._files}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _cli(self, *args, env_extra=None):
        return _run_cli(*args, env_extra=env_extra)

    def _ok(self, *args, **kw):
        done = self._cli(*args, **kw)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def _fail(self, *args, **kw):
        done = self._cli(*args, **kw)
        self.assertEqual(done.returncode, 2, done.stdout)
        self.assertTrue(done.stderr.startswith("loan-intake: "), done.stderr)
        self.assertEqual(done.stdout, "")
        return done

    def _init(self):
        done = self._ok("init", "--store", str(self.store), "--source", str(self.source))
        self.assertEqual(json.loads(done.stdout), {"initialized": True})

    def _accept(self, request_id, changes, based_on=(), env_extra=None):
        request = self._write("accept", {"request_id": request_id,
                                         "based_on": list(based_on),
                                         "changes": list(changes)})
        return self._cli("accept", "--store", str(self.store),
                         "--request", str(request), env_extra=env_extra)

    def _accept_ok(self, request_id, changes, based_on=(), **kw):
        done = self._accept(request_id, changes, based_on, **kw)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def _withdraw(self, request_id, targets, env_extra=None):
        request = self._write("withdraw", {"request_id": request_id,
                                           "targets": list(targets)})
        return self._cli("withdraw", "--store", str(self.store),
                         "--request", str(request), env_extra=env_extra)

    def _withdraw_ok(self, request_id, targets, **kw):
        done = self._withdraw(request_id, targets, **kw)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def _explain(self, kind, rid):
        done = self._ok("explain", "--store", str(self.store),
                        "--kind", kind, "--id", rid)
        return json.loads(done.stdout)

    def _snapshot(self, output):
        self._ok("snapshot", "--store", str(self.store), "--output", str(output))
        return json.loads(Path(output).read_text(encoding="utf-8"))

    def test_full_lifecycle_across_processes(self):
        self._init()
        # Every command below is its own process sharing the one store.
        receipt = self._accept_ok("C1", [_edit(
            "event", "E1",
            {"time": "2026-05-02T09:00:00+00:00", "location": "Museum"},
            {"time": "2026-05-03T09:00:00+00:00", "location": "Vault"})])
        self.assertEqual(receipt, {"request_id": "C1", "status": "accepted"})
        self._accept_ok("C2", [_edit("loan", "L1", {"borrower": "City Gallery"},
                                     {"borrower": "City Gallery Annex"})],
                        based_on=["C1"])
        self._accept_ok("C3", [_edit("event", "E1",
                                     {"time": "2026-05-03T09:00:00+00:00"},
                                     {"time": "2026-05-03T09:00:00+00:00"})])

        shown = self._explain("event", "E1")
        self.assertEqual(shown["row"]["location"], "Vault")
        self.assertEqual(shown["field_sources"],
                         {"loan_id": None, "type": None, "time": "C3",
                          "location": "C1"})

        snap = self._snapshot(self.tmp / "audit-input.json")
        self.assertEqual(snap["as_of"], "2026-05-15")
        self.assertEqual([row["id"] for row in snap["loans"]], ["L1", "L2"])
        # The export feeds loan-audit directly.
        audit_out = self.tmp / "audit.json"
        done = _run_audit("--input", str(self.tmp / "audit-input.json"),
                          "--output", str(audit_out))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout),
                         {"exceptions": 0, "loans": 2, "overdue": 0})
        report = json.loads(audit_out.read_text(encoding="utf-8"))
        self.assertEqual(report["loans"][0]["borrower"], "City Gallery Annex")

        receipt = self._withdraw_ok("W1", ["C1"])
        self.assertEqual(receipt, {"request_id": "W1", "withdrawn": ["C1", "C2"]})
        shown = self._explain("event", "E1")
        self.assertEqual(shown["row"]["location"], "Museum")  # fell back to base
        self.assertIsNone(shown["field_sources"]["location"])
        self.assertEqual(shown["row"]["time"], "2026-05-03T09:00:00+00:00")
        self.assertEqual(shown["field_sources"]["time"], "C3")  # independent fix
        self.assertEqual(self._explain("loan", "L1")["row"]["borrower"],
                         "City Gallery")

    def test_out_of_interval_handover_accepted_and_flagged_by_audit(self):
        self._init()
        self._accept_ok("C1", [_add("event", "E9", {
            "loan_id": "L1", "type": "receipt",
            "time": "2026-06-02T09:00:00+00:00", "location": "Museum"})])
        snap = self._snapshot(self.tmp / "in.json")
        self.assertEqual([row["id"] for row in snap["events"]], ["E1", "E2", "E9"])
        audit_out = self.tmp / "audit.json"
        done = _run_audit("--input", str(self.tmp / "in.json"),
                          "--output", str(audit_out))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["exceptions"], 1)
        report = json.loads(audit_out.read_text(encoding="utf-8"))
        self.assertEqual(report["loans"][0]["exceptions"], ["receipt_outside_loan"])

    def test_stale_expected_and_whole_correction_rejection(self):
        self._init()
        self._accept_ok("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                     {"due": "2026-05-25"})])
        # Compiled against the pre-C1 value: rejected as a whole, and the
        # other change in the same correction is not applied either.
        done = self._accept("C2", [_edit("loan", "L1", {"due": "2026-05-20"},
                                         {"due": "2026-05-26"}),
                                   _edit("event", "E1", {"location": "Museum"},
                                         {"location": "Vault"})])
        self.assertEqual(done.returncode, 2)
        self.assertTrue(done.stderr.startswith("loan-intake: "))
        self.assertEqual(self._explain("event", "E1")["row"]["location"], "Museum")
        # The failed id is free for the corrected request.
        self._accept_ok("C2", [_edit("loan", "L1", {"due": "2026-05-25"},
                                     {"due": "2026-05-26"})])
        self.assertEqual(self._explain("loan", "L1")["row"]["due"], "2026-05-26")

    def test_conflicting_request_ids(self):
        self._init()
        self._accept_ok("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                     {"due": "2026-05-21"})])
        self._fail("accept", "--store", str(self.store), "--request",
                   str(self._write("x", {"request_id": "C1", "based_on": [],
                                         "changes": [_edit(
                                             "loan", "L1", {"due": "2026-05-21"},
                                             {"due": "2026-05-22"})]})))
        self._fail("withdraw", "--store", str(self.store), "--request",
                   str(self._write("y", {"request_id": "C1", "targets": ["C1"]})))
        # The identical retry is fine and changes nothing.
        self._accept_ok("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                     {"due": "2026-05-21"})])

    def test_reordered_request_is_the_same_business_request(self):
        self._init()
        changes = [_edit("loan", "L1", {"due": "2026-05-20"}, {"due": "2026-05-21"}),
                   _edit("event", "E1", {"location": "Museum"}, {"location": "Vault"})]
        first = self._accept_ok("C1", changes)
        again = self._accept_ok("C1", list(reversed(changes)))
        self.assertEqual(first, again)
        # Reordered withdraw targets behave the same way.
        second = self._withdraw_ok("W1", ["C1"])
        self.assertEqual(second, self._withdraw_ok("W1", ["C1"]))

    def test_crash_after_commit_is_retry_safe(self):
        self._init()
        env = {"LOAN_INTAKE_FAILPOINT": "commit.after_replace:crash"}
        # Killed after the accept commit but before the receipt was printed.
        done = self._accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                         {"due": "2026-05-21"})], env_extra=env)
        self.assertEqual(done.returncode, 3)
        self.assertEqual(done.stdout, "")
        receipt = self._accept_ok("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                               {"due": "2026-05-21"})])
        self.assertEqual(receipt, {"request_id": "C1", "status": "accepted"})
        self.assertEqual(self._explain("loan", "L1")["row"]["due"], "2026-05-21")
        # Same for a withdrawal: the receipt and the state survive together.
        done = self._withdraw("W1", ["C1"], env_extra=env)
        self.assertEqual(done.returncode, 3)
        self.assertEqual(self._withdraw_ok("W1", ["C1"]),
                         {"request_id": "W1", "withdrawn": ["C1"]})
        self.assertEqual(self._explain("loan", "L1")["row"]["due"], "2026-05-20")

    def test_crash_before_write_leaves_old_state(self):
        self._init()
        env = {"LOAN_INTAKE_FAILPOINT": "commit.before_write:crash"}
        done = self._accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                         {"due": "2026-05-21"})], env_extra=env)
        self.assertEqual(done.returncode, 3)
        self.assertEqual(self._explain("loan", "L1")["row"]["due"], "2026-05-20")
        # The crashed request id was not consumed.
        self._accept_ok("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                     {"due": "2026-05-22"})])

    def test_injected_write_failure_is_all_or_nothing(self):
        self._init()
        env = {"LOAN_INTAKE_FAILPOINT": "commit.before_write:raise"}
        done = self._accept("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                         {"due": "2026-05-21"})], env_extra=env)
        self.assertEqual(done.returncode, 2)
        self.assertTrue(done.stderr.startswith("loan-intake: "))
        self.assertEqual(done.stdout, "")
        self._accept_ok("C1", [_edit("loan", "L1", {"due": "2026-05-20"},
                                     {"due": "2026-05-21"})])

    def test_snapshot_output_is_create_only(self):
        self._init()
        out = self.tmp / "audit-input.json"
        self._snapshot(out)
        again = self._cli("snapshot", "--store", str(self.store), "--output", str(out))
        self.assertEqual(again.returncode, 2)
        self.assertTrue(again.stderr.startswith("loan-intake: "))
        self.assertEqual(again.stdout, "")
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["as_of"],
                         "2026-05-15")
        # Parent directories are created for a fresh path.
        deep = self.tmp / "deep" / "nested" / "in.json"
        self._snapshot(deep)
        self.assertTrue(deep.exists())

    def test_init_refuses_an_existing_store(self):
        self._init()
        self._fail("init", "--store", str(self.store), "--source", str(self.source))
        # And the store is untouched.
        self.assertEqual(self._snapshot(self.tmp / "in.json"), _source())

    def test_commands_require_an_existing_store(self):
        missing = self.tmp / "no-store"
        self._fail("snapshot", "--store", str(missing), "--output",
                   str(self.tmp / "x.json"))
        self._fail("explain", "--store", str(missing), "--kind", "loan", "--id", "L1")
        self._fail("accept", "--store", str(missing), "--request",
                   str(self._write("a", {"request_id": "C1", "based_on": [],
                                         "changes": [_add("loan", "L9", {
                                             "object_id": "O", "borrower": "B",
                                             "start": "2026-05-01",
                                             "due": "2026-05-02"})]})))
        self._fail("withdraw", "--store", str(missing), "--request",
                   str(self._write("w", {"request_id": "W1", "targets": ["C1"]})))

    def test_input_errors_use_prefix_and_empty_stdout(self):
        self._init()
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self._fail("accept", "--store", str(self.store), "--request", str(bad))
        self._fail("accept", "--store", str(self.store), "--request",
                   str(self.tmp / "missing.json"))
        self._fail("init", "--store", str(self.tmp / "other"), "--source", str(bad))
        self._fail("explain", "--store", str(self.store), "--kind", "loan",
                   "--id", "NOPE")
        self._fail("explain", "--store", str(self.store), "--kind", "object",
                   "--id", "L1")
        # A failed explain/snapshot creates nothing.
        self.assertFalse((self.tmp / "nope.json").exists())

    def test_added_loan_and_event_withdrawal_flow(self):
        self._init()
        self._accept_ok("C1", [_add("loan", "L9", {
            "object_id": "OBJ-9", "borrower": "New Gallery",
            "start": "2026-05-01", "due": "2026-05-30"})])
        self._accept_ok("C2", [_add("event", "E9", {
            "loan_id": "L9", "type": "dispatch",
            "time": "2026-05-02T09:00:00+00:00", "location": "Museum"})])
        # Withdrawing only the loan correction would orphan the event.
        done = self._withdraw("W1", ["C1"])
        self.assertEqual(done.returncode, 2)
        self.assertIn("E9", done.stderr)
        self._explain("loan", "L9")
        self._explain("event", "E9")
        receipt = self._withdraw_ok("W2", ["C1", "C2"])
        self.assertEqual(receipt, {"request_id": "W2", "withdrawn": ["C1", "C2"]})
        self._fail("explain", "--store", str(self.store), "--kind", "loan",
                   "--id", "L9")
        snap = self._snapshot(self.tmp / "in.json")
        self.assertEqual(snap, _source())


if __name__ == "__main__":
    unittest.main()
