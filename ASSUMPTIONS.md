# Requirements Clarification and Assumptions

Each entry follows the format requested in the challenge brief (§7): the issue identified, the assumption made, its
effect, and what would change if the assumption were not valid. A **Status** line states what the code in this
repository does today, so the document can be checked against it.

---

## A1. Meaning of "the same product must never be sent twice"

**Issue.**
The WMS offers no idempotency key and no lookup endpoint, so the guarantee must be enforced entirely by the
integration. The statement can mean any of:

1. a product must not be sent more than once within a run, including when the run is resumed after a failure;
2. a product that has not changed must not be re-sent on later days;
3. a request whose outcome is unknown (timeout, gateway error) must not be blindly retried.

**Assumption.**
All three apply. "The same product" means the same SKU with the same content; a product whose data changed in the
PIM is a new version and must be sent.

- **Within a run:** the ledger records every batch (`IN_FLIGHT` → `SUCCEEDED` / `PARTIAL` / `FAILED` / `AMBIGUOUS`).
  Resuming a run skips confirmed batches and never resends an `IN_FLIGHT` or `AMBIGUOUS` one.
- **Across runs (delta sync):** the ledger keeps one record per SKU with a SHA-256 fingerprint of the exact WMS
  payload. A product is sent only if it is new or its fingerprint changed.
- **Unknown outcomes are at-most-once:** each product is recorded as `SENT` *before* its batch is posted and updated to
  `ACCEPTED` / `REJECTED` afterwards. If the outcome is never confirmed (crash, timeout), the `SENT` record stays and
  later runs treat the product as possibly delivered. Only failures that certainly did not reach the WMS
  (see A12) are recorded as `NOT_SENT` and sent again.

**Effect.**
- Duplicates are prevented at the cost of possibly delaying, never silently resending, products in an ambiguous batch.
  Those need operator reconciliation.
- Daily WMS traffic is proportional to the number of changed products, not to the catalogue size.

**Status.** Implemented (`app/services/warehouse_sync.py`, both ledgers) and covered by `tests/test_delivery.py`,
including with several batches in flight. Duplicate SKUs within one export are dropped by the export (A3).

**If the assumption is not valid.**
- *If the WMS upserts by SKU (at-least-once is acceptable):* retry ambiguous batches automatically and drop the
  reconciliation step.
- *If the WMS adds an idempotency key or a lookup endpoint:* send `run_id` + batch number as the key, or query the WMS,
  and resolve ambiguous batches automatically.
- *If a full resend is sometimes required (e.g. the WMS is restored from backup):* add an operator "full resync" option
  that ignores the fingerprints.

---

## A2. Handling of business-validation rejections

**Issue.**
The WMS can accept part of a batch and reject the rest. It is undefined whether rejected products should be retried,
and whether rejections should fail a run.

**Assumption.**
Rejections are data problems that retrying unchanged data will not fix. A rejected product is recorded as `REJECTED`
with its fingerprint and is re-sent only when its PIM data changes. Per-batch rejections and reasons are kept in the
ledger and the run ends as `COMPLETED_WITH_REJECTIONS`.

**Effect.** Known-bad payloads are not sent to the WMS every day; operations can list rejected SKUs from the ledger.

**Status.** Implemented. Not implemented: a rejection-rate circuit breaker that stops a run when a large share of
products is rejected (a sign of a systemic transformation bug rather than bad data).

**If the assumption is not valid.**
- *If rejections can be transient (e.g. reference data not loaded yet):* re-send `REJECTED` products on every run, or
  classify WMS reason codes into retryable and permanent.
- *If a high rejection rate must stop the run:* add the circuit breaker with a configurable threshold.

---

## A3. Consistency of the paginated PIM export

**Issue.**
The PIM paginates by page number with no snapshot or cursor, so products created or deleted during an export can shift
page boundaries and be skipped or returned twice.

**Assumption.**
The PIM is mostly stable during the sync window. A product skipped by a page shift is picked up by the next run's delta
comparison. The WMS sync starts only from a complete export: the CSV is written to a `.partial` file and is published
and renamed only after the last page.

**Effect.** A product may be up to one day late in the worst case; a half-finished export is never synced.

**Status.** "Sync only from a complete export" and de-duplication by SKU (first occurrence wins, logged as
`duplicates_dropped`) are implemented and tested. Not implemented: a warning when the row count differs from the
PIM's `total`.

**If the assumption is not valid.** Detect count drift and re-export, move the schedule to a quieter window, or ask
the PIM team for cursor-based pagination or an `updated_since` filter.

---

## A4. Run identity and re-triggers

**Issue.** Each run used a new random ID, so re-running after a failure exported and sent the whole catalogue again.

**Assumption.**
There is one logical run per business day. The run ID defaults to the UTC date (`YYYY-MM-DD`) and can be set with
`--run-id` or `RUN_ID`. Re-triggering a run reuses its export (from local disk, or downloaded from S3 on a fresh
container) and resumes from the batch ledger.

In AWS, Step Functions sets `RUN_ID` from the execution's start date (or `runId` in the execution input), so every
retry inside one execution resumes the same run even if it crosses midnight.

**Effect.** Recovery from an interruption is a plain re-trigger, which the workflow does automatically on exit code 1.

