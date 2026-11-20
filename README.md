# data-qd-175 | Museum collection loan tracking

Builds an item-level custody timeline from loan and condition events, flags due/overdue loans, detects events outside an active loan interval, and projects per-object physical custody including conflicts where concurrent loans claim the same object. Python 3.11+; standard library only.

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
.venv/bin/loan-audit --input fixtures/input.json --output run/audit.json
.venv/bin/python -m unittest discover -s tests -v
```

All records are synthetic. This is an operational exception report, not a condition valuation. MIT licensed.

Fixture summary: 1 overdue loan, 0 loans with event exceptions.

## loan-deposit: refund reconciliation and safe new refund advice

`loan-deposit` settles one batch of loan-deposit refunds entirely from local
files: it reconciles bank-confirmed refund movements against the instructions
already handed to the bank, states which movements are confirmed, ambiguous or
unmatched, and advises which new refunds are still safe to issue. It never
opens a network connection and never sends, revokes or cancels a refund.
(Without installing, use `python3 -m museum.deposit_cli` in place of
`loan-deposit`.)

```sh
loan-deposit --loans LOANS.json --batch BATCH.json --output RESULT.json
```

- **Inputs.** The loans file is the usual audit input; only each loan's `id`
  is read, and finance facts are never inferred from events or loan dates.
  The batch is the complete material of one settlement run and holds exactly
  five arrays: `assessments` (one `loan_id` + `fee_cents` per settled loan;
  the fee may be zero or exceed the deposit, and may change on re-runs),
  `deposits` (`deposit_id`, `loan_id`, `payer_ref`, `received_at`, `cents`;
  one payer may fund several deposits across loans), `issued_refunds`
  (instructions already with the bank, each tied to one original deposit and
  never exceeding its receipt in total), `bank_paid` (bank-confirmed refund
  movements, kept even when they cannot be related to anything), and
  `confirmed_links` (manual `bank_id`/`refund_id` verifications). Amounts are
  integer cents; fees are non-negative, every other amount positive, and
  booleans are never amounts. Missing fields, wrong types, duplicate primary
  keys, dangling references, duplicate fee rows, contradictory manual links,
  or instructions exceeding a deposit reject the whole batch with the
  offending record or field named.
- **Reconciliation.** Manual links are accepted first and must still share
  payer and amount and not predate the instruction; neither a bank row nor an
  instruction may appear in two links. Among the remaining rows, an automatic
  link is possible only when payer and amount match and the payment lies in
  the inclusive 48-hour window after the instruction was sent. Finance's
  objective is the maximum number of explainable successful bank rows. A row
  is `matched` only when every maximum explanation settles it against the
  same instruction; `ambiguous` when maximum explanations disagree (including
  one leaving it empty, and including a row with a single contested
  candidate); `unmatched` only when no maximum explanation can link it. The
  Dulmage-Mendelsohn decomposition computes these facts without enumerating
  explanations, so batches of dozens of interchangeable same-payer,
  same-amount rows stay fast.
- **Balances.** Per loan: `deposited_cents`, `fee_cents`, `paid_cents`
  (confirmed refunds only), `reserved_cents` (all other issued instructions,
  including receipts still in transit that may yet execute), `top_up_cents`
  (the cash shortfall when deposits minus confirmed refunds do not cover the
  fee), `excess_reserved_cents` (the extra exposure added by in-transit
  instructions, kept separate from the cash top-up), and `blocking_bank_ids`.
  Any unmatched or ambiguous bank row for a payer the loan received money
  from blocks that loan; rows for payers with no deposit stay in the
  reconciliation but block nobody.
- **Suggestions.** Only unblocked loans with a positive remainder get
  suggestions, one per deposit, each refunding no more than that deposit's
  amount left outside its already issued instructions. Deposits are consumed
  earliest received first, ties broken by `deposit_id`. The command only
  prints advice; the actual instruction reaches the next batch as an
  `issued_refunds` entry.
- **Determinism and exit codes.** Reordering any input array never changes
  the result. Success is `0`; any input, validation or output error is `2`
  with a `loan-deposit: ...` stderr message and empty stdout. The output is
  create-only (parent directories are created, an existing file is never
  overwritten, and a failed write leaves no partial result).

### Copyable example

Two loans share payer `P`; each has a 400-cent refund instruction and two
interchangeable success rows arrive, so nothing can be auto-confirmed and no
new refund is advised. After finance manually verifies one row, the other is
forced as well and the remaining 600 cents per loan become safe suggestions. A
third loan's unexplained bank row stays on record without disturbing the
others.

```sh
cd /path/to/project
D="python3 -m museum.deposit_cli"   # or: .venv/bin/loan-deposit

