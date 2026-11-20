import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _run_cli(input_path, output_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run(
        [sys.executable, "-m", "museum.cli", "--input", str(input_path),
         "--output", str(output_path)],
        cwd=ROOT, env=env, capture_output=True, text=True)


class CliTests(unittest.TestCase):
    def _data(self):
        return {"as_of": "2026-05-08",
                "loans": [{"id": "L1", "object_id": "OBJ-1", "borrower": "b",
                           "start": "2026-05-01", "due": "2026-05-20"},
                          {"id": "L2", "object_id": "OBJ-1", "borrower": "b",
                           "start": "2026-05-01", "due": "2026-05-20"}],
                "events": [{"id": "d1", "loan_id": "L1", "type": "dispatch",
                            "time": "2026-05-02T09:00:00+00:00", "location": "Museum"},
                           {"id": "d2", "loan_id": "L2", "type": "dispatch",
                            "time": "2026-05-03T09:00:00+00:00", "location": "Museum"}]}

    def test_report_file_includes_object_custody_and_unchanged_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            input_path = tmp / "input.json"
            output_path = tmp / "nested" / "audit.json"
            input_path.write_text(json.dumps(self._data()))

            completed = _run_cli(input_path, output_path)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout),
                             {"exceptions": 0, "loans": 2, "overdue": 0})

            report = json.loads(output_path.read_text())
            self.assertEqual(set(report), {"loans", "overdue", "exceptions", "object_custody"})
            self.assertEqual(report["object_custody"], [
                {"object_id": "OBJ-1", "active_loan_ids": ["L1", "L2"],
                 "conflicts": [{"object_id": "OBJ-1", "new_dispatch_event_id": "d2",
                                "earlier_dispatch_event_id": "d1"}]}])

            repeated = _run_cli(input_path, output_path)
            self.assertEqual(repeated.returncode, 2)
            self.assertIn("loan-audit:", repeated.stderr)

    def test_malformed_input_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            input_path = tmp / "input.json"
            bad = self._data()
            bad["events"][0]["loan_id"] = "unknown"
            input_path.write_text(json.dumps(bad))

            completed = _run_cli(input_path, tmp / "audit.json")
            self.assertEqual(completed.returncode, 2)
            self.assertIn("unknown loan", completed.stderr)


if __name__ == "__main__":
    unittest.main()
