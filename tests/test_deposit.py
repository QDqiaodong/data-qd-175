"""Library tests for the ``loan-deposit`` reconciliation and settlement logic."""

import copy
import random
import unittest

from museum.deposit import DepositError, reconcile


def _loans(*loan_ids):
    # Full audit-style loan records: only ``id`` may drive finance facts.
    return {"as_of": "2026-05-15",
            "loans": [{"id": loan_id, "object_id": f"OBJ-{loan_id}",
                       "borrower": "b", "start": "2026-01-01",
                       "due": "2026-12-31"} for loan_id in loan_ids],
            "events": []}


def _assessment(loan_id, fee_cents=0):
    return {"loan_id": loan_id, "fee_cents": fee_cents}


def _deposit(deposit_id, loan_id, payer_ref, received_at, cents):
    return {"deposit_id": deposit_id, "loan_id": loan_id,
            "payer_ref": payer_ref, "received_at": received_at,
            "cents": cents}


def _refund(refund_id, deposit_id, sent_at, cents):
    return {"refund_id": refund_id, "deposit_id": deposit_id,
            "sent_at": sent_at, "cents": cents}


def _bank(bank_id, payer_ref, paid_at, cents):
    return {"bank_id": bank_id, "payer_ref": payer_ref,
            "paid_at": paid_at, "cents": cents}


def _link(bank_id, refund_id):
    return {"bank_id": bank_id, "refund_id": refund_id}


def _batch(assessments=(), deposits=(), issued_refunds=(), bank_paid=(),
           confirmed_links=()):
    return {"assessments": list(assessments), "deposits": list(deposits),
            "issued_refunds": list(issued_refunds),
            "bank_paid": list(bank_paid),
            "confirmed_links": list(confirmed_links)}


def _row(result, bank_id):
    rows = {row["bank_id"]: row for row in result["bank_reconciliation"]}
    return rows[bank_id]


def _balance(result, loan_id):
    rows = {row["loan_id"]: row for row in result["balances"]}
    return rows[loan_id]


def _shuffled(doc, rng):
    """A deep copy with every list field reordered (loans doc or batch)."""
    copy_doc = copy.deepcopy(doc)
    for key, value in list(copy_doc.items()):
        if isinstance(value, list):
            rng.shuffle(copy_doc[key])
    return copy_doc


