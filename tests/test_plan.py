import unittest

from museum import plan
from museum.plan import InfeasibleError, PlanError, plan_round


def _source(*loans):
    return {"as_of": "2026-01-01",
            "loans": [{"id": loan_id, "object_id": object_id,
                       "borrower": "b", "start": start, "due": due}
                      for loan_id, object_id, start, due in loans],
            "events": []}


def _run(run_id, from_site, to_site, depart, arrive, capacity):
    return {"run_id": run_id, "from_site": from_site, "to_site": to_site,
            "depart": depart, "arrive": arrive, "capacity": capacity}


def _batch(batch_id, lots, priority=5, home="HOME", venue="VENUE",
           exhibit_start="2026-03-05T00:00:00Z",
           exhibit_end="2026-03-15T00:00:00Z"):
    return {"batch_id": batch_id, "home_site": home, "venue_site": venue,
            "exhibit_start": exhibit_start, "exhibit_end": exhibit_end,
            "priority": priority,
            "lots": [{"loan_id": loan_id, "units": units}
                     for loan_id, units in lots]}


def _booking(batch_id, outbound, return_, units, locked=False):
    return {"batch_id": batch_id, "outbound": list(outbound),
            "return": list(return_), "units": units, "locked": locked}


def _request(hubs=(), runs=(), batches=(), previous_bookings=()):
    return {"hubs": list(hubs), "runs": list(runs), "batches": list(batches),
            "previous_bookings": list(previous_bookings)}


def _booked_units(result):
    """(batch_id, run_id) -> units, from the result's bookings."""
    usage = {}
    for entry in result["bookings"]:
        for run_id in entry["outbound"] + entry["return"]:
            usage[(entry["batch_id"], run_id)] = entry["units"]
    return usage


def _apply_changes(previous_bookings, changes):
    """Old per-(batch, run) quantities with the change list applied."""
    usage = {}
    for booking in previous_bookings:
        for run_id in booking["outbound"] + booking["return"]:
            usage[(booking["batch_id"], run_id)] = booking["units"]
    for change in changes:
        key = (change["batch_id"], change["run_id"])
        usage[key] = usage.get(key, 0) + change["delta_units"]
        if usage[key] == 0:
            del usage[key]
    return usage


class PrioritySumTests(unittest.TestCase):
    """A tight run: one 8-point batch fills it, two 5-point batches share it."""

    def _inputs(self, return_capacity=100, lc_due="2026-03-31"):
        source = _source(("LA", "OBJ-A", "2026-03-01", "2026-03-31"),
                         ("LB", "OBJ-B", "2026-03-01", "2026-03-31"),
                         ("LC", "OBJ-C", "2026-03-01", lc_due))
        runs = [_run("OUT", "HOME", "VENUE",
                     "2026-03-02T08:00:00Z", "2026-03-02T16:00:00Z", 10),
                _run("BACK", "VENUE", "HOME",
                     "2026-03-20T08:00:00Z", "2026-03-20T16:00:00Z",
                     return_capacity)]
        batches = [_batch("BA", [("LA", 10)], priority=8),
                   _batch("BB", [("LB", 5)], priority=5),
                   _batch("BC", [("LC", 5)], priority=5)]
        return source, _request(runs=runs, batches=batches)

    def test_two_five_point_batches_beat_one_eight_point_batch(self):
        source, request = self._inputs()
        result = plan_round(source, request)
        self.assertEqual([b["batch_id"] for b in result["bookings"]],
                         ["BB", "BC"])
        self.assertEqual(result["waitlisted"], ["BA"])
        for entry in result["bookings"]:
            self.assertEqual(entry["outbound"], ["OUT"])
            self.assertEqual(entry["return"], ["BACK"])
        self.assertEqual(result["changes"], [
            {"batch_id": "BB", "run_id": "BACK", "delta_units": 5},
            {"batch_id": "BB", "run_id": "OUT", "delta_units": 5},
            {"batch_id": "BC", "run_id": "BACK", "delta_units": 5},
            {"batch_id": "BC", "run_id": "OUT", "delta_units": 5}])

    def test_outbound_only_application_scores_nothing(self):
        # LC's loan ends before the return run: BC has an outbound leg but
        # no return leg, so it must not count toward the accepted score.
        source, request = self._inputs(lc_due="2026-03-19")
        result = plan_round(source, request)
        self.assertEqual([b["batch_id"] for b in result["bookings"]], ["BA"])
        self.assertEqual(result["waitlisted"], ["BB", "BC"])

    def test_return_capacity_shortfall_scores_nothing(self):
        # The return run fits the 8-point batch alone or either 5-point
        # batch alone, but not both 5-point batches: the pair cannot
        # complete, so its combined score never materializes.
        source = _source(("LA", "OBJ-A", "2026-03-01", "2026-03-31"),
                         ("LB", "OBJ-B", "2026-03-01", "2026-03-31"),
                         ("LC", "OBJ-C", "2026-03-01", "2026-03-31"))
        runs = [_run("OUT", "HOME", "VENUE",
                     "2026-03-02T08:00:00Z", "2026-03-02T16:00:00Z", 10),
                _run("BACK", "VENUE", "HOME",
                     "2026-03-20T08:00:00Z", "2026-03-20T16:00:00Z", 6)]
        batches = [_batch("BA", [("LA", 6)], priority=8),
                   _batch("BB", [("LB", 5)], priority=5),
                   _batch("BC", [("LC", 5)], priority=5)]
        result = plan_round(source, _request(runs=runs, batches=batches))
        self.assertEqual([b["batch_id"] for b in result["bookings"]], ["BA"])
        self.assertEqual(result["waitlisted"], ["BB", "BC"])