cat > loans.json <<'JSON'
{"as_of":"2026-05-15",
 "loans":[{"id":"L1","object_id":"OBJ-1","borrower":"Gallery A",
           "start":"2026-01-01","due":"2026-12-31"},
          {"id":"L2","object_id":"OBJ-2","borrower":"Gallery A",
           "start":"2026-01-01","due":"2026-12-31"},
          {"id":"L3","object_id":"OBJ-3","borrower":"Gallery B",
           "start":"2026-01-01","due":"2026-12-31"}],
 "events":[]}
JSON
cat > batch.json <<'JSON'
{"assessments":[{"loan_id":"L1","fee_cents":0},
                {"loan_id":"L2","fee_cents":0},
                {"loan_id":"L3","fee_cents":0}],
 "deposits":[{"deposit_id":"D1","loan_id":"L1","payer_ref":"P",
              "received_at":"2026-01-01T00:00:00Z","cents":1000},
             {"deposit_id":"D2","loan_id":"L2","payer_ref":"P",
              "received_at":"2026-01-02T00:00:00Z","cents":1000},
             {"deposit_id":"D3","loan_id":"L3","payer_ref":"Q",
              "received_at":"2026-01-03T00:00:00Z","cents":1000}],
 "issued_refunds":[{"refund_id":"R1","deposit_id":"D1",
                    "sent_at":"2026-02-01T00:00:00Z","cents":400},
                   {"refund_id":"R2","deposit_id":"D2",
                    "sent_at":"2026-02-01T00:00:00Z","cents":400}],
 "bank_paid":[{"bank_id":"B1","payer_ref":"P",
               "paid_at":"2026-02-01T08:00:00Z","cents":400},
              {"bank_id":"B2","payer_ref":"P",
               "paid_at":"2026-02-01T09:00:00Z","cents":400},
              {"bank_id":"B3","payer_ref":"Q",
               "paid_at":"2026-03-01T09:00:00Z","cents":250}],
 "confirmed_links":[]}
JSON

$D --loans loans.json --batch batch.json --output run/result.json
#   -> B1 and B2 are ambiguous (each could settle R1 or R2), B3 unmatched;
#      refund_suggestions is empty while the ambiguity stands.

# Finance verifies B1 -> R1 with extra evidence; B2 is then forced to R2.
python3 - <<'PY'
import json
batch = json.load(open("batch.json"))
batch["confirmed_links"] = [{"bank_id": "B1", "refund_id": "R1"}]
json.dump(batch, open("batch2.json", "w"))
PY
$D --loans loans.json --batch batch2.json --output run/result2.json
#   -> B1, B2 matched (R1, R2); suggestions: 600 cents from D1 (loan L1)
#      and 600 cents from D2 (loan L2); L3 stays blocked by its own B3 row.