class ReconciliationMatchingTests(unittest.TestCase):
    def _batch(self):
        # R1 is the only instruction B1 can settle; B2 can see R1 and R2.
        # A greedy look at B2 first would steal R1 and mis-settle B1.
        return _batch(
            assessments=[_assessment("L1"), _assessment("L2")],
            deposits=[
                _deposit("D1", "L1", "P", "2026-01-01T00:00:00Z", 1000),
                _deposit("D2", "L2", "P", "2026-01-02T00:00:00Z", 1000),
            ],
            issued_refunds=[
                _refund("R1", "D1", "2026-02-01T00:00:00Z", 300),
                _refund("R2", "D2", "2026-02-02T00:00:00Z", 300),
            ],
            bank_paid=[
                # 12h after R1 and 12h before R2: only R1 qualifies.
                _bank("B1", "P", "2026-02-01T12:00:00Z", 300),
                # 33h after R2, still 57h after R1: both instructions qualify.
                _bank("B2", "P", "2026-02-03T09:00:00Z", 300),
            ])

    def test_global_maximum_confirms_both_rows(self):
        result = reconcile(_loans("L1", "L2"), self._batch())
        # B1 has the single surface candidate R1; B2 can see both, yet the
        # only maximum explanations settle B1->R1 and therefore B2->R2.
        self.assertEqual(_row(result, "B1")["status"], "matched")
        self.assertEqual(_row(result, "B1")["refund_id"], "R1")
        self.assertEqual(_row(result, "B2")["status"], "matched")
        self.assertEqual(_row(result, "B2")["refund_id"], "R2")
        for row in result["bank_reconciliation"]:
            self.assertFalse(row["can_be_unmatched"])

    def test_48h_window_is_inclusive_at_both_ends(self):
        batch = self._batch()
        # Exactly 48h after R2 is sent (R2 sent 2026-02-02T00:00): the edge
        # is still admissible.
        batch["bank_paid"][1]["paid_at"] = "2026-02-04T00:00:00Z"
        result = reconcile(_loans("L1", "L2"), batch)
        self.assertEqual(_row(result, "B2")["refund_id"], "R2")
        # One second later R2 leaves B2's surface candidates and R1's window
        # has already closed (R1 sent 2026-02-01T00:00), so B2 is unmatched;
        # B1 keeps its unique instruction.
        batch["bank_paid"][1]["paid_at"] = "2026-02-04T00:00:01Z"
        result = reconcile(_loans("L1", "L2"), batch)
        self.assertEqual(_row(result, "B2")["status"], "unmatched")
        self.assertEqual(_row(result, "B2")["possible_refund_ids"], [])
        self.assertTrue(_row(result, "B2")["can_be_unmatched"])
        self.assertEqual(_row(result, "B1")["refund_id"], "R1")
        # The lower bound is inclusive too: paying exactly when the
        # instruction was sent still qualifies.
        batch["bank_paid"][1]["paid_at"] = "2026-02-02T00:00:00Z"
        result = reconcile(_loans("L1", "L2"), batch)
        self.assertIn(
            "R2", _row(result, "B2")["possible_refund_ids"])

    def test_single_surface_candidate_can_stay_ambiguous_when_contested(self):
        # Two banks, one shared instruction: each has exactly one surface
        # candidate, yet neither may be auto-confirmed.
        batch = _batch(
            assessments=[_assessment("L1")],
            deposits=[_deposit("D1", "L1", "P",
                               "2026-01-01T00:00:00Z", 1000)],
            issued_refunds=[_refund("R1", "D1",
                                    "2026-02-01T00:00:00Z", 200)],
            bank_paid=[
                _bank("B1", "P", "2026-02-01T01:00:00Z", 200),
                _bank("B2", "P", "2026-02-01T02:00:00Z", 200),
            ])
        result = reconcile(_loans("L1"), batch)
        for bank_id in ("B1", "B2"):
            row = _row(result, bank_id)
            self.assertEqual(row["status"], "ambiguous")
            self.assertEqual(row["possible_refund_ids"], ["R1"])
            self.assertTrue(row["can_be_unmatched"])

    def test_manual_link_confirms_payment_outside_window(self):
        batch = self._batch()
        # 100h after R2 was sent: automatic matching cannot see it, but
        # finance holds extra evidence and confirms the link manually.
        batch["bank_paid"][1]["paid_at"] = "2026-02-06T04:00:00Z"
        batch["confirmed_links"] = [_link("B2", "R2")]
        result = reconcile(_loans("L1", "L2"), batch)
        self.assertEqual(_row(result, "B2")["status"], "matched")
        self.assertEqual(_row(result, "B2")["refund_id"], "R2")
        self.assertEqual(_row(result, "B2")["possible_refund_ids"], ["R2"])
        self.assertFalse(_row(result, "B2")["can_be_unmatched"])
        self.assertEqual(_balance(result, "L2")["paid_cents"], 300)

    def test_reordered_inputs_give_identical_output(self):
        loans = _loans("L1", "L2")
        batch = self._batch()
        first = reconcile(loans, batch)
        for seed in range(5):
            rng = random.Random(20260515 + seed)
            shuffled_loans = _shuffled(loans, random.Random(seed + 1))
            shuffled_batch = _shuffled(batch, rng)
            self.assertEqual(reconcile(shuffled_loans, shuffled_batch), first)


