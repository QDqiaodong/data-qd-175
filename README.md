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

## Verification

Executed on Python 3.11.2 from the project root:

```text
$ python3 -m unittest discover -s tests -v
... 56 tests (13 pre-existing loan-audit tests unchanged, 43 new
loan-share library and CLI tests) ...
Ran 56 tests in ~2s
OK
```

The new suites are `tests/test_share.py` (library) and
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