$D --loans loans.json --batch batch2.json --output run/result2.json
#   -> exit 2, "loan-deposit: output already exists: run/result2.json"
```

## loan-share: authorized publishing of scoped reports

`loan-share` is a local, file-backed publishing tool for handing audit data to
partner institutions. Operators maintain authorizations and issue frozen
release packages; partners fetch exactly the packages issued to them. There is
no login system and no network service: `recipient` is the operator-provided
business identity of the partner, and sequential processes share one
persistent store directory. (Without installing, use
`python3 -m museum.share_cli` in place of `loan-share`.)

- **Rights** are keyed by the `(collection_id, recipient, loan_id)` triple and
  start fully unauthorized. `collection_id` is a stable namespace: a loan ID
  keeps its meaning inside one collection forever, and identical loan IDs in
  different collections never share rights or revocations.
- **Issue** freezes a report built only from the authorized loans and the
  events that reference them, using the original audit semantics. Counts,
  custody claims, and conflicts reflect only the selected data; other loans
  on the same object leave no trace (no ID, event ID, borrower, or effect).
  The full source must still pass the original audit validation — a broken
  record outside the shared scope fails the issue.
- **Revocation is permanent.** A real revocation makes every earlier package
  for that collection/recipient containing the loan unreadable as a whole —
  even if the rest of the package is still authorized. Re-granting never
  revives an old package; it only allows issuing new ones.
- **Idempotent credentials.** `change_id` / `release_id` are unique per store
  and act as retry credentials: an identical retry returns the first receipt
  without re-applying anything; the same ID with a different business request
  is rejected. Source equality compares parsed JSON (object key order
  ignored, array order preserved); loan ID lists compare as sets.
- **Digest.** The body digest is the SHA-256 (lowercase hex) of the UTF-8
  encoding of `json.dumps(report, ensure_ascii=False, sort_keys=True,
  separators=(',', ':'))`.
- **Durability.** Each successful rights/issue operation commits the whole
  store atomically (temp file, fsync, `os.replace`, directory fsync) before
  the receipt is printed, so a crash near commit leaves the complete old or
  the complete new state — never half a rights change, half a package, or a
  credential without its frozen body.
- **Exit codes.** Success is `0`; `rights`/`issue` print one receipt JSON to
  stdout, `fetch` prints `{"release_id": ..., "report_sha256": ...}`. Any
  input, permission, identity, storage, or output-path error exits `2`, writes
  `loan-share: ...` to stderr, and leaves stdout empty. `fetch` output is
  create-only (parent directories are created, existing files are never
  overwritten, and a failed write leaves no partial file).

### Copyable example

Two loans share one object; their dispatches conflict museum-wide. The
scenario: grant both, issue a combined package, revoke one loan, and watch
the old package die permanently while correctly scoped packages keep working.

```sh
cd /path/to/project
STORE=/tmp/loan-share-store
S="python3 -m museum.share_cli"   # or: .venv/bin/loan-share

cat > source.json <<'JSON'
{"as_of":"2026-05-15",
 "loans":[{"id":"L1","object_id":"OBJ-1","borrower":"City Gallery","start":"2026-05-01","due":"2026-05-20"},
          {"id":"L2","object_id":"OBJ-1","borrower":"History Hall","start":"2026-05-01","due":"2026-05-20"}],
 "events":[{"id":"E1","loan_id":"L1","type":"dispatch","time":"2026-05-02T09:00:00+00:00","location":"Museum"},
           {"id":"E2","loan_id":"L2","type":"dispatch","time":"2026-05-03T09:00:00+00:00","location":"Museum"},
           {"id":"E3","loan_id":"L1","type":"condition","time":"2026-05-04T10:00:00+00:00","location":"City Gallery","condition_code":"stable"}]}
JSON
req() { cat > "$1"; }   # write stdin to a request file

echo '{"change_id":"C1","collection_id":"M1","recipient":"R1","grant":["L1","L2"],"revoke":[]}' | req r1.json
echo '{"release_id":"P12","collection_id":"M1","recipient":"R1","loan_ids":["L1","L2"],"source":'"$(cat source.json)"'}' | req i12.json
echo '{"release_id":"P1","collection_id":"M1","recipient":"R1","loan_ids":["L1"],"source":'"$(cat source.json)"'}' | req i1.json
echo '{"change_id":"C2","collection_id":"M1","recipient":"R1","grant":[],"revoke":["L2"]}' | req r2.json
echo '{"change_id":"C3","collection_id":"M1","recipient":"R1","grant":["L2"],"revoke":[]}' | req r3.json
echo '{"release_id":"P2","collection_id":"M1","recipient":"R1","loan_ids":["L1","L2"],"source":'"$(cat source.json)"'}' | req i2.json
echo '{"change_id":"C4","collection_id":"M1","recipient":"R1","grant":[],"revoke":["L2"]}' | req r4.json

