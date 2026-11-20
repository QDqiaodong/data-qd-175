"""CLI tests for ``loan-deposit`` via independent Python subprocesses."""

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
        [sys.executable, "-m", "museum.deposit_cli", *args],
        cwd=ROOT, env=env, capture_output=True, text=True)


def _loans():
    return {"as_of": "2026-05-15",
            "loans": [
                {"id": "L1", "object_id": "OBJ-1", "borrower": "b",
                 "start": "2026-01-01", "due": "2026-12-31"},
                {"id": "L2", "object_id": "OBJ-2", "borrower": "b",
                 "start": "2026-01-01", "due": "2026-12-31"}],
            "events": []}


def _batch():
    return {
        "assessments": [
            {"loan_id": "L1", "fee_cents": 200},
            {"loan_id": "L2", "fee_cents": 0}],
        "deposits": [
            {"deposit_id": "D1", "loan_id": "L1", "payer_ref": "P1",
             "received_at": "2026-01-01T00:00:00Z", "cents": 1000},
            {"deposit_id": "D2", "loan_id": "L2", "payer_ref": "P2",
             "received_at": "2026-01-02T00:00:00Z", "cents": 800}],
        "issued_refunds": [
            {"refund_id": "R1", "deposit_id": "D1",
             "sent_at": "2026-02-01T00:00:00Z", "cents": 300}],
        "bank_paid": [
            {"bank_id": "B1", "payer_ref": "P1",
             "paid_at": "2026-02-01T06:00:00Z", "cents": 300}],
        "confirmed_links": [],
    }


class CliTests(unittest.TestCase):
    def _write(self, directory, name, data):
        path = Path(directory) / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_success_writes_result_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            loans = self._write(tmp, "loans.json", _loans())
            batch = self._write(tmp, "batch.json", _batch())
            output = Path(tmp) / "nested" / "result.json"

            completed = _run_cli("--loans", str(loans), "--batch",
                                 str(batch), "--output", str(output))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout),
                             {"bank_rows": 1, "matched": 1, "ambiguous": 0,
                              "unmatched": 0, "balances": 2,
                              "refund_suggestions": 2})
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["bank_reconciliation"][0]["refund_id"],
                             "R1")
            # L1: 1000 - 200 fee - 300 paid = 500; L2: full 800.
            self.assertEqual(result["refund_suggestions"], [
                {"loan_id": "L1", "deposit_id": "D1", "cents": 500},
                {"loan_id": "L2", "deposit_id": "D2", "cents": 800}])

    def test_output_is_create_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            loans = self._write(tmp, "loans.json", _loans())
            batch = self._write(tmp, "batch.json", _batch())
            output = Path(tmp) / "result.json"
            output.write_text("previous contents", encoding="utf-8")

            completed = _run_cli("--loans", str(loans), "--batch",
                                 str(batch), "--output", str(output))
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(completed.stderr.startswith("loan-deposit: "))
            self.assertIn("already exists", completed.stderr)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(output.read_text(encoding="utf-8"),
                             "previous contents")

    def test_invalid_batch_exits_2_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            loans = self._write(tmp, "loans.json", _loans())
            bad = _batch()
            # Refund instructions on D1 exceed its 1000-cent receipt.
            bad["issued_refunds"].append(
                {"refund_id": "R2", "deposit_id": "D1",
                 "sent_at": "2026-02-02T00:00:00Z", "cents": 701})
            batch = self._write(tmp, "batch.json", bad)
            output = Path(tmp) / "result.json"

            completed = _run_cli("--loans", str(loans), "--batch",
                                 str(batch), "--output", str(output))
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(completed.stderr.startswith("loan-deposit: "))
            self.assertIn("exceed", completed.stderr)
            self.assertEqual(completed.stdout, "")
            self.assertFalse(output.exists())

    def test_conflicting_manual_link_exits_2_and_keeps_old_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            loans = self._write(tmp, "loans.json", _loans())
            bad = _batch()
            bad["bank_paid"].append(
                {"bank_id": "B2", "payer_ref": "P1",
                 "paid_at": "2026-02-03T06:00:00Z", "cents": 300})
            bad["confirmed_links"] = [
                {"bank_id": "B1", "refund_id": "R1"},
                {"bank_id": "B2", "refund_id": "R1"}]
            batch = self._write(tmp, "batch.json", bad)
            output = Path(tmp) / "result.json"
            output.write_text("keep me", encoding="utf-8")

            completed = _run_cli("--loans", str(loans), "--batch",
                                 str(batch), "--output", str(output))
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(completed.stderr.startswith("loan-deposit: "))
            self.assertIn("confirmed link", completed.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "keep me")

    def test_missing_or_malformed_files_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            batch = self._write(tmp, "batch.json", _batch())
            output = Path(tmp) / "result.json"

            completed = _run_cli("--loans", str(missing), "--batch",
                                 str(batch), "--output", str(output))
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(completed.stderr.startswith("loan-deposit: "))
            self.assertFalse(output.exists())

            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            completed = _run_cli("--loans", str(broken), "--batch",
                                 str(batch), "--output", str(output))
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(completed.stderr.startswith("loan-deposit: "))
            self.assertFalse(output.exists())

    def test_shuffled_input_records_give_identical_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            loans = self._write(tmp, "loans.json", _loans())
            batch = self._write(tmp, "batch.json", _batch())
            first = Path(tmp) / "first.json"
            completed = _run_cli("--loans", str(loans), "--batch",
                                 str(batch), "--output", str(first))
            self.assertEqual(completed.returncode, 0, completed.stderr)

            shuffled_loans = _loans()
            shuffled_loans["loans"] = list(reversed(shuffled_loans["loans"]))
            shuffled_batch = _batch()
            for key in ("assessments", "deposits", "issued_refunds",
                        "bank_paid", "confirmed_links"):
                shuffled_batch[key] = list(reversed(shuffled_batch[key]))
            loans2 = self._write(tmp, "loans2.json", shuffled_loans)
            batch2 = self._write(tmp, "batch2.json", shuffled_batch)
            second = Path(tmp) / "second.json"
            completed = _run_cli("--loans", str(loans2), "--batch",
                                 str(batch2), "--output", str(second))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(first.read_text(encoding="utf-8")),
                             json.loads(second.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
