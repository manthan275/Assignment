# Unfinished work and next steps

Everything below is known and deliberately left open; nothing here is hidden behind a passing test. Priorities:
**P0** before production, **P1** within the first weeks, **P2** soon after, **P3** when convenient.

| # | Item | Type | Priority |
|---|---|---|---|
| 1 | Validation in a real AWS account | Environment limitation | P0 |
| 2 | Reconciliation tooling for ambiguous batches | Deliberate prioritisation | P1 |
| 3 | Invalid source rows stop the run; no rejection-rate circuit breaker | Deliberate prioritisation | P1 |
| 4 | Load test at 1M with realistic latency, including DynamoDB write throughput | Environment limitation | P1 |
| 5 | Overlapping executions of the same run | Deliberate prioritisation | P2 |
| 6 | Production configuration guard (mock API keys as defaults) | Deliberate prioritisation | P2 |
| 7 | Operator full-resync option | Deliberate prioritisation | P2 |
| 8 | CI pipeline | Deliberate prioritisation | P2 |
| 9 | Data-quality details: deleted products, `Decimal` prices, schema drift warnings | Deliberate prioritisation | P3 |
| 10 | Ledger growth: expiry of old batch records | Deliberate prioritisation | P3 |
| 11 | WMS idempotency key or lookup endpoint | External dependency | P2 (request now) |

---

## 1. Validation in a real AWS account

- **What remains:**
  - The Terraform passes `terraform fmt` and `terraform validate` (Terraform 1.9.8, AWS provider 5.x), but has never
    been planned or applied against an account.
  - Three behaviours have only been checked against documentation:
    - the Step Functions exit-code routing, which reads `$.Containers[0].ExitCode`;
    - EMF metric extraction through the `awslogs` driver;
    - the IAM policies, in particular S3 `HeadObject` returning 404 rather than 403 with the `ListBucket` grant.
  - The S3 adapter is tested with fakes, and the DynamoDB adapter against moto.
- **Proposed solution:** Apply to a sandbox account with `example.tfvars` and real VPC/secret values. Then run four
  executions:
  - a normal run;
  - a run whose task is stopped mid-sync;
  - a run against a WMS stub returning 500;
  - a manual run with `{"runId": "..."}`.
- **Validation:**
  - The first execution succeeds.
  - The stopped one resumes and succeeds, with no duplicate batch claims in the ledger.
  - The 500 run ends in `NeedsReconciliation` and the SNS alert arrives.
  - The `CatalogueSync` metrics appear in CloudWatch.
- **Why not done:** No AWS account was available for this exercise. The brief says deployment is not required.
- **Type:** Environment limitation.
- **Risk:** A misnamed field in the state machine or a missing IAM action would fail the first scheduled run. The
  run would fail safely, with nothing sent twice, but the day's sync would be missed.
- **Priority:** P0.

## 2. Reconciliation tooling for ambiguous batches

- **What remains:**
  - An `AMBIGUOUS` (or crash-left `IN_FLIGHT`) batch stops the run with exit code 2 and an alert, which is correct.
  - But the only way to release it is to edit the ledger by hand.
- **Proposed solution:** Add `python -m app.reconcile --run-id … --batch N` with two actions:
  - `--resend` marks the batch `FAILED` and its products `NOT_SENT`;
  - `--delivered` marks the batch and its products as delivered.

  Both require the operator to confirm the batch's SKUs, and both record who did it and why. A `list` action shows
  every unreconciled batch across runs.
- **Validation:**
  - Unit tests confirm each action leads to exactly one send, or none, on the next run.
  - A sandbox drill: inject a WMS timeout, reconcile, resume.
- **Why not done:** The safety behaviour, stopping and never resending, came first. The tooling is operational
  convenience on top of it.
- **Type:** Deliberate prioritisation.
- **Risk:** Slower recovery and a risk of manual mistakes during an incident. A wrong manual edit could cause a
  duplicate.
- **Priority:** P1.

## 3. Invalid source rows stop the run; no rejection-rate circuit breaker

- **What remains:**
  - A row with a missing field or an unparseable price stops the whole run (ASSUMPTIONS.md A8).
  - There is no check that stops a run when the WMS rejects an unusually large share of products, which would be a
    sign of a transformation bug rather than bad data (A2).
- **Proposed solution:**
  - Record invalid rows as local rejections (status `INVALID`, with the reason) and continue.
  - Stop the run once the invalid or rejected share exceeds a threshold (e.g. 5% after at least 1,000 products).
  - Emit `ProductsInvalid` and alarm on it.
- **Validation:** Tests with a few bad rows (the run completes and reports them) and with many (the run stops before
  sending more). Mutation-test the threshold.
- **Why not done:** Prioritised duplicate safety, resume and throughput.
- **Type:** Deliberate prioritisation.
- **Risk:**
  - One bad PIM record blocks the day's sync until it is fixed, which needs a new run ID (A8).
  - A systemic transformation bug would be sent to the whole catalogue before anyone notices.
- **Priority:** P1.

## 4. Load test at 1M with realistic latency

- **What remains:**
  - Measured: 250,000 products against the mocks (export 97 s, sync 183 s at about 13.7 batches/s). The 1M figure
    (≈ 19 min) is an extrapolation.
  - The mocks answer in milliseconds, and the local ledger is SQLite.
  - In AWS each batch makes about 12 DynamoDB calls from the main thread: a lookup, the claim, two batched product
    writes and the finish. At 5–10 ms per call, that could cap delivery at 10–15 batches/s, close to the WMS limit.