$S rights --store $STORE --request r1.json    # -> receipt, grant ["L1","L2"]
$S issue  --store $STORE --request i12.json   # -> receipt with report_sha256
$S fetch  --store $STORE --release-id P12 --recipient R1 --output p12.json
$S rights --store $STORE --request r2.json    # revoke L2
$S fetch  --store $STORE --release-id P12 --recipient R1 --output again.json
#   -> exit 2, "loan-share: release P12 has been permanently withdrawn"
$S issue  --store $STORE --request i1.json    # L1-only package: no L2 trace,
$S fetch  --store $STORE --release-id P1 --recipient R1 --output p1.json
#   no cross-scope conflict in p1.json
$S rights --store $STORE --request r3.json    # re-grant L2
$S fetch  --store $STORE --release-id P12 --recipient R1 --output again2.json
#   -> still exit 2: re-granting never revives an old package
$S issue  --store $STORE --request i2.json    # new package P2 is fine
$S fetch  --store $STORE --release-id P2 --recipient R1 --output p2.json
$S rights --store $STORE --request r2.json    # retry of the old revocation:
#   -> same C2 receipt, P2 untouched
$S fetch  --store $STORE --release-id P2 --recipient R1 --output p2b.json
$S rights --store $STORE --request r4.json    # fresh change_id revokes again
$S fetch  --store $STORE --release-id P2 --recipient R1 --output p2c.json
#   -> exit 2; P1 (no L2) still fetches fine
```

### Failure injection (for tests and drills)

`LOAN_SHARE_FAILPOINT="<name>:<action>"` injects a fault at a commit
boundary; actions are `raise` (I/O-style error, exit 2) and `crash`
(immediate process exit, simulating a kill). Names: `commit.before_write`,
`commit.after_replace`.

```sh
echo '{"change_id":"C5","collection_id":"M1","recipient":"R1","grant":["L9"],"revoke":[]}' | req r5.json
LOAN_SHARE_FAILPOINT=commit.after_replace:crash \
  $S rights --store $STORE --request r5.json   # killed after commit (rc 3)
