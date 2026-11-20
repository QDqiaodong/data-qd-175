import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _run_cli(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run(
        [sys.executable, "-m", "museum.plan_cli", *args],
        cwd=ROOT, env=env, capture_output=True, text=True)


def _source():
    return {"as_of": "2026-01-01",
            "loans": [{"id": "L1", "object_id": "OBJ-1", "borrower": "b",
                       "start": "2026-06-01", "due": "2026-06-30"},
                      {"id": "L2", "object_id": "OBJ-2", "borrower": "b",
                       "start": "2026-06-03", "due": "2026-06-30"}],
            "events": []}


def _request():
    return {
        "hubs": ["HUB"],
        "runs": [
            {"run_id": "R1", "from_site": "HOME", "to_site": "HUB",
             "depart": "2026-06-02T09:00:00Z",
             "arrive": "2026-06-02T12:00:00Z", "capacity": 4},
            {"run_id": "R2", "from_site": "HUB", "to_site": "VENUE",
             "depart": "2026-06-02T14:00:00Z",
             "arrive": "2026-06-02T18:00:00Z", "capacity": 4},
            {"run_id": "R3", "from_site": "VENUE", "to_site": "HOME",
             "depart": "2026-06-25T09:00:00Z",
             "arrive": "2026-06-25T17:00:00Z", "capacity": 4},
            {"run_id": "R4", "from_site": "HOME", "to_site": "VENUE",
             "depart": "2026-06-03T09:00:00Z",
             "arrive": "2026-06-03T17:00:00Z", "capacity": 2},
        ],
        "batches": [
            {"batch_id": "B1", "home_site": "HOME", "venue_site": "VENUE",
             "exhibit_start": "2026-06-05T00:00:00Z",
             "exhibit_end": "2026-06-20T00:00:00Z", "priority": 1,
             "lots": [{"loan_id": "L1", "units": 2}]},
            {"batch_id": "B2", "home_site": "HOME", "venue_site": "VENUE",
             "exhibit_start": "2026-06-06T00:00:00Z",
             "exhibit_end": "2026-06-19T00:00:00Z", "priority": 5,
             "lots": [{"loan_id": "L2", "units": 2}]},
        ],
        "previous_bookings": [
            {"batch_id": "B1", "outbound": ["R4"], "return": ["R3"],
             "units": 2, "locked": False},
        ],
    }


def _expected_result():
    return {
        "bookings": [
            {"batch_id": "B1", "outbound": ["R1", "R2"], "return": ["R3"],
             "units": 2},
            {"batch_id": "B2", "outbound": ["R4"], "return": ["R3"],
             "units": 2},
        ],
        "waitlisted": [],
        "changes": [
            {"batch_id": "B1", "run_id": "R1", "delta_units": 2},
            {"batch_id": "B1", "run_id": "R2", "delta_units": 2},
            {"batch_id": "B1", "run_id": "R4", "delta_units": -2},
            {"batch_id": "B2", "run_id": "R3", "delta_units": 2},
            {"batch_id": "B2", "run_id": "R4", "delta_units": 2},
        ],
    }


class CliTests(unittest.TestCase):
    def _write(self, directory, name, data):
        path = Path(directory) / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_success_writes_result_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._write(tmp, "source.json", _source())
            request = self._write(tmp, "plan.json", _request())
            output = Path(tmp) / "nested" / "result.json"

            completed = _run_cli("--source", str(source), "--request",
                                 str(request), "--output", str(output))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout),
                             {"bookings": 2, "changes": 5, "waitlisted": 0})
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")),
                             _expected_result())

    def test_output_is_create_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._write(tmp, "source.json", _source())
            request = self._write(tmp, "plan.json", _request())
            output = Path(tmp) / "result.json"
            output.write_text("previous contents", encoding="utf-8")

            completed = _run_cli("--source", str(source), "--request",
                                 str(request), "--output", str(output))
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(completed.stderr.startswith("loan-plan: "))
            self.assertIn("already exists", completed.stderr)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(output.read_text(encoding="utf-8"),
                             "previous contents")

    def test_invalid_request_exits_2_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._write(tmp, "source.json", _source())
            bad = _request()
            bad["batches"][0]["lots"][0]["loan_id"] = "GHOST"
            request = self._write(tmp, "plan.json", bad)
            output = Path(tmp) / "result.json"

            completed = _run_cli("--source", str(source), "--request",
                                 str(request), "--output", str(output))
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(completed.stderr.startswith("loan-plan: "))
            self.assertIn("GHOST", completed.stderr)
            self.assertEqual(completed.stdout, "")
            self.assertFalse(output.exists())

    def test_infeasible_round_exits_2_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._write(tmp, "source.json", _source())
            locked = _request()
            locked["previous_bookings"] = [
                {"batch_id": "B1", "outbound": ["R_GONE"], "return": ["R3"],
                 "units": 2, "locked": True}]
            request = self._write(tmp, "plan.json", locked)
            output = Path(tmp) / "result.json"

            completed = _run_cli("--source", str(source), "--request",
                                 str(request), "--output", str(output))
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(completed.stderr.startswith("loan-plan: "))
            self.assertIn("B1", completed.stderr)
            self.assertEqual(completed.stdout, "")
            self.assertFalse(output.exists())

    def test_missing_or_malformed_files_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            request = self._write(tmp, "plan.json", _request())
            output = Path(tmp) / "result.json"

            completed = _run_cli("--source", str(missing), "--request",
                                 str(request), "--output", str(output))
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(completed.stderr.startswith("loan-plan: "))
            self.assertFalse(output.exists())

            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            completed = _run_cli("--source", str(broken), "--request",
                                 str(request), "--output", str(output))
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(completed.stderr.startswith("loan-plan: "))
            self.assertFalse(output.exists())

    def test_shuffled_input_records_give_identical_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._write(tmp, "source.json", _source())
            request = self._write(tmp, "plan.json", _request())
            first = Path(tmp) / "first.json"
            completed = _run_cli("--source", str(source), "--request",
                                 str(request), "--output", str(first))
            self.assertEqual(completed.returncode, 0, completed.stderr)

            shuffled = _request()
            for key in ("hubs", "runs", "batches", "previous_bookings"):
                shuffled[key] = list(reversed(shuffled[key]))
            shuffled_source = _source()
            shuffled_source["loans"] = list(reversed(
                shuffled_source["loans"]))
            source2 = self._write(tmp, "source2.json", shuffled_source)
            request2 = self._write(tmp, "plan2.json", shuffled)
            second = Path(tmp) / "second.json"
            completed = _run_cli("--source", str(source2), "--request",
                                 str(request2), "--output", str(second))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(first.read_text(encoding="utf-8")),
                             json.loads(second.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