- **Proposed solution:**
  - Run the smoke test against a latency-injecting proxy (300 ms PIM, 400 ms WMS) and a real DynamoDB table with
    1M products.
  - If DynamoDB writes become the bottleneck, move the outcome writes into the worker threads, giving each thread
    its own boto3 resource. The claim and `SENT` writes stay on the main thread, so the crash-safety ordering is
    unchanged.
- **Validation:** The run finishes in under 25 minutes, and the `RunDurationSeconds` alarm stays quiet.
- **Why not done:** It needs AWS and a realistic latency model; neither was available.
- **Type:** Environment limitation.
- **Risk:** A first full load or full resync could exceed 30 minutes. Daily delta runs are much smaller and not at
  risk.
- **Priority:** P1.

## 5. Overlapping executions of the same run

- **What remains:**
  - A manual execution started while the scheduled one is still running uses the same run ID.
  - Conditional claims stop both from sending the same batch.
  - But the second process stops with an `AmbiguousBatchError` (exit 2) when it meets a batch the first one holds,
    which raises a false reconciliation alert.
- **Proposed solution:**
  - Take a lease on the run record (conditional write with an owner and an expiry, renewed every minute). A second
    process exits with a distinct code that Step Functions treats as "already running".
  - Alternatively, start executions with a name derived from the run ID, so Step Functions itself rejects a
    duplicate.
- **Validation:** Start two executions at once in the sandbox. One succeeds, the other ends as "already running",
  and no batch is sent twice.
- **Why not done:** Duplicates are already prevented; this only improves the operator experience.
- **Type:** Deliberate prioritisation.
- **Risk:** A misleading alert. No duplicate is possible.
- **Priority:** P2.

## 6. Production configuration guard

- **What remains:** `app/config.py` falls back to the mock API keys and localhost URLs when variables are unset.
- **Proposed solution:** When `STORAGE_BACKEND=aws`, require every URL and key to be set explicitly and fail at
  start-up otherwise.
- **Validation:** A unit test for each missing variable; a container started without secrets exits immediately with
  a clear message.
- **Why not done:** Convenient for local runs; Terraform always sets the variables.
- **Type:** Deliberate prioritisation.
- **Risk:** A misconfigured deployment could call localhost or send a mock key, and fail confusingly.
- **Priority:** P2.

## 7. Operator full-resync option

- **What remains:** There is no way to resend the whole catalogue, for example after the WMS is restored from backup
  and has lost data.
- **Proposed solution:** Add `--full-resync`, which ignores fingerprints from previous runs but keeps every other
  safeguard. Record the option on the run so a resume behaves the same way.
- **Validation:** A test where day 2 with `--full-resync` sends every product once, and a resume of that run sends
  nothing twice.
- **Why not done:** Not needed for the daily flow.
- **Type:** Deliberate prioritisation.
- **Risk:** Recovering from WMS data loss would need manual ledger changes.
- **Priority:** P2.

## 8. CI pipeline

- **What remains:** No automated pipeline. Tests, the smoke test and `terraform validate` are run by hand.
- **Proposed solution:** A GitHub Actions workflow with the following steps:
  - `ruff` and `mypy`;
  - `pytest`;
  - `scripts/smoke_test.py`;
  - `terraform fmt -check` and `terraform validate`;
  - `tflint` / `checkov`;
  - build the image and push it to ECR tagged by commit SHA.
- **Validation:** A failing test or an invalid Terraform change blocks the merge.
- **Why not done:** Repository hosting and CI were out of scope for the exercise.
- **Type:** Deliberate prioritisation.
- **Risk:** Regressions reach the main branch unnoticed.
- **Priority:** P2.

## 9. Data-quality details

- **What remains:**
  - Deleted products: the WMS has no delete API, and SKUs missing from the export are not even reported (A7).
  - Prices are `float` rather than `Decimal` (A6).
  - Unknown PIM fields are dropped silently (A9).
  - The export does not compare its row count with the PIM's `total`.
- **Proposed solution:**
  - A per-run report of SKUs in the ledger but absent from the export.
  - `Decimal` parsing, quantised to two places.
  - A warning listing new field names.
  - A warning metric when the row count differs from `total`.
- **Validation:** A unit test for each, plus a check that changing the price type changes fingerprints only once.
- **Why not done:** Lower risk than delivery safety; each needs a business decision before it can be finished.
- **Type:** Deliberate prioritisation.
- **Risk:** Stale products remain in the WMS; possible rounding differences; silent loss of new PIM fields.
- **Priority:** P3.

## 10. Ledger growth

- **What remains:** Batch records are kept forever: about 10,000 a day on a full load, far fewer on delta days.
- **Proposed solution:** A DynamoDB TTL attribute on batch and run records (e.g. 180 days). Product records must not
  expire.
- **Validation:** A test that only batch and run items carry the TTL attribute.
- **Why not done:** Storage cost is negligible in year one.
- **Type:** Deliberate prioritisation.
- **Risk:** Slowly growing storage cost.
- **Priority:** P3.

## 11. WMS idempotency key or lookup endpoint

- **What remains:**
  - The WMS offers neither an idempotency key nor a lookup endpoint, so ambiguous outcomes cannot be resolved
    automatically.
  - It is also an assumption that a WMS 503 means "not processed" (A12).
- **Proposed solution:** Ask the WMS team for one of two things:
  - an `Idempotency-Key` header (we would send `run_id` + batch number);
  - `GET /products/{sku}`.

  Either one lets the client retry or check ambiguous batches automatically, which removes item 2's manual step.
  Also confirm the 503 semantics with them.
- **Validation:** A contract test against their sandbox: replaying the same key applies a batch once.
- **Why not done:** Owned by another team.
- **Type:** External dependency.
- **Risk:** Manual reconciliation remains; if 503 can follow a partial write, a retry could duplicate.
- **Priority:** P2, with the request raised now.