$S rights --store $STORE --request r5.json     # safe retry: first receipt
```

## loan-plan: pre-departure round-trip scheduling

`loan-plan` plans one round of transport for loan batches that have not
departed yet. It reads the loans from an existing audit input (events and
`as_of` are ignored) plus a plan request, and emits both the complete
schedule and the booking changes the carrier should apply. It never simulates
transport already under way. (Without installing, use
`python3 -m museum.plan_cli` in place of `loan-plan`.)

```sh
loan-plan --source INPUT.json --request PLAN.json --output RESULT.json
```

- **Source** is the usual audit input; only `loans` (id, object_id, start,
  due) is used. Loan IDs must be unique and `start <= due`. The start..due
  range (both days included) is when each object may travel.
- **Request** has four arrays. `hubs`: sites where one transfer per leg is
  allowed. `runs`: currently bookable runs (`run_id`, `from_site`, `to_site`,
  `depart`, `arrive`, `capacity`); times are `YYYY-MM-DDTHH:MM:SSZ`, and
  `capacity` is the total space offered for this round — it already includes
  previously booked space, so old bookings are not subtracted again. Stopped
  runs are simply absent. `batches`: `batch_id`, `home_site`, `venue_site`,
  `exhibit_start`, `exhibit_end`, `priority`, and a non-empty `lots` list of
  `{loan_id, units}`; a batch travels together on every run of its route and
  each loan and each object may appear only once in the whole request.
  `previous_bookings`: at most one per batch — `outbound`/`return` (ordered
  run IDs, one or two per leg, no repeats), the `units` previously occupied
  on each of those runs, and `locked`. Locked bookings must be kept exactly;
  unlocked ones may be re-routed or re-sized but their exhibition is
  confirmed and must still get a complete round trip.
- **Rules** for new or kept routes alike: outbound home -> venue arriving no
  later than `exhibit_start`; return venue -> home departing no earlier than
  `exhibit_end`; each leg is direct or transfers once at a hub that is not an
  endpoint of that leg, with a 1..24 hour connection (both ends included);
  every used run's depart and arrive dates must lie inside every loan's
  start..due window; loads of all scheduled batches sum against each run's
  capacity.
- **Selection.** If the confirmed batches cannot all be scheduled (locked
  route stopped, volume changed, overload, timing), the whole round fails —
  no partial plan. Otherwise the plan maximizes the accepted new batches'
  priority sum, then minimizes disturbance of existing bookings (per booked
  batch and run, |new - old| occupancy, unused side counting as zero), then
  takes the lexicographically smallest `(batch_id, outbound, return)`
  sequence by ascending batch ID.
- **Result.** `bookings`: every scheduled batch with `outbound`, `return`,
  and actual `units`, by ascending batch ID. `waitlisted`: new batch IDs that
  did not fit, ascending. `changes`: one non-zero `{batch_id, run_id,
  delta_units}` entry per batch/run pair (positive = book, negative =
  cancel), sorted by batch ID then run ID; applying the whole list to the old
  booking quantities yields exactly the new bookings. The command only
  produces the list; it calls no external system.
- **Exit codes.** Success is `0` and prints a one-line summary; the output
  file is create-only (parent directories are created, existing files are
  never overwritten). Any input, validation, infeasibility, or output error
  exits `2`, writes `loan-plan: ...` to stderr, and leaves no output that
  could be mistaken for a complete schedule.

### Copyable example

B1 is confirmed (booked 2 units on the direct run R4, adjustable); B2 is a
new 5-point request whose loan L2 cannot travel before 2026-06-03, so B2 can
only use R4 — which B1 fills. The planner moves B1 to the hub route R1+R2
(its loan L1 may travel on 2026-06-02) and accepts B2. B1's unchanged return
run R3 gets no cancel/rebook pair.

```sh
cd /path/to/project
P="python3 -m museum.plan_cli"   # or: .venv/bin/loan-plan

cat > source.json <<'JSON'
{"as_of":"2026-05-15",
 "loans":[{"id":"L1","object_id":"OBJ-1","borrower":"City Gallery","start":"2026-06-01","due":"2026-06-30"},
          {"id":"L2","object_id":"OBJ-2","borrower":"History Hall","start":"2026-06-03","due":"2026-06-30"}],
 "events":[]}
JSON
cat > plan.json <<'JSON'
{"hubs":["HUB"],
 "runs":[{"run_id":"R1","from_site":"HOME","to_site":"HUB","depart":"2026-06-02T09:00:00Z","arrive":"2026-06-02T12:00:00Z","capacity":4},
         {"run_id":"R2","from_site":"HUB","to_site":"VENUE","depart":"2026-06-02T14:00:00Z","arrive":"2026-06-02T18:00:00Z","capacity":4},
         {"run_id":"R3","from_site":"VENUE","to_site":"HOME","depart":"2026-06-25T09:00:00Z","arrive":"2026-06-25T17:00:00Z","capacity":4},
         {"run_id":"R4","from_site":"HOME","to_site":"VENUE","depart":"2026-06-03T09:00:00Z","arrive":"2026-06-03T17:00:00Z","capacity":2}],
 "batches":[{"batch_id":"B1","home_site":"HOME","venue_site":"VENUE","exhibit_start":"2026-06-05T00:00:00Z","exhibit_end":"2026-06-20T00:00:00Z","priority":1,"lots":[{"loan_id":"L1","units":2}]},
            {"batch_id":"B2","home_site":"HOME","venue_site":"VENUE","exhibit_start":"2026-06-06T00:00:00Z","exhibit_end":"2026-06-19T00:00:00Z","priority":5,"lots":[{"loan_id":"L2","units":2}]}],
 "previous_bookings":[{"batch_id":"B1","outbound":["R4"],"return":["R3"],"units":2,"locked":false}]}