class MatchingGroundTruthTests(unittest.TestCase):
    """Dulmage-Mendelsohn classification checked against full enumeration."""

    @staticmethod
    def _brute_force(edges):
        """All maximum matchings; edges maps bank id -> refund id list."""
        bank_ids = sorted(edges)
        best_size = -1
        explanations = []

        def search(pos, used, assignment):
            nonlocal best_size, explanations
            if pos == len(bank_ids):
                size = len(assignment)
                if size > best_size:
                    best_size, explanations = size, [dict(assignment)]
                elif size == best_size:
                    explanations.append(dict(assignment))
                return
            bank_id = bank_ids[pos]
            search(pos + 1, used, assignment)
            for refund_id in edges[bank_id]:
                if refund_id in used:
                    continue
                used.add(refund_id)
                assignment[bank_id] = refund_id
                search(pos + 1, used, assignment)
                del assignment[bank_id]
                used.discard(refund_id)

        search(0, set(), {})
        possibilities = {bank_id: set() for bank_id in bank_ids}
        emptyable = {bank_id: False for bank_id in bank_ids}
        for explanation in explanations:
            for bank_id in bank_ids:
                if bank_id in explanation:
                    possibilities[bank_id].add(explanation[bank_id])
                else:
                    emptyable[bank_id] = True
        return possibilities, emptyable

    def test_random_graphs_match_brute_force(self):
        from museum.deposit import _analyze_candidates
        rng = random.Random(424242)
        refunds = [f"R{i}" for i in range(6)]
        for _ in range(1000):
            edges = {}
            for b in range(5):
                degree = rng.randrange(0, 4)
                edges[f"B{b}"] = sorted(
                    rng.sample(refunds, degree)) if degree else []
            truth_possible, truth_empty = self._brute_force(edges)
            matched, possible, emptyable = _analyze_candidates(edges)
            for bank_id in edges:
                self.assertEqual(possible[bank_id],
                                 truth_possible[bank_id], edges)
                self.assertEqual(emptyable[bank_id],
                                 truth_empty[bank_id], edges)
                if len(truth_possible[bank_id]) == 1 and \
                        not truth_empty[bank_id]:
                    self.assertEqual(
                        matched.get(bank_id),
                        next(iter(truth_possible[bank_id])), edges)
                else:
                    self.assertNotIn(bank_id, matched)