class RerouteTests(unittest.TestCase):
    """A confirmed adjustable batch moves to a hub route to free capacity."""

    def _inputs(self, direct_capacity=5, extra_direct=False):
        # LN's window starts 2026-04-03, so BN can only use the direct runs
        # (R1, R5); the hub route via R2/R3 departs a day earlier and is open
        # only to the confirmed batch BK.
        source = _source(("LK", "OBJ-K", "2026-04-01", "2026-04-30"),
                         ("LN", "OBJ-N", "2026-04-03", "2026-04-30"))
        runs = [_run("R1", "HOME", "VENUE",
                     "2026-04-03T09:00:00Z", "2026-04-03T17:00:00Z",
                     direct_capacity),
                _run("R2", "HOME", "HUB",
                     "2026-04-02T08:00:00Z", "2026-04-02T11:00:00Z", 5),
                _run("R3", "HUB", "VENUE",
                     "2026-04-02T13:00:00Z", "2026-04-02T18:00:00Z", 5),
                _run("R4", "VENUE", "HOME",
                     "2026-04-25T09:00:00Z", "2026-04-25T17:00:00Z", 10)]
        if extra_direct:
            runs.append(_run("R5", "HOME", "VENUE",
                             "2026-04-04T09:00:00Z", "2026-04-04T17:00:00Z",
                             5))
        batches = [_batch("BK", [("LK", 5)], priority=9,
                          exhibit_start="2026-04-05T00:00:00Z",
                          exhibit_end="2026-04-20T00:00:00Z"),
                   _batch("BN", [("LN", 5)], priority=7,
                          exhibit_start="2026-04-05T00:00:00Z",
                          exhibit_end="2026-04-20T00:00:00Z")]
        bookings = [_booking("BK", ["R1"], ["R4"], 5)]
        request = _request(hubs=["HUB"], runs=runs, batches=batches,
                           previous_bookings=bookings)
        return source, request, bookings

    def test_reroute_via_hub_admits_new_batch(self):
        source, request, bookings = self._inputs()
        result = plan_round(source, request)
        self.assertEqual(result["bookings"], [
            {"batch_id": "BK", "outbound": ["R2", "R3"], "return": ["R4"],
             "units": 5},
            {"batch_id": "BN", "outbound": ["R1"], "return": ["R4"],
             "units": 5}])
        self.assertEqual(result["waitlisted"], [])
        # The unchanged return run R4 gets no cancel/rebook pair for BK.
        self.assertEqual(result["changes"], [
            {"batch_id": "BK", "run_id": "R1", "delta_units": -5},
            {"batch_id": "BK", "run_id": "R2", "delta_units": 5},
            {"batch_id": "BK", "run_id": "R3", "delta_units": 5},
            {"batch_id": "BN", "run_id": "R1", "delta_units": 5},
            {"batch_id": "BN", "run_id": "R4", "delta_units": 5}])
        self.assertEqual(_apply_changes(bookings, result["changes"]),
                         _booked_units(result))

    def test_equal_score_prefers_minimal_disturbance(self):
        # With room for both on R1, keeping the old route (disturbance 0)
        # beats rerouting BK via the hub (disturbance 15); both are feasible
        # and accept BN for the same score.
        source, request, bookings = self._inputs(direct_capacity=10)
        result = plan_round(source, request)
        self.assertEqual(result["bookings"], [
            {"batch_id": "BK", "outbound": ["R1"], "return": ["R4"],
             "units": 5},
            {"batch_id": "BN", "outbound": ["R1"], "return": ["R4"],
             "units": 5}])
        self.assertEqual(result["changes"], [
            {"batch_id": "BN", "run_id": "R1", "delta_units": 5},
            {"batch_id": "BN", "run_id": "R4", "delta_units": 5}])

    def test_equal_score_and_disturbance_prefers_lexicographic_plan(self):
        # BN may use R1 (sharing with BK) or the equally priced R5; both
        # plans score 7 with zero disturbance, so the smaller run ID wins.
        source, request, bookings = self._inputs(direct_capacity=10,
                                                 extra_direct=True)
        result = plan_round(source, request)
        self.assertEqual(result["bookings"], [
            {"batch_id": "BK", "outbound": ["R1"], "return": ["R4"],
             "units": 5},
            {"batch_id": "BN", "outbound": ["R1"], "return": ["R4"],
             "units": 5}])
        self.assertEqual(result["waitlisted"], [])


