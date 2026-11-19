import json
import unittest
from pathlib import Path
from museum.core import audit


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


if __name__ == "__main__":
    unittest.main()
