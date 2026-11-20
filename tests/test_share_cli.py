import hashlib
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
        [sys.executable, "-m", "museum.share_cli", *args],
        cwd=ROOT, env=env, capture_output=True, text=True)


def _source():
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


def _digest_file(path):
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    blob = json.dumps(report, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ShareCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store = self.tmp / "store"
        self._requests = 0

    def _request(self, payload):
        self._requests += 1
        path = self.tmp / f"request-{self._requests}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _rights(self, payload, env_extra=None):
        return _run_cli("rights", "--store", str(self.store),
                        "--request", str(self._request(payload)),
                        env_extra=env_extra)

    def _issue(self, payload, env_extra=None):
        return _run_cli("issue", "--store", str(self.store),
                        "--request", str(self._request(payload)),
                        env_extra=env_extra)

    def _fetch(self, release_id, recipient, output):
        return _run_cli("fetch", "--store", str(self.store),
                        "--release-id", release_id, "--recipient", recipient,
                        "--output", str(output))

    def _grant(self, change_id, *loan_ids, **kw):
        done = self._rights(_rights(change_id, grant=loan_ids, **kw))
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def _issue_ok(self, release_id, **kw):
        done = self._issue(_issue(release_id, **kw))
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def _fetch_ok(self, release_id, recipient, output):
        done = self._fetch(release_id, recipient, output)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def _fetch_denied(self, release_id, recipient, output):
        done = self._fetch(release_id, recipient, output)
        self.assertEqual(done.returncode, 2)
        self.assertTrue(done.stderr.startswith("loan-share: "), done.stderr)
        self.assertEqual(done.stdout, "")
        self.assertFalse(Path(output).exists())

    def test_full_scenario_across_processes(self):
        # Grant both loans of the shared object and issue the combined package.
        receipt = self._grant("C1", "L2", "L1")
        self.assertEqual(receipt, {"change_id": "C1", "collection_id": "M1",
                                   "recipient": "R1", "grant": ["L1", "L2"],
                                   "revoke": []})
        p12 = self._issue_ok("P12", loan_ids=["L2", "L1"])
        self.assertEqual(p12["loan_ids"], ["L1", "L2"])
        self.assertNotIn("report", p12)  # issue prints only the receipt

        out = self.tmp / "p12.json"
        echoed = self._fetch_ok("P12", "R1", out)
        self.assertEqual(echoed, {"release_id": "P12",
                                  "report_sha256": p12["report_sha256"]})
        self.assertEqual(_digest_file(out), p12["report_sha256"])
        self.assertEqual(json.loads(out.read_text())["object_custody"][0]["conflicts"],
                         [{"object_id": "OBJ-1", "new_dispatch_event_id": "E2",
                           "earlier_dispatch_event_id": "E1"}])

        # Revoking L2 withdraws the whole combined package.
        self._grant("C2", revoke=["L2"])
        self._fetch_denied("P12", "R1", self.tmp / "p12-again.json")

        # A scoped L1-only package is issuable and free of L2 traces.
        p1 = self._issue_ok("P1", loan_ids=["L1"])
        out1 = self.tmp / "p1.json"
        self._fetch_ok("P1", "R1", out1)
        body = out1.read_text(encoding="utf-8")
        for leaked in ("L2", "E2", "History Hall"):
            self.assertNotIn(leaked, body)
        self.assertEqual(json.loads(body)["object_custody"],
                         [{"object_id": "OBJ-1", "active_loan_ids": ["L1"],
                           "conflicts": []}])
        self.assertEqual(_digest_file(out1), p1["report_sha256"])

        # Re-granting L2 does not revive P12; a new package works.
        self._grant("C3", "L2")
        self._fetch_denied("P12", "R1", self.tmp / "p12-b.json")
        self._issue_ok("P2", loan_ids=["L1", "L2"])
        self._fetch_ok("P2", "R1", self.tmp / "p2.json")

        # Retrying the old revocation change_id must not revoke the new grant.
        done = self._rights(_rights("C2", revoke=["L2"]))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["revoke"], ["L2"])
        self._fetch_ok("P2", "R1", self.tmp / "p2-b.json")

        # A fresh change_id revokes for real and kills the new package.
        self._grant("C4", revoke=["L2"])
        self._fetch_denied("P2", "R1", self.tmp / "p2-c.json")

        # Retrying the original issue returns the first receipt, not access.
        done = self._issue(_issue("P12", loan_ids=["L1", "L2"]))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), p12)
        self._fetch_denied("P12", "R1", self.tmp / "p12-c.json")

        # P1 never contained L2 and stays readable throughout.
        self._fetch_ok("P1", "R1", self.tmp / "p1-b.json")

    def test_collections_and_recipients_are_isolated(self):
        self._grant("C1", "L1")
        # Same loan id in another collection or for another recipient is not
        # authorized by the M1/R1 grant.
        done = self._issue(_issue("PX", collection="M2", loan_ids=["L1"]))
        self.assertEqual(done.returncode, 2)
        done = self._issue(_issue("PX", recipient="R2", loan_ids=["L1"]))
        self.assertEqual(done.returncode, 2)
        self._grant("C2", "L1", collection="M2")
        self._grant("C3", "L1", recipient="R2")
        self._issue_ok("PA", loan_ids=["L1"])
        self._issue_ok("PB", collection="M2", loan_ids=["L1"])
        self._issue_ok("PC", recipient="R2", loan_ids=["L1"])
        # Revocation in M1/R1 leaves the other collection/recipient alone.
        self._grant("C4", revoke=["L1"])
        self._fetch_denied("PA", "R1", self.tmp / "pa.json")
        self._fetch_ok("PB", "R1", self.tmp / "pb.json")
        self._fetch_ok("PC", "R2", self.tmp / "pc.json")
        # Identity mismatch and unknown ids fail without output.
        self._fetch_denied("PB", "R2", self.tmp / "pb-wrong.json")
        self._fetch_denied("NOPE", "R1", self.tmp / "nope.json")

    def test_conflicting_credentials_rejected(self):
        self._grant("C1", "L1")
        done = self._rights(_rights("C1", grant=["L2"]))
        self.assertEqual(done.returncode, 2)
        self.assertTrue(done.stderr.startswith("loan-share: "))
        self.assertEqual(done.stdout, "")
        # The identical retry is fine.
        done = self._rights(_rights("C1", grant=["L1"]))
        self.assertEqual(done.returncode, 0, done.stderr)

        p1 = self._issue_ok("P1", loan_ids=["L1"])
        changed = _source()
        changed["as_of"] = "2026-05-16"
        done = self._issue(_issue("P1", loan_ids=["L1"], source=changed))
        self.assertEqual(done.returncode, 2)
        done = self._issue(_issue("P1", loan_ids=["L1", "L2"]))
        self.assertEqual(done.returncode, 2)
        done = self._issue(_issue("P1", loan_ids=["L1"]))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), p1)

    def test_out_of_scope_source_error_rejected(self):
        self._grant("C1", "L1")
        bad = _source()
        bad["loans"][1]["due"] = "2026-04-01"  # broken L2, outside the scope
        done = self._issue(_issue("P1", loan_ids=["L1"], source=bad))
        self.assertEqual(done.returncode, 2)
        self.assertTrue(done.stderr.startswith("loan-share: "))
        self.assertEqual(done.stdout, "")
        # The failed attempt consumed nothing.
        self._issue_ok("P1", loan_ids=["L1"])

    def test_source_change_cannot_alter_issued_package(self):
        self._grant("C1", "L1")
        p1 = self._issue_ok("P1", loan_ids=["L1"])
        changed = _source()
        changed["events"].append({"id": "E4", "loan_id": "L1", "type": "receipt",
                                  "time": "2026-05-05T09:00:00+00:00",
                                  "location": "Museum"})
        p2 = self._issue_ok("P2", loan_ids=["L1"], source=changed)
        self.assertNotEqual(p1["report_sha256"], p2["report_sha256"])
        out = self.tmp / "p1.json"
        echoed = self._fetch_ok("P1", "R1", out)
        self.assertEqual(echoed["report_sha256"], p1["report_sha256"])
        self.assertEqual(json.loads(out.read_text())["loans"][0]["status"],
                         "on_loan")

    def test_existing_output_is_protected(self):
        self._grant("C1", "L1")
        self._issue_ok("P1", loan_ids=["L1"])
        out = self.tmp / "report.json"
        out.write_text("keep me", encoding="utf-8")
        done = self._fetch("P1", "R1", out)
        self.assertEqual(done.returncode, 2)
        self.assertTrue(done.stderr.startswith("loan-share: "))
        self.assertEqual(done.stdout, "")
        self.assertEqual(out.read_text(encoding="utf-8"), "keep me")
        # Fetching does not mutate the store on failure either.
        self._fetch_ok("P1", "R1", self.tmp / "other.json")

    def test_fetch_creates_parent_directories(self):
        self._grant("C1", "L1")
        self._issue_ok("P1", loan_ids=["L1"])
        out = self.tmp / "deep" / "nested" / "report.json"
        self._fetch_ok("P1", "R1", out)
        self.assertTrue(out.exists())

    def test_input_errors_use_prefix_and_empty_stdout(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        done = _run_cli("rights", "--store", str(self.store), "--request", str(bad))
        self.assertEqual(done.returncode, 2)
        self.assertTrue(done.stderr.startswith("loan-share: "))
        self.assertEqual(done.stdout, "")

        done = _run_cli("rights", "--store", str(self.store),
                        "--request", str(self.tmp / "missing.json"))
        self.assertEqual(done.returncode, 2)
        self.assertTrue(done.stderr.startswith("loan-share: "))

        shape = self._request({"change_id": "C1", "collection_id": "M1",
                               "recipient": "R1", "grant": ["L1"]})
        done = _run_cli("rights", "--store", str(self.store), "--request",
                        str(shape))
        self.assertEqual(done.returncode, 2)
        self.assertTrue(done.stderr.startswith("loan-share: "))

        done = _run_cli("fetch", "--store", str(self.store), "--release-id", "",
                        "--recipient", "R1", "--output", str(self.tmp / "x.json"))
        self.assertEqual(done.returncode, 2)
        self.assertTrue(done.stderr.startswith("loan-share: "))

    def test_crash_after_commit_is_retry_safe(self):
        env = {"LOAN_SHARE_FAILPOINT": "commit.after_replace:crash"}
        # Killed after the rights commit but before the receipt was printed.
        done = self._rights(_rights("C1", grant=["L1"]), env_extra=env)
        self.assertEqual(done.returncode, 3)
        self.assertEqual(done.stdout, "")
        done = self._rights(_rights("C1", grant=["L1"]))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["grant"], ["L1"])

        # Killed after the issue commit: credential and body survive together.
        done = self._issue(_issue("P1", loan_ids=["L1"]), env_extra=env)
        self.assertEqual(done.returncode, 3)
        done = self._issue(_issue("P1", loan_ids=["L1"]))
        self.assertEqual(done.returncode, 0, done.stderr)
        receipt = json.loads(done.stdout)
        out = self.tmp / "p1.json"
        echoed = self._fetch_ok("P1", "R1", out)
        self.assertEqual(echoed["report_sha256"], receipt["report_sha256"])
        self.assertEqual(_digest_file(out), receipt["report_sha256"])

        # Killed during a revocation: the package is dead after restart.
        self._grant("C2", "L2")
        self._issue_ok("P12", loan_ids=["L1", "L2"])
        done = self._rights(_rights("C3", revoke=["L2"]), env_extra=env)
        self.assertEqual(done.returncode, 3)
        self._fetch_denied("P12", "R1", self.tmp / "p12.json")
        # The revocation is not replayed when the receipt is retried.
        self._grant("C4", "L2")
        self._issue_ok("P2", loan_ids=["L2"])
        done = self._rights(_rights("C3", revoke=["L2"]))
        self.assertEqual(done.returncode, 0, done.stderr)
        self._fetch_ok("P2", "R1", self.tmp / "p2.json")

    def test_crash_before_write_leaves_old_state(self):
        self._grant("C1", "L1")
        env = {"LOAN_SHARE_FAILPOINT": "commit.before_write:crash"}
        done = self._rights(_rights("C2", revoke=["L1"]), env_extra=env)
        self.assertEqual(done.returncode, 3)
        # The revocation never happened: the grant still authorizes issues.
        self._issue_ok("P1", loan_ids=["L1"])
        # And the crashed change_id was not consumed.
        done = self._rights(_rights("C2", grant=["L2"]))
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_injected_write_failure_is_all_or_nothing(self):
        env = {"LOAN_SHARE_FAILPOINT": "commit.before_write:raise"}
        done = self._rights(_rights("C1", grant=["L1"]), env_extra=env)
        self.assertEqual(done.returncode, 2)
        self.assertTrue(done.stderr.startswith("loan-share: "))
        self.assertEqual(done.stdout, "")
        done = self._rights(_rights("C1", grant=["L1"]))
        self.assertEqual(done.returncode, 0, done.stderr)

        done = self._issue(_issue("P1", loan_ids=["L1"]), env_extra=env)
        self.assertEqual(done.returncode, 2)
        done = self._issue(_issue("P1", loan_ids=["L1"]))
        self.assertEqual(done.returncode, 0, done.stderr)
        self._fetch_ok("P1", "R1", self.tmp / "p1.json")

    def test_restart_recovery_between_processes(self):
        # Each command is a separate process; the store carries the state.
        self._grant("C1", "L1")
        self._issue_ok("P1", loan_ids=["L1"])
        self._fetch_ok("P1", "R1", self.tmp / "p1.json")
        self.assertTrue((self.store / "store.json").exists())


if __name__ == "__main__":
    unittest.main()