class PreviousBookingTests(unittest.TestCase):
    """Stopped runs, changed volumes, locked bookings, and hard failures."""

    def _inputs(self, locked=False, booked_units=6, current_units=4):
        source = _source(("LK2", "OBJ-K2", "2026-05-01", "2026-05-31"))
        runs = [_run("R_NEW_OUT", "HOME", "VENUE",
                     "2026-05-03T09:00:00Z", "2026-05-03T17:00:00Z", 10),
                _run("R_NEW_BACK", "VENUE", "HOME",
                     "2026-05-28T09:00:00Z", "2026-05-28T17:00:00Z", 10)]
        batches = [_batch("BK2", [("LK2", current_units)], priority=3,
                          exhibit_start="2026-05-06T00:00:00Z",
                          exhibit_end="2026-05-20T00:00:00Z")]
        bookings = [_booking("BK2", ["R_OLD_OUT"], ["R_OLD_BACK"],
                             booked_units, locked=locked)]
        request = _request(runs=runs, batches=batches,
                           previous_bookings=bookings)
        return source, request, bookings

    def test_stopped_run_and_changed_volume_give_net_changes(self):
        source, request, bookings = self._inputs()
        result = plan_round(source, request)
        self.assertEqual(result["bookings"], [
            {"batch_id": "BK2", "outbound": ["R_NEW_OUT"],
             "return": ["R_NEW_BACK"], "units": 4}])
        # The stopped runs are still cancelled at their old quantity.
        self.assertEqual(result["changes"], [
            {"batch_id": "BK2", "run_id": "R_NEW_BACK", "delta_units": 4},
            {"batch_id": "BK2", "run_id": "R_NEW_OUT", "delta_units": 4},
            {"batch_id": "BK2", "run_id": "R_OLD_BACK", "delta_units": -6},
            {"batch_id": "BK2", "run_id": "R_OLD_OUT", "delta_units": -6}])
        self.assertEqual(_apply_changes(bookings, result["changes"]),
                         _booked_units(result))

    def test_locked_booking_is_kept_exactly_and_needs_no_changes(self):
        source = _source(("LK2", "OBJ-K2", "2026-05-01", "2026-05-31"))
        runs = [_run("R_NEW_OUT", "HOME", "VENUE",
                     "2026-05-03T09:00:00Z", "2026-05-03T17:00:00Z", 10),
                _run("R_NEW_BACK", "VENUE", "HOME",
                     "2026-05-28T09:00:00Z", "2026-05-28T17:00:00Z", 10)]
        batches = [_batch("BK2", [("LK2", 4)], priority=3,
                          exhibit_start="2026-05-06T00:00:00Z",
                          exhibit_end="2026-05-20T00:00:00Z")]
        bookings = [_booking("BK2", ["R_NEW_OUT"], ["R_NEW_BACK"], 4,
                             locked=True)]
        result = plan_round(source, _request(runs=runs, batches=batches,
                                             previous_bookings=bookings))
        self.assertEqual(result["bookings"], [
            {"batch_id": "BK2", "outbound": ["R_NEW_OUT"],
             "return": ["R_NEW_BACK"], "units": 4}])
        self.assertEqual(result["changes"], [])

    def test_locked_two_run_route_is_kept_exactly(self):
        source = _source(("LK", "OBJ-K", "2026-06-01", "2026-06-30"))
        runs = [_run("R1", "HOME", "HUB",
                     "2026-06-02T09:00:00Z", "2026-06-02T12:00:00Z", 4),
                _run("R2", "HUB", "VENUE",
                     "2026-06-02T14:00:00Z", "2026-06-02T18:00:00Z", 4),
                _run("R3", "VENUE", "HOME",
                     "2026-06-25T09:00:00Z", "2026-06-25T17:00:00Z", 4)]
        batches = [_batch("BK", [("LK", 2)], priority=3,
                          exhibit_start="2026-06-05T00:00:00Z",
                          exhibit_end="2026-06-20T00:00:00Z")]
        bookings = [_booking("BK", ["R1", "R2"], ["R3"], 2, locked=True)]
        result = plan_round(source,
                            _request(hubs=["HUB"], runs=runs,
                                     batches=batches,
                                     previous_bookings=bookings))
        self.assertEqual(result["bookings"], [
            {"batch_id": "BK", "outbound": ["R1", "R2"], "return": ["R3"],
             "units": 2}])
        self.assertEqual(result["changes"], [])

    def test_kept_route_with_changed_volume_books_the_difference(self):
        source = _source(("LK2", "OBJ-K2", "2026-05-01", "2026-05-31"))
        runs = [_run("R_OUT", "HOME", "VENUE",
                     "2026-05-03T09:00:00Z", "2026-05-03T17:00:00Z", 10),
                _run("R_BACK", "VENUE", "HOME",
                     "2026-05-28T09:00:00Z", "2026-05-28T17:00:00Z", 10)]
        batches = [_batch("BK2", [("LK2", 5)], priority=3,
                          exhibit_start="2026-05-06T00:00:00Z",
                          exhibit_end="2026-05-20T00:00:00Z")]
        # Booked 3 units on the still-operating route; the batch now needs 5.
        bookings = [_booking("BK2", ["R_OUT"], ["R_BACK"], 3)]
        result = plan_round(source, _request(runs=runs, batches=batches,
                                             previous_bookings=bookings))
        self.assertEqual(result["bookings"], [
            {"batch_id": "BK2", "outbound": ["R_OUT"],
             "return": ["R_BACK"], "units": 5}])
        self.assertEqual(result["changes"], [
            {"batch_id": "BK2", "run_id": "R_BACK", "delta_units": 2},
            {"batch_id": "BK2", "run_id": "R_OUT", "delta_units": 2}])
        self.assertEqual(_apply_changes(bookings, result["changes"]),
                         _booked_units(result))

    def test_locked_booking_on_stopped_run_fails_the_round(self):
        source, request, _ = self._inputs(locked=True, booked_units=4)
        with self.assertRaisesRegex(InfeasibleError, "BK2"):
            plan_round(source, request)

    def test_locked_booking_with_changed_volume_fails_the_round(self):
        source, request, _ = self._inputs(locked=True, booked_units=6,
                                          current_units=4)
        with self.assertRaisesRegex(InfeasibleError, "BK2"):
            plan_round(source, request)

    def test_two_must_keep_bookings_overloading_a_run_fail_the_round(self):
        source = _source(("LK1", "OBJ-K1", "2026-05-01", "2026-05-31"),
                         ("LK2", "OBJ-K2", "2026-05-01", "2026-05-31"))
        runs = [_run("R_OUT", "HOME", "VENUE",
                     "2026-05-03T09:00:00Z", "2026-05-03T17:00:00Z", 8),
                _run("R_BACK", "VENUE", "HOME",
                     "2026-05-28T09:00:00Z", "2026-05-28T17:00:00Z", 100)]
        batches = [_batch("BK1", [("LK1", 5)], priority=3,
                          exhibit_start="2026-05-06T00:00:00Z",
                          exhibit_end="2026-05-20T00:00:00Z"),
                   _batch("BK2", [("LK2", 5)], priority=3,
                          exhibit_start="2026-05-06T00:00:00Z",
                          exhibit_end="2026-05-20T00:00:00Z")]
        # Both locked onto R_OUT at 5 units each: 10 > capacity 8.
        bookings = [_booking("BK1", ["R_OUT"], ["R_BACK"], 5, locked=True),
                    _booking("BK2", ["R_OUT"], ["R_BACK"], 5, locked=True)]
        request = _request(runs=runs, batches=batches,
                           previous_bookings=bookings)
        with self.assertRaises(InfeasibleError):
            plan_round(source, request)

    def test_adjustable_bookings_still_fail_when_never_fitting(self):
        # Same overload, but with adjustable bookings and no alternative
        # route: the round still fails rather than dropping a confirmed show.
        source = _source(("LK1", "OBJ-K1", "2026-05-01", "2026-05-31"),
                         ("LK2", "OBJ-K2", "2026-05-01", "2026-05-31"))
        runs = [_run("R_OUT", "HOME", "VENUE",
                     "2026-05-03T09:00:00Z", "2026-05-03T17:00:00Z", 8),
                _run("R_BACK", "VENUE", "HOME",
                     "2026-05-28T09:00:00Z", "2026-05-28T17:00:00Z", 100)]
        batches = [_batch("BK1", [("LK1", 5)], priority=3,
                          exhibit_start="2026-05-06T00:00:00Z",
                          exhibit_end="2026-05-20T00:00:00Z"),
                   _batch("BK2", [("LK2", 5)], priority=3,
                          exhibit_start="2026-05-06T00:00:00Z",
                          exhibit_end="2026-05-20T00:00:00Z")]
        bookings = [_booking("BK1", ["R_OUT"], ["R_BACK"], 5),
                    _booking("BK2", ["R_OUT"], ["R_BACK"], 5)]
        request = _request(runs=runs, batches=batches,
                           previous_bookings=bookings)
        with self.assertRaises(InfeasibleError):
            plan_round(source, request)


