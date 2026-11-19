from datetime import date, datetime


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
            "exceptions": sum(bool(x["exceptions"]) for x in result)}