**Status.** Implemented and tested. Not implemented: a run lease. Two processes on the same run cannot both claim a
batch (conditional writes), but the second one stops with exit code 2 instead of waiting (UNFINISHED.md #5).

**If the assumption is not valid.** If several syncs per day are needed, pass explicit run IDs (e.g. a timestamp).
The delta comparison still prevents re-sending unchanged products.

---

## A5. Rate-limit budget

**Issue.** It is not stated whether the documented limits (PIM 10 req/s, WMS 20 req/s) are shared with other clients.

**Assumption.** This integration owns the budget and targets 90% of it (PIM 9 req/s, WMS 18 req/s, configurable).
Up to 8 requests per API are in flight, all sharing that API's limiter. A throttled request waits at least
`Retry-After` (capped at 30 s) plus up to 25% jitter; the other requests carry on at the limiter's rate.

**Effect.** Throughput is set by the rate limits rather than by latency. Pausing *all* requests on every
`Retry-After` was tried and dropped: against the mock, which throttles a fixed share of requests regardless of rate,
it cut WMS throughput from about 13 to 3.5 batches/s.

**If not valid.** If the limits are shared with other clients, lower the configured rates. If the real WMS throttles
because of overall load, replace the fixed limiter with an adaptive one (cut the rate on each 429, raise it slowly
after successes). If work were spread over several processes, a shared limiter (e.g. a DynamoDB token bucket) would
be needed.

---

## A6. Monetary precision

**Issue.** Prices are converted to `float`, which can introduce binary rounding errors.

**Assumption.** The WMS accepts a JSON number for `selling_price`, and the PIM prices have at most two decimal places,
which `float` round-trips exactly through JSON. Fingerprints are therefore stable.

**Status.** Prices are still `float`.

**If not valid.** Parse prices as `Decimal` and quantise to the WMS's precision; only `transform` and its tests change.
Changing the payload changes every fingerprint, so the next run would resend the full catalogue once.

---

## A7. Products deleted from the PIM

**Issue.** The WMS API has no delete or deactivate operation.

**Assumption.** Propagating deletions is out of scope.

**Status.** Not implemented. Not reported either; SKUs in the ledger but missing from the export could be listed per
run.

**If not valid.** If the WMS adds a delete endpoint, send deletions as a separate, also ledgered, step.

---

## A8. Invalid source records

**Issue.** A row with a missing field or an unparseable price raises an error in `transform`.

**Assumption (current behaviour).** Fail fast: the run stops at the bad row and is marked `FAILED`. Re-triggering the
same run reuses its export and stops at the same row, so after the PIM data is fixed the operator starts a new run
(`--run-id 2026-10-22-2`). Delta sync skips the products the failed run already sent.

**Effect.** Bad data never reaches the WMS, but one bad record blocks the rest of the day's sync.

**If not valid.** Record invalid rows as local rejections and continue, with a threshold that still fails the run when
many rows are invalid.

---

## A9. PIM schema drift

**Issue.** The PIM may add fields.

**Assumption.** The CSV keeps the documented columns; unknown fields are dropped (`extrasaction="ignore"`).
Missing required fields are handled by A8.

**Effect.** Additive PIM changes do not break the sync, but they are dropped silently.

**If not valid.** Log new field names as a warning, or keep the raw PIM pages alongside the CSV.

---

## A10. Data sensitivity and security baseline

**Issue.** The brief does not classify the catalogue data.

**Assumption.** Product data is commercially sensitive but not personal data. Baseline: S3 encryption at rest, S3
public access blocked, API keys in AWS Secrets Manager, least-privilege IAM, TLS for external calls.

**Status.** The Terraform uses SSE-S3 (`AES256`) and injects keys from Secrets Manager. `app/config.py` still falls
back to the mock API keys when the variables are unset, which is convenient locally but should fail in production.

**If not valid.** For PII or regulated data, use SSE-KMS with a customer key, private networking, and access-log
review.

---

## A11. First run and the 30-minute window

**Issue.** On the first run every product is new, so all 1M products must be sent.

**Measured.** Against the mocks with 250,000 products, the export took 97 s (500 pages) and the sync took 183 s, about
13.7 batches/s. About 15% of mock responses are throttles, which take up rate-limit slots. Extrapolated to 1M: export
≈ 6.5 min plus sync ≈ 12 min ≈ 19 min. Daily runs after the first only send changed products.

**Assumption.** The real APIs' latency stays under about 0.45 s per request, so 8 requests in flight are enough to
reach the rate limits. DynamoDB ledger writes keep pace with about 13 batches/s.

**If not valid.** Raise `*_MAX_IN_FLIGHT` for higher latency. If ledger writes become the bottleneck, move outcome
writes into the worker threads (UNFINISHED.md #4). If the first load still exceeds the window, run it as a one-off
backfill.

---

## A12. Which WMS failures mean "not applied"

**Issue.**
A POST that fails may or may not have been processed. Retrying a processed batch creates duplicates; treating every
failure as ambiguous would block the run on routine throttling. The mocks return HTTP 429 (every 11th request) and
HTTP 503 (every 17th), both with `Retry-After`, before processing anything.

**Assumption.**
- **Not applied, retried with backoff:** HTTP 429 and HTTP 503. Both say the service refused the request.
- **Not applied, not retried:** other 4xx (bad request, authentication, batch too large) and connection failures
  before the request was sent. The batch is marked `FAILED` and is sent again when the run is resumed.
- **Ambiguous, never retried automatically:** HTTP 500, 502 and 504, timeouts and dropped connections after sending.
  The batch is marked `AMBIGUOUS` and the run stops for reconciliation.
- A 429/503 that is still failing after 5 attempts is `FAILED`, not ambiguous.

**Effect.** Runs against the mocks complete without manual work. A real outage stops the run with a precise list of
batches to check.

**Status.** Implemented in `app/clients/warehouse_api.py` (`WMS_RETRYABLE_STATUS_CODES`, `was_not_applied`) and tested.

**If not valid.**
- *If the real WMS can return 503 after partly applying a batch:* remove 503 from the retryable set (a one-line change,
  covered by tests).
- *If the WMS is known to roll back on 500:* make 500 retryable too.