class BoundaryTests(unittest.TestCase):
    """Cross-day loan dates and transfer-wait edges, both ends inclusive."""

    def test_run_dates_must_fit_every_loan_window(self):
        source = _source(("LD1", "OBJ-D1", "2026-06-01", "2026-06-03"),
                         ("LD2", "OBJ-D2", "2026-06-01", "2026-06-02"))
        runs = [_run("R_OUT", "HOME", "VENUE",
                     "2026-06-02T22:00:00Z", "2026-06-03T06:00:00Z", 2),
                _run("R_BACK", "VENUE", "HOME",
                     "2026-06-03T19:00:00Z", "2026-06-03T23:00:00Z", 2)]
        batches = [_batch("BD1", [("LD1", 1)],
                          exhibit_start="2026-06-03T12:00:00Z",
                          exhibit_end="2026-06-03T18:00:00Z"),
                   _batch("BD2", [("LD2", 1)],
                          exhibit_start="2026-06-03T12:00:00Z",
                          exhibit_end="2026-06-03T18:00:00Z")]
        result = plan_round(source, _request(runs=runs, batches=batches))
        # The overnight run arrives on LD1's due day (valid, inclusive) but
        # one day past LD2's due day (invalid).
        self.assertEqual([b["batch_id"] for b in result["bookings"]],
                         ["BD1"])
        self.assertEqual(result["waitlisted"], ["BD2"])

    def test_depart_and_arrive_on_window_edges_are_valid(self):
        source = _source(("LD", "OBJ-D", "2026-06-02", "2026-06-03"))
        runs = [_run("R_OUT", "HOME", "VENUE",
                     "2026-06-02T00:00:00Z", "2026-06-02T12:00:00Z", 1),
                _run("R_BACK", "VENUE", "HOME",
                     "2026-06-03T08:00:00Z", "2026-06-03T23:59:00Z", 1)]
        batches = [_batch("BD", [("LD", 1)],
                          exhibit_start="2026-06-02T18:00:00Z",
                          exhibit_end="2026-06-03T06:00:00Z")]
        result = plan_round(source, _request(runs=runs, batches=batches))
        self.assertEqual([b["batch_id"] for b in result["bookings"]],
                         ["BD"])

    def _transfer_result(self, second_run):
        source = _source(("LT", "OBJ-T", "2026-06-01", "2026-06-10"))
        runs = [_run("T1", "HOME", "HUB",
                     "2026-06-02T09:00:00Z", "2026-06-02T10:00:00Z", 1),
                second_run,
                _run("BACK", "VENUE", "HOME",
                     "2026-06-07T09:00:00Z", "2026-06-07T12:00:00Z", 1)]
        batches = [_batch("BT", [("LT", 1)],
                          exhibit_start="2026-06-05T00:00:00Z",
                          exhibit_end="2026-06-06T00:00:00Z")]
        return plan_round(source, _request(hubs=["HUB"], runs=runs,
                                           batches=batches))

    def test_transfer_gap_of_exactly_one_hour_is_accepted(self):
        result = self._transfer_result(
            _run("T2", "HUB", "VENUE",
                 "2026-06-02T11:00:00Z", "2026-06-02T12:00:00Z", 1))
        self.assertEqual(result["bookings"][0]["outbound"], ["T1", "T2"])

    def test_transfer_gap_under_one_hour_is_rejected(self):
        result = self._transfer_result(
            _run("T2", "HUB", "VENUE",
                 "2026-06-02T10:59:00Z", "2026-06-02T12:00:00Z", 1))
        self.assertEqual(result["bookings"], [])
        self.assertEqual(result["waitlisted"], ["BT"])

    def test_transfer_gap_of_exactly_twenty_four_hours_is_accepted(self):
        result = self._transfer_result(
            _run("T2", "HUB", "VENUE",
                 "2026-06-03T10:00:00Z", "2026-06-03T11:00:00Z", 1))
        self.assertEqual(result["bookings"][0]["outbound"], ["T1", "T2"])

    def test_transfer_gap_over_twenty_four_hours_is_rejected(self):
        result = self._transfer_result(
            _run("T2", "HUB", "VENUE",
                 "2026-06-03T10:01:00Z", "2026-06-03T11:00:00Z", 1))
        self.assertEqual(result["bookings"], [])
        self.assertEqual(result["waitlisted"], ["BT"])

    def test_transfer_site_must_be_a_hub_and_not_an_endpoint(self):
        source = _source(("LT", "OBJ-T", "2026-06-01", "2026-06-10"))
        runs = [_run("T1", "HOME", "HUB",
                     "2026-06-02T09:00:00Z", "2026-06-02T10:00:00Z", 1),
                _run("T2", "HUB", "VENUE",
                     "2026-06-02T11:00:00Z", "2026-06-02T12:00:00Z", 1),
                _run("BACK", "VENUE", "HOME",
                     "2026-06-07T09:00:00Z", "2026-06-07T12:00:00Z", 1)]
        batches = [_batch("BT", [("LT", 1)],
                          exhibit_start="2026-06-05T00:00:00Z",
                          exhibit_end="2026-06-06T00:00:00Z")]
        # HUB is not listed as a hub: no transfer is possible there.
        result = plan_round(source, _request(runs=runs, batches=batches))
        self.assertEqual(result["waitlisted"], ["BT"])

    def test_multi_lot_batch_sums_units_and_checks_every_loan(self):
        source = _source(("L1", "OBJ-1", "2026-06-01", "2026-06-10"),
                         ("L2", "OBJ-2", "2026-06-01", "2026-06-05"))
        runs = [_run("R_BAD", "HOME", "VENUE",
                     "2026-06-05T22:00:00Z", "2026-06-06T08:00:00Z", 9),
                _run("R_GOOD", "HOME", "VENUE",
                     "2026-06-04T09:00:00Z", "2026-06-04T17:00:00Z", 9),
                _run("R_BACK", "VENUE", "HOME",
                     "2026-06-05T09:00:00Z", "2026-06-05T17:00:00Z", 9)]
        batches = [_batch("BX", [("L1", 2), ("L2", 3)], priority=4,
                          exhibit_start="2026-06-04T18:00:00Z",
                          exhibit_end="2026-06-05T08:00:00Z")]
        result = plan_round(source, _request(runs=runs, batches=batches))
        # R_BAD arrives 2026-06-06, past L2's due day: the whole batch must
        # take R_GOOD, and it occupies 2 + 3 = 5 units there.
        self.assertEqual(result["bookings"], [
            {"batch_id": "BX", "outbound": ["R_GOOD"], "return": ["R_BACK"],
             "units": 5}])
        self.assertEqual(result["changes"], [
            {"batch_id": "BX", "run_id": "R_BACK", "delta_units": 5},
            {"batch_id": "BX", "run_id": "R_GOOD", "delta_units": 5}])


