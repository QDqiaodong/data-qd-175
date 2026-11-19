# Collection loan contract

Each loan has a unique ID, object ID, borrower, start date, and due date. Events have unique IDs, loan ID, type (`dispatch`, `receipt`, `condition`), UTC timestamp, location, and optional condition code. Dispatch/receipt must fall within the loan interval; condition observations may occur at any time. Each loan is `overdue` when the as-of date exceeds due date and no receipt exists; otherwise it is `returned` or `on_loan`. Timeline order is timestamp then event ID. The report surfaces exceptions and does not determine conservation status or insurance value.