class InterchangeableRowsTests(unittest.TestCase):
    def _two_loan_batch(self, link=None):
        batch = _batch(
            assessments=[_assessment("L1"), _assessment("L2")],
            deposits=[
                _deposit("D1", "L1", "P", "2026-01-01T00:00:00Z", 1000),
                _deposit("D2", "L2", "P", "2026-01-02T00:00:00Z", 1000),
            ],
            issued_refunds=[
                _refund("R1", "D1", "2026-02-01T00:00:00Z", 400),
                _refund("R2", "D2", "2026-02-01T00:00:00Z", 400),
            ],
            bank_paid=[
                _bank("B1", "P", "2026-02-01T08:00:00Z", 400),
                _bank("B2", "P", "2026-02-01T09:00:00Z", 400),
            ])
        if link is not None:
            batch["confirmed_links"] = [link]
        return batch

    def test_swapable_rows_block_suggestions_until_manual_confirmation(self):
        loans = _loans("L1", "L2")
        result = reconcile(loans, self._two_loan_batch())
        for bank_id in ("B1", "B2"):
            row = _row(result, bank_id)
            self.assertEqual(row["status"], "ambiguous")
            self.assertIsNone(row["refund_id"])
            self.assertEqual(row["possible_refund_ids"], ["R1", "R2"])
            # Both banks are matched in every maximum explanation.
            self.assertFalse(row["can_be_unmatched"])
        # Both loans keep unused deposits, but the ambiguous payer blocks any
        # new refund advice.
        self.assertEqual(result["refund_suggestions"], [])
        for loan_id in ("L1", "L2"):
            self.assertEqual(_balance(result, loan_id)["blocking_bank_ids"],
                             ["B1", "B2"])

        # Finance verifies one row; the other is then forced as well.
        confirmed = reconcile(
            loans, self._two_loan_batch(link=_link("B1", "R1")))
        self.assertEqual(_row(confirmed, "B1")["refund_id"], "R1")
        self.assertEqual(_row(confirmed, "B2")["status"], "matched")
        self.assertEqual(_row(confirmed, "B2")["refund_id"], "R2")
        for loan_id in ("L1", "L2"):
            self.assertEqual(
                _balance(confirmed, loan_id)["blocking_bank_ids"], [])
        suggestions = confirmed["refund_suggestions"]
        # Each loan had 1000 in, 400 confirmed out: 600 remains per deposit.
        self.assertEqual(suggestions, [
            {"loan_id": "L1", "deposit_id": "D1", "cents": 600},
            {"loan_id": "L2", "deposit_id": "D2", "cents": 600},
        ])

    def test_two_banks_competing_for_one_instruction_stay_ambiguous(self):
        batch = _batch(
            assessments=[_assessment("L1")],
            deposits=[_deposit("D1", "L1", "P",
                               "2026-01-01T00:00:00Z", 1000)],
            issued_refunds=[_refund("R1", "D1",
                                    "2026-02-01T00:00:00Z", 250)],
            bank_paid=[
                _bank("B1", "P", "2026-02-01T01:00:00Z", 250),
                _bank("B2", "P", "2026-02-01T02:00:00Z", 250),
            ])
        result = reconcile(_loans("L1"), batch)
        for bank_id in ("B1", "B2"):
            row = _row(result, bank_id)
            self.assertEqual(row["status"], "ambiguous")
            self.assertEqual(row["possible_refund_ids"], ["R1"])
            # A maximum explanation settles only one of the two rows.
            self.assertTrue(row["can_be_unmatched"])
        self.assertEqual(result["refund_suggestions"], [])
        self.assertEqual(_balance(result, "L1")["blocking_bank_ids"],
                         ["B1", "B2"])
        # The instruction is never confirmed, so its money stays reserved.
        self.assertEqual(_balance(result, "L1")["reserved_cents"], 250)
        self.assertEqual(_balance(result, "L1")["paid_cents"], 0)

    def test_forty_interchangeable_pairs_do_not_lose_any_row(self):
        n = 40
        loans = _loans(*(f"L{i:02d}" for i in range(n)))
        deposits = [
            _deposit(f"D{i:02d}", f"L{i:02d}", "P",
                     f"2026-01-{(i % 28) + 1:02d}T00:00:00Z", 1000)
            for i in range(n)]
        refunds = [
            _refund(f"R{i:02d}", f"D{i:02d}",
                    "2026-03-01T00:00:00Z", 500) for i in range(n)]
        banks = [
            _bank(f"B{i:02d}", "P",
                  f"2026-03-01T{(i % 24):02d}:00:00Z", 500)
            for i in range(n)]
        result = reconcile(loans, _batch(
            assessments=[_assessment(f"L{i:02d}") for i in range(n)],
            deposits=deposits, issued_refunds=refunds, bank_paid=banks))
        self.assertEqual(len(result["bank_reconciliation"]), n)
        for row in result["bank_reconciliation"]:
            self.assertEqual(row["status"], "ambiguous")
            self.assertEqual(len(row["possible_refund_ids"]), n)
            self.assertFalse(row["can_be_unmatched"])
        # Every loan is blocked; no suggestions despite the free remainder.
        self.assertEqual(result["refund_suggestions"], [])
        for balance in result["balances"]:
            self.assertEqual(len(balance["blocking_bank_ids"]), n)
            self.assertEqual(balance["reserved_cents"], 500)
            self.assertEqual(balance["paid_cents"], 0)
        # And under shuffled input, every one of the forty rows survives.
        rng = random.Random(7)
        shuffled = reconcile(_shuffled(loans, random.Random(8)),
                             _shuffled(_batch(
                                 assessments=[_assessment(f"L{i:02d}")
                                              for i in range(n)],
                                 deposits=deposits, issued_refunds=refunds,
                                 bank_paid=banks), rng))
        self.assertEqual(
            sorted(row["bank_id"] for row in shuffled["bank_reconciliation"]),
            [f"B{i:02d}" for i in range(n)])
        self.assertEqual(shuffled, result)