class DeterminismTests(unittest.TestCase):
    def test_record_order_does_not_change_the_result(self):
        source = _source(("LK", "OBJ-K", "2026-04-01", "2026-04-30"),
                         ("LN", "OBJ-N", "2026-04-01", "2026-04-30"))
        runs = [_run("R1", "HOME", "VENUE",
                     "2026-04-02T09:00:00Z", "2026-04-02T17:00:00Z", 5),
                _run("R2", "HOME", "HUB",
                     "2026-04-02T08:00:00Z", "2026-04-02T11:00:00Z", 5),
                _run("R3", "HUB", "VENUE",
                     "2026-04-02T13:00:00Z", "2026-04-02T18:00:00Z", 5),
                _run("R4", "VENUE", "HOME",
                     "2026-04-25T09:00:00Z", "2026-04-25T17:00:00Z", 10)]
        batches = [_batch("BK", [("LK", 5)], priority=9,
                          exhibit_start="2026-04-04T00:00:00Z",
                          exhibit_end="2026-04-20T00:00:00Z"),
                   _batch("BN", [("LN", 5)], priority=7,
                          exhibit_start="2026-04-04T00:00:00Z",
                          exhibit_end="2026-04-20T00:00:00Z")]
        bookings = [_booking("BK", ["R1"], ["R4"], 5)]
        reference = plan_round(source, _request(
            hubs=["HUB"], runs=runs, batches=batches,
            previous_bookings=bookings))
        shuffled_source = _source(("LN", "OBJ-N", "2026-04-01", "2026-04-30"),
                                  ("LK", "OBJ-K", "2026-04-01", "2026-04-30"))
        shuffled = _request(hubs=["HUB"], runs=list(reversed(runs)),
                            batches=list(reversed(batches)),
                            previous_bookings=bookings)
        self.assertEqual(plan_round(shuffled_source, shuffled), reference)

    def test_empty_request_is_valid(self):
        result = plan_round(_source(), _request())
        self.assertEqual(result, {"bookings": [], "waitlisted": [],
                                  "changes": []})

    def test_unfittable_new_batch_is_waitlisted_not_failed(self):
        source = _source(("LN", "OBJ-N", "2026-04-01", "2026-04-30"))
        batches = [_batch("BN", [("LN", 5)], priority=7)]
        result = plan_round(source, _request(runs=[], batches=batches))
        self.assertEqual(result["bookings"], [])
        self.assertEqual(result["waitlisted"], ["BN"])
        self.assertEqual(result["changes"], [])


