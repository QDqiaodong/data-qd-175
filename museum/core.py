from datetime import date, datetime


def _object_custody(loans, events):
    """Project physical custody per object from valid dispatch/receipt events."""
    relevant = []
    for event in events:
        if event["type"] not in ("dispatch", "receipt"):
            continue
        loan = loans[event["loan_id"]]
        start, due = date.fromisoformat(loan["start"]), date.fromisoformat(loan["due"])
        stamp = datetime.fromisoformat(event["time"])
        if start <= stamp.date() <= due:
            relevant.append((stamp, event))
    relevant.sort(key=lambda item: (item[0], item[1]["id"]))

    object_ids = {loan["object_id"] for loan in loans.values()}
    active_by_object = {object_id: {} for object_id in object_ids}
    conflicts_by_object = {object_id: [] for object_id in object_ids}

    for _, event in relevant:
        object_id = loans[event["loan_id"]]["object_id"]
        active = active_by_object[object_id]
        if event["type"] == "dispatch":
            for active_loan_id in sorted(active, key=lambda loan_id: active[loan_id]):
                conflicts_by_object[object_id].append({
                    "object_id": object_id,
                    "new_dispatch_event_id": event["id"],
                    "earlier_dispatch_event_id": active[active_loan_id],
                })
            active[event["loan_id"]] = event["id"]
        else:
            active.pop(event["loan_id"], None)

    return [
        {"object_id": object_id,
         "active_loan_ids": sorted(active_by_object[object_id]),
         "conflicts": conflicts_by_object[object_id]}
        for object_id in sorted(object_ids)
    ]


def _condition_hold_violations(loans, events):
    """List valid dispatches that occur while their object is on conservation hold."""
    object_ids = {loan["object_id"] for loan in loans.values()}
    per_object = {object_id: [] for object_id in object_ids}
    for event in events:
        loan = loans[event["loan_id"]]
        stamp = datetime.fromisoformat(event["time"])
        if event["type"] == "condition":
            per_object[loan["object_id"]].append((stamp, event))
        elif event["type"] == "dispatch":
            start, due = date.fromisoformat(loan["start"]), date.fromisoformat(loan["due"])
            if start <= stamp.date() <= due:
                per_object[loan["object_id"]].append((stamp, event))

    violations = []
    for object_id in sorted(object_ids):
        hold_event_id = None
        stream = sorted(per_object[object_id], key=lambda item: (item[0], item[1]["id"]))
        for _, event in stream:
            if event["type"] == "condition":
                code = event.get("condition_code")
                if code == "conservation_hold":
                    hold_event_id = event["id"]
                elif code == "conservation_clear":
                    hold_event_id = None
            elif hold_event_id is not None:
                violations.append({"object_id": object_id,
                                   "dispatch_event_id": event["id"],
                                   "hold_event_id": hold_event_id})
    return violations


def audit(data):
    as_of = date.fromisoformat(data["as_of"])
    loans = {x["id"]: x for x in data["loans"]}
    events = data["events"]
    if len(loans) != len(data["loans"]) or len({x["id"] for x in events}) != len(events):
        raise ValueError("loan and event IDs must be unique")
    grouped = {key: [] for key in loans}
    for loan in loans.values():
        start, due = date.fromisoformat(loan["start"]), date.fromisoformat(loan["due"])
        if due < start:
            raise ValueError(f"invalid loan dates: {loan['id']}")
    for event in events:
        if event["loan_id"] not in loans:
            raise ValueError(f"unknown loan {event['loan_id']}")
        stamp = datetime.fromisoformat(event["time"])
        if stamp.tzinfo is None:
            raise ValueError("event timestamps require an offset")
        if event["type"] not in {"dispatch", "receipt", "condition"}:
            raise ValueError(f"unknown event type {event['type']}")
        grouped[event["loan_id"]].append(event)
    result = []
    for loan_id, loan in sorted(loans.items()):
        timeline = sorted(grouped[loan_id], key=lambda e: (datetime.fromisoformat(e["time"]), e["id"]))
        start, due = date.fromisoformat(loan["start"]), date.fromisoformat(loan["due"])
        exceptions = []
        valid_receipts = []
        for event in timeline:
            day = datetime.fromisoformat(event["time"]).date()
            if event["type"] == "dispatch" and not start <= day <= due:
                exceptions.append("dispatch_outside_loan")
            if event["type"] == "receipt":
                if not start <= day <= due:
                    exceptions.append("receipt_outside_loan")
                else:
                    valid_receipts.append(event)
        if len(valid_receipts) > 1:
            exceptions.append("multiple_receipts")
        status = "returned" if valid_receipts else ("overdue" if as_of > due else "on_loan")
        result.append({"loan_id": loan_id, "object_id": loan["object_id"], "borrower": loan["borrower"],
                       "status": status, "last_event_id": timeline[-1]["id"] if timeline else None,
                       "condition_codes": [e["condition_code"] for e in timeline if e["type"] == "condition" and e.get("condition_code")],
                       "exceptions": sorted(set(exceptions)), "timeline": [e["id"] for e in timeline]})
    return {"loans": result, "overdue": sum(x["status"] == "overdue" for x in result),
            "exceptions": sum(bool(x["exceptions"]) for x in result),
            "object_custody": _object_custody(loans, events),
            "condition_hold_violations": _condition_hold_violations(loans, events)}