class SettlementTests(unittest.TestCase):
    def _batch(self):
        # L1: fee was raised above the cash left after a confirmed refund;
        # an in-transit instruction makes the potential shortfall even worse.
        # L2: a healthy loan paid through two deposits, split suggestions.
        # L3: a different payer with an unexplainable bank row of its own.
        return _batch(
            assessments=[_assessment("L1", 800), _assessment("L2", 100),
                         _assessment("L3", 0)],
            deposits=[
                _deposit("D1a", "L1", "PBAD",
                         "2026-01-01T00:00:00Z", 1000),
                _deposit("D2a", "L2", "PGOOD",
                         "2026-01-01T00:00:00Z", 700),
                _deposit("D2b", "L2", "PGOOD",
                         "2026-01-05T00:00:00Z", 600),
                _deposit("D3", "L3", "POTHER",
                         "2026-01-06T00:00:00Z", 500),
            ],
            issued_refunds=[
                _refund("R1PAID", "D1a", "2026-02-01T00:00:00Z", 300),
                _refund("R1TRANSIT", "D1a", "2026-02-10T00:00:00Z", 200),
            ],
            bank_paid=[
                _bank("BOK", "PBAD", "2026-02-01T05:00:00Z", 300),
                _bank("BUNK", "PBAD", "2026-02-20T05:00:00Z", 150),
                _bank("BFOREIGN", "POTHER",
                      "2026-02-21T05:00:00Z", 999),
                # No deposit anywhere names NORef: the row is retained but
                # cannot block any of the settled partners.
                _bank("BNODEPOSIT", "NOREF",
                      "2026-02-22T05:00:00Z", 700),
            ])

    def test_top_up_excess_reserve_and_split_suggestions(self):
        result = reconcile(_loans("L1", "L2", "L3"), self._batch())
        l1 = _balance(result, "L1")
        # deposited 1000, confirmed 300 -> 700 cash remains vs fee 800:
        # 100 top-up.  The 200 in transit deepens the exposure by 200, but
        # must not be counted as cash already short.
        self.assertEqual(l1["deposited_cents"], 1000)
        self.assertEqual(l1["fee_cents"], 800)
        self.assertEqual(l1["paid_cents"], 300)
        self.assertEqual(l1["reserved_cents"], 200)
        self.assertEqual(l1["top_up_cents"], 100)
        self.assertEqual(l1["excess_reserved_cents"], 200)
        self.assertEqual(l1["blocking_bank_ids"], ["BUNK"])
        # Blocked loan: no suggestion even though an instruction is in flight.
        self.assertNotIn(
            "L1", [s["loan_id"] for s in result["refund_suggestions"]])

        l2 = _balance(result, "L2")
        # 1300 in, fee 100, nothing out -> 1200 refundable across the two
        # deposits in receipt order, capped by each deposit's own remainder.
        self.assertEqual(l2["top_up_cents"], 0)
        self.assertEqual(l2["excess_reserved_cents"], 0)
        self.assertEqual(l2["blocking_bank_ids"], [])
        self.assertEqual(result["refund_suggestions"], [
            {"loan_id": "L2", "deposit_id": "D2a", "cents": 700},
            {"loan_id": "L2", "deposit_id": "D2b", "cents": 500},
        ])

        l3 = _balance(result, "L3")
        # L3 itself receives from POTHER, so that payer's unexplained row
        # blocks L3; the row still stays in the reconciliation and does not
        # touch the other partners.
        self.assertEqual(l3["blocking_bank_ids"], ["BFOREIGN"])
        self.assertEqual(_row(result, "BFOREIGN")["status"], "unmatched")
        # The NOREF row has no deposit behind it: retained, but it never
        # blocks a loan because no loan's payer is NOREF.
        self.assertEqual(_row(result, "BNODEPOSIT")["status"], "unmatched")
        for balance in result["balances"]:
            self.assertNotIn("BNODEPOSIT", balance["blocking_bank_ids"])
        self.assertNotIn(
            "L3", [s["loan_id"] for s in result["refund_suggestions"]])
        # L2 uses a different payer, so the foreign rows never block it.
        self.assertEqual(
            [s["loan_id"] for s in result["refund_suggestions"]],
            ["L2", "L2"])

    def test_fee_above_deposit_and_zero_fee_edges(self):
        batch = _batch(
            assessments=[_assessment("L1", 1500), _assessment("L2", 0)],
            deposits=[
                _deposit("D1", "L1", "P1",
                         "2026-01-01T00:00:00Z", 1000),
                _deposit("D2", "L2", "P2",
                         "2026-01-01T00:00:00Z", 400),
            ])
        result = reconcile(_loans("L1", "L2"), batch)
        # Cash left after receipts: only 1000 against a 1500 fee, so the
        # missing 500 is the top-up (not the whole fee).
        self.assertEqual(_balance(result, "L1")["top_up_cents"], 500)
        self.assertEqual(_balance(result, "L1")["excess_reserved_cents"], 0)
        self.assertEqual(result["refund_suggestions"], [
            {"loan_id": "L2", "deposit_id": "D2", "cents": 400}])

    def test_suggestion_respects_per_deposit_remainder_and_tie_order(self):
        batch = _batch(
            assessments=[_assessment("L1", 100)],
            deposits=[
                _deposit("D1", "L1", "P",
                         "2026-01-01T00:00:00Z", 500),
                _deposit("D2", "L1", "P",
                         "2026-01-01T00:00:00Z", 500),
            ],
            issued_refunds=[_refund("R1", "D1",
                                    "2026-02-01T00:00:00Z", 450)])
        result = reconcile(_loans("L1"), batch)
        # 1000 - 100 fee - 450 reserved = 450 to suggest.  D1 only has 50
        # free; the tie in received time is broken by deposit_id, so D1 is
        # drained first and D2 carries the rest.
        self.assertEqual(result["refund_suggestions"], [
            {"loan_id": "L1", "deposit_id": "D1", "cents": 50},
            {"loan_id": "L1", "deposit_id": "D2", "cents": 400},
        ])

    def test_no_refund_suggestion_when_nothing_is_refundable(self):
        batch = _batch(
            assessments=[_assessment("L1", 1000)],
            deposits=[_deposit("D1", "L1", "P",
                               "2026-01-01T00:00:00Z", 1000)])
        result = reconcile(_loans("L1"), batch)
        self.assertEqual(result["refund_suggestions"], [])
        self.assertEqual(_balance(result, "L1")["top_up_cents"], 0)