JSON

$P --source source.json --request plan.json --output run/result.json
#   -> stdout: {"bookings": 2, "changes": 5, "waitlisted": 0}
#   run/result.json: B1 rerouted via ["R1","R2"] + return ["R3"], B2 on
#   ["R4"] + ["R3"]; changes: B1/R1 +2, B1/R2 +2, B1/R4 -2, B2/R3 +2,
#   B2/R4 +2.
$P --source source.json --request plan.json --output run/result.json
#   -> exit 2, "loan-plan: output already exists: run/result.json"
```

## Verification

Executed on Python 3.11.2 from the project root:

```text
$ python3 -m unittest discover -s tests -v
... 122 tests (13 pre-existing loan-audit tests unchanged, 43 loan-share
library and CLI tests, 38 loan-plan library and CLI tests, 28 new
loan-deposit library and CLI tests) ...
Ran 122 tests in ~3s
OK
```

The loan-deposit suites are `tests/test_deposit.py` (reconciliation and
settlement logic, testable without any file I/O) and
`tests/test_deposit_cli.py` (CLI via independent `python3 -m
museum.deposit_cli` processes). Together they demonstrate: the maximum-matching
case where a row with more candidates must not steal the only instruction a
sibling row can settle; the inclusive 48-hour window at both boundaries;
manual confirmation of a payment outside the automatic window; interchangeable
same-payer rows blocking every involved loan's refund suggestions until one
manual link forces the rest; two bank rows competing for one instruction
staying ambiguous even though each has only that one surface candidate; a
brute-force cross-check of the Dulmage-Mendelsohn classification against full
enumeration on 1000 random small graphs; forty interchangeable rows handled
without enumerating explanations and unchanged after shuffling; raised fees
splitting the cash top-up from the in-transit excess; multi-deposit loans
split into per-deposit suggestions in receipt order, independent of blocked
partners; and whole-batch rejection (with the existing output preserved) for
conflicting manual links and instructions exceeding the deposit.

The loan-plan suites are `tests/test_plan.py` (planning logic) and
`tests/test_plan_cli.py` (CLI via independent `python3 -m museum.plan_cli`
processes). Together they demonstrate: a priority-8 application that would
consume a tight run losing to two priority-5 applications that share it, and
losing again when one of the pair cannot complete its return leg (return
capacity or loan end date), so a half-planned application never counts; a
confirmed adjustable batch re-routed through a hub to admit one more new
batch, with its unchanged runs left out of the change list; equal-score
plans broken by booking disturbance and then by the lexicographic plan
order; stopped runs and changed volumes on adjustable bookings producing the
correct net changes; locked bookings kept exactly, and the whole round
failing when a locked route is stopped, a locked volume changed, or two
must-keep bookings overload a run no matter how they are adjusted; cross-day
loan date edges and 1/24-hour transfer-wait edges (both ends inclusive);
identical results when input records are reordered; create-only output that
never overwrites an existing file; and validation errors that name the
failing location.

The loan-share suites are `tests/test_share.py` (library) and
`tests/test_share_cli.py` (CLI via independent `python3 -m museum.share_cli`
processes). Together they demonstrate: the full grant → issue → revoke →
re-grant lifecycle above; permanent withdrawal of combined packages with no
cross-loan leakage in scoped ones; old `change_id` retries not revoking new
grants while a fresh `change_id` does; per-collection and per-recipient
isolation of same-named loans; frozen bodies immune to later source edits;
conflicting duplicate credentials rejected; broken source records outside
the shared scope failing the issue; state recovery across process restarts;
injected write failures and post-commit crashes staying atomic (no split
between a rights change and package invalidation, or between an issue
credential and its frozen body); and create-only output protection.