class ValidationTests(unittest.TestCase):
    def _valid_source(self):
        return _source(("L1", "OBJ-1", "2026-03-01", "2026-03-31"),
                       ("L2", "OBJ-2", "2026-03-01", "2026-03-31"))

    def _valid_request(self):
        runs = [_run("OUT", "HOME", "VENUE",
                     "2026-03-02T08:00:00Z", "2026-03-02T16:00:00Z", 10),
                _run("BACK", "VENUE", "HOME",
                     "2026-03-20T08:00:00Z", "2026-03-20T16:00:00Z", 10)]
        return _request(runs=runs, batches=[_batch("B1", [("L1", 2)])])

    def test_source_structure_and_loan_dates(self):
        with self.assertRaisesRegex(PlanError, "source must be"):
            plan_round([], self._valid_request())
        with self.assertRaisesRegex(PlanError, "source.loans"):
            plan_round({}, self._valid_request())
        with self.assertRaisesRegex(PlanError, r"source\.loans\[0\]\.id"):
            plan_round({"loans": [{"id": "", "object_id": "O",
                                   "start": "2026-03-01",
                                   "due": "2026-03-02"}]},
                       self._valid_request())
        with self.assertRaisesRegex(PlanError, "duplicates loan L1"):
            plan_round(_source(("L1", "OBJ-1", "2026-03-01", "2026-03-31"),
                               ("L1", "OBJ-2", "2026-03-01", "2026-03-31")),
                       self._valid_request())
        with self.assertRaisesRegex(PlanError, r"loans\[0\]\.start"):
            plan_round(_source(("L1", "OBJ-1", "03-01", "2026-03-31")),
                       self._valid_request())
        with self.assertRaisesRegex(PlanError, "start after due"):
            plan_round(_source(("L1", "OBJ-1", "2026-03-31", "2026-03-01")),
                       self._valid_request())
        with self.assertRaisesRegex(PlanError, "object_id"):
            plan_round(_source(("L1", "", "2026-03-01", "2026-03-31")),
                       self._valid_request())

    def test_request_structure(self):
        with self.assertRaisesRegex(PlanError, "request must be"):
            plan_round(self._valid_source(), [])
        with self.assertRaisesRegex(PlanError, "unknown fields"):
            plan_round(self._valid_source(),
                       {**self._valid_request(), "extra": 1})
        with self.assertRaisesRegex(PlanError, "request.hubs"):
            plan_round(self._valid_source(),
                       {**self._valid_request(), "hubs": "HUB"})
        with self.assertRaisesRegex(PlanError, "hubs.*duplicates"):
            plan_round(self._valid_source(),
                       {**self._valid_request(), "hubs": ["H", "H"]})

    def test_run_fields(self):
        def runs_with(run):
            return {**self._valid_request(), "runs": [run]}

        good = self._valid_request()["runs"][0]
        with self.assertRaisesRegex(PlanError, "unknown fields"):
            plan_round(self._valid_source(),
                       runs_with({**good, "note": "x"}))
        with self.assertRaisesRegex(PlanError, "duplicates run OUT"):
            plan_round(self._valid_source(),
                       {**self._valid_request(),
                        "runs": [good, dict(good)]})
        with self.assertRaisesRegex(PlanError, "identical from_site"):
            plan_round(self._valid_source(),
                       runs_with(_run("X", "A", "A", "2026-03-02T08:00:00Z",
                                      "2026-03-02T16:00:00Z", 1)))
        with self.assertRaisesRegex(PlanError, "arrive after it departs"):
            plan_round(self._valid_source(),
                       runs_with(_run("X", "A", "B", "2026-03-02T16:00:00Z",
                                      "2026-03-02T08:00:00Z", 1)))
        with self.assertRaisesRegex(PlanError, r"runs\[0\]\.depart"):
            plan_round(self._valid_source(),
                       runs_with(_run("X", "A", "B", "2026-03-02 08:00",
                                      "2026-03-02T16:00:00Z", 1)))
        with self.assertRaisesRegex(PlanError, "valid calendar time"):
            plan_round(self._valid_source(),
                       runs_with(_run("X", "A", "B", "2026-02-30T08:00:00Z",
                                      "2026-03-02T16:00:00Z", 1)))
        with self.assertRaisesRegex(PlanError, "capacity"):
            plan_round(self._valid_source(),
                       runs_with(_run("X", "A", "B", "2026-03-02T08:00:00Z",
                                      "2026-03-02T16:00:00Z", -1)))
        with self.assertRaisesRegex(PlanError, "capacity"):
            plan_round(self._valid_source(),
                       runs_with(_run("X", "A", "B", "2026-03-02T08:00:00Z",
                                      "2026-03-02T16:00:00Z", True)))

    def test_batch_fields(self):
        def batches_with(batch):
            return {**self._valid_request(), "batches": [batch]}

        with self.assertRaisesRegex(PlanError, "identical home_site"):
            plan_round(self._valid_source(),
                       batches_with(_batch("B1", [("L1", 2)], home="SAME",
                                           venue="SAME")))
        with self.assertRaisesRegex(PlanError, "exhibit_end"):
            plan_round(self._valid_source(),
                       batches_with(_batch(
                           "B1", [("L1", 2)],
                           exhibit_start="2026-03-05T00:00:00Z",
                           exhibit_end="2026-03-05T00:00:00Z")))
        with self.assertRaisesRegex(PlanError, "priority"):
            plan_round(self._valid_source(),
                       batches_with(_batch("B1", [("L1", 2)], priority=0)))
        with self.assertRaisesRegex(PlanError, "priority"):
            plan_round(self._valid_source(),
                       batches_with(_batch("B1", [("L1", 2)],
                                           priority=False)))
        with self.assertRaisesRegex(PlanError, "lots"):
            plan_round(self._valid_source(),
                       batches_with(_batch("B1", [])))
        with self.assertRaisesRegex(PlanError, "unknown loan L9"):
            plan_round(self._valid_source(),
                       batches_with(_batch("B1", [("L9", 2)])))
        with self.assertRaisesRegex(PlanError, r"lots\[0\]\.units"):
            plan_round(self._valid_source(),
                       batches_with(_batch("B1", [("L1", 0)])))
        with self.assertRaisesRegex(PlanError, r"lots\[0\]\.units"):
            plan_round(self._valid_source(),
                       batches_with(_batch("B1", [("L1", True)])))
        with self.assertRaisesRegex(PlanError, "duplicates batch B1"):
            plan_round(self._valid_source(),
                       {**self._valid_request(),
                        "batches": [_batch("B1", [("L1", 2)]),
                                    _batch("B1", [("L2", 2)])]})

    def test_loan_and_object_used_once_per_request(self):
        request = {**self._valid_request(),
                   "batches": [_batch("B1", [("L1", 2)]),
                               _batch("B2", [("L1", 2)])]}
        with self.assertRaisesRegex(PlanError, "reuses loan L1"):
            plan_round(self._valid_source(), request)
        source = _source(("L1", "OBJ-1", "2026-03-01", "2026-03-31"),
                         ("L2", "OBJ-1", "2026-03-01", "2026-03-31"))
        request = {**self._valid_request(),
                   "batches": [_batch("B1", [("L1", 2)]),
                               _batch("B2", [("L2", 2)])]}
        with self.assertRaisesRegex(PlanError, "reuses object OBJ-1"):
            plan_round(source, request)

    def test_booking_fields(self):
        def bookings_with(booking):
            return {**self._valid_request(), "previous_bookings": [booking]}

        with self.assertRaisesRegex(PlanError, "unknown batch B9"):
            plan_round(self._valid_source(),
                       bookings_with(_booking("B9", ["OUT"], ["BACK"], 2)))
        with self.assertRaisesRegex(PlanError, "duplicates the booking"):
            plan_round(self._valid_source(),
                       {**self._valid_request(),
                        "previous_bookings": [
                            _booking("B1", ["OUT"], ["BACK"], 2),
                            _booking("B1", ["OUT"], ["BACK"], 2)]})
        with self.assertRaisesRegex(PlanError, r"\.outbound"):
            plan_round(self._valid_source(),
                       bookings_with(_booking("B1", [], ["BACK"], 2)))
        with self.assertRaisesRegex(PlanError, r"\.return"):
            plan_round(self._valid_source(),
                       bookings_with(
                           _booking("B1", ["OUT"], ["A", "B", "C"], 2)))
        with self.assertRaisesRegex(PlanError, "must not repeat a run"):
            plan_round(self._valid_source(),
                       bookings_with(_booking("B1", ["OUT", "OUT"],
                                              ["BACK"], 2)))
        with self.assertRaisesRegex(PlanError, "must not repeat a run"):
            plan_round(self._valid_source(),
                       bookings_with(_booking("B1", ["X"], ["X"], 2)))
        with self.assertRaisesRegex(PlanError, r"\.units"):
            plan_round(self._valid_source(),
                       bookings_with(_booking("B1", ["OUT"], ["BACK"], 0)))
        with self.assertRaisesRegex(PlanError, r"\.locked"):
            plan_round(self._valid_source(),
                       bookings_with(_booking("B1", ["OUT"], ["BACK"], 2,
                                              locked=1)))

    def test_events_and_as_of_are_not_used(self):
        # Garbage in the unused source sections does not affect planning.
        source = self._valid_source()
        source["as_of"] = "not a date"
        source["events"] = [{"id": "E", "loan_id": "GHOST", "type": "??",
                             "time": "whenever"}]
        result = plan_round(source, self._valid_request())
        self.assertEqual([b["batch_id"] for b in result["bookings"]],
                         ["B1"])


if __name__ == "__main__":
    unittest.main()