class ValidationTests(unittest.TestCase):
    def _minimal_batch(self):
        return _batch(
            assessments=[_assessment("L1")],
            deposits=[_deposit("D1", "L1", "P",
                               "2026-01-01T00:00:00Z", 1000)])

    def assert_rejected(self, loans, batch, fragment):
        with self.assertRaisesRegex(DepositError, fragment):
            reconcile(loans, batch)

    def test_duplicate_loan_id_rejected(self):
        self.assert_rejected(
            {"loans": [{"id": "L1"}, {"id": "L1"}]},
            self._minimal_batch(), "duplicates loan L1")

    def test_missing_fields_and_bad_types_are_named(self):
        loans = _loans("L1")
        batch = self._minimal_batch()
        batch["deposits"][0].pop("cents")
        self.assert_rejected(loans, batch, r"deposits\[0\]\.cents")
        batch = self._minimal_batch()
        batch["deposits"][0]["cents"] = True
        self.assert_rejected(loans, batch, r"deposits\[0\]\.cents")
        batch = self._minimal_batch()
        batch["deposits"][0]["cents"] = 0
        self.assert_rejected(loans, batch, "positive integer")
        batch = self._minimal_batch()
        batch["assessments"][0]["fee_cents"] = -1
        self.assert_rejected(loans, batch, "non-negative")
        batch = self._minimal_batch()
        batch["deposits"][0]["payer_ref"] = ""
        self.assert_rejected(loans, batch, "payer_ref")
        batch = self._minimal_batch()
        batch["deposits"][0]["received_at"] = "2026-13-01T00:00:00Z"
        self.assert_rejected(loans, batch, "valid calendar time")

    def test_duplicate_primary_keys_rejected(self):
        loans = _loans("L1")
        batch = self._minimal_batch()
        batch["deposits"].append(_deposit("D1", "L1", "P",
                                          "2026-01-02T00:00:00Z", 10))
        self.assert_rejected(loans, batch, "duplicates deposit D1")
        batch = self._minimal_batch()
        batch["assessments"].append(_assessment("L1", 10))
        self.assert_rejected(loans, batch, "duplicates the assessment")
        batch = self._minimal_batch()
        batch["bank_paid"] = [_bank("B1", "P", "2026-02-01T00:00:00Z", 10),
                              _bank("B1", "P", "2026-02-02T00:00:00Z", 10)]
        self.assert_rejected(loans, batch, "duplicates bank row B1")

    def test_unknown_references_rejected(self):
        loans = _loans("L1")
        batch = self._minimal_batch()
        batch["assessments"] = [_assessment("GHOST")]
        self.assert_rejected(loans, batch, "unknown loan GHOST")
        batch = self._minimal_batch()
        batch["deposits"][0]["loan_id"] = "L2"
        self.assert_rejected(loans, batch, "has no assessment")
        batch = self._minimal_batch()
        batch["issued_refunds"] = [_refund("R1", "GHOST",
                                           "2026-02-01T00:00:00Z", 10)]
        self.assert_rejected(loans, batch, "unknown deposit GHOST")
        batch = self._minimal_batch()
        batch["confirmed_links"] = [_link("GHOST", "R1")]
        self.assert_rejected(loans, batch, "unknown bank row GHOST")

    def test_refund_before_deposit_and_over_receipt_rejected(self):
        loans = _loans("L1")
        batch = self._minimal_batch()
        batch["issued_refunds"] = [
            _refund("R1", "D1", "2025-12-31T23:59:59Z", 10)]
        self.assert_rejected(loans, batch, "sent before its deposit")
        batch = self._minimal_batch()
        batch["issued_refunds"] = [
            _refund("R1", "D1", "2026-02-01T00:00:00Z", 600),
            _refund("R2", "D1", "2026-02-02T00:00:00Z", 401)]
        self.assert_rejected(loans, batch, "exceed")

    def test_conflicting_manual_links_rejected(self):
        loans = _loans("L1")
        batch = self._minimal_batch()
        batch["issued_refunds"] = [
            _refund("R1", "D1", "2026-02-01T00:00:00Z", 100),
            _refund("R2", "D1", "2026-02-02T00:00:00Z", 100)]
        batch["bank_paid"] = [
            _bank("B1", "P", "2026-02-03T00:00:00Z", 100),
            _bank("B2", "P", "2026-02-03T00:00:00Z", 100)]
        batch["confirmed_links"] = [_link("B1", "R1"), _link("B1", "R2")]
        self.assert_rejected(loans, batch, "more than one confirmed link")
        batch["confirmed_links"] = [_link("B1", "R1"), _link("B2", "R1")]
        self.assert_rejected(loans, batch, "more than one confirmed link")

    def test_manual_link_must_share_payer_amount_and_timing(self):
        loans = _loans("L1", "L2")
        base = _batch(
            assessments=[_assessment("L1"), _assessment("L2")],
            deposits=[
                _deposit("D1", "L1", "P1",
                         "2026-01-01T00:00:00Z", 1000),
                _deposit("D2", "L2", "P2",
                         "2026-01-01T00:00:00Z", 1000)],
            issued_refunds=[_refund("R1", "D1",
                                    "2026-02-01T00:00:00Z", 100)],
            bank_paid=[_bank("B1", "P2",
                             "2026-02-02T00:00:00Z", 100)])
        base["confirmed_links"] = [_link("B1", "R1")]
        self.assert_rejected(loans, base, "does not match deposit")
        base["bank_paid"][0]["payer_ref"] = "P1"
        base["bank_paid"][0]["cents"] = 101
        self.assert_rejected(loans, base, "does not match instruction")
        base["bank_paid"][0]["cents"] = 100
        base["bank_paid"][0]["paid_at"] = "2026-01-31T23:59:59Z"
        self.assert_rejected(loans, base, "paid before instruction")

    def test_unknown_fields_and_bad_structure_rejected(self):
        loans = _loans("L1")
        batch = self._minimal_batch()
        batch["assessments"][0]["surprise"] = 1
        self.assert_rejected(loans, batch, "unknown fields")
        self.assert_rejected(loans, {"assessments": {}}, "JSON array")
        self.assert_rejected({"loans": []}, self._minimal_batch(),
                             "unknown loan")

    def test_empty_arrays_are_valid(self):
        result = reconcile(_loans("L1"),
                           _batch(assessments=[_assessment("L1")]))
        self.assertEqual(result["bank_reconciliation"], [])
        self.assertEqual(len(result["balances"]), 1)
        self.assertEqual(result["refund_suggestions"], [])
        balance = result["balances"][0]
        for key in ("deposited_cents", "fee_cents", "paid_cents",
                    "reserved_cents", "top_up_cents",
                    "excess_reserved_cents"):
            self.assertEqual(balance[key], 0)
        self.assertEqual(balance["blocking_bank_ids"], [])


if __name__ == "__main__":
    unittest.main()
