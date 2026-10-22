# Product Catalogue Integration

Daily synchronisation of the product catalogue from the PIM into the WMS. It is designed for 1M products in a
30-minute window, keeps every export for 90 days, and never sends the same product twice.

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Diagram, components, execution, data and failure flows, scaling, AWS choices, security, observability, trade-offs |
| [ASSUMPTIONS.md](ASSUMPTIONS.md) | How ambiguous requirements were interpreted (A1–A12) |
| [UNFINISHED.md](UNFINISHED.md) | What is not done, why, the risk, and the next step |
| [ENGINEERING_CHALLENGE.md](ENGINEERING_CHALLENGE.md) | The original brief |

## How it works in one paragraph

`python -m app.main` runs two stages for one run ID (by default today's UTC date):

1. **Export.** Stream the PIM, up to 8 pages in flight within its rate limit, into a CSV. Publish it under
   `exports/<run-id>/catalogue.csv` only when complete.
2. **Sync.** Send only new or changed products to the WMS, up to 8 batches of 100 in flight within its rate limit.

A ledger (SQLite locally, DynamoDB in AWS) records every batch and every product's content hash, written *before*
each send. Re-running the same run ID therefore resumes it, and a later run skips unchanged products. Responses that
prove a batch was not applied (429, 503, other 4xx) are retried or resent. Anything with an unknown outcome is never
resent automatically.

## Repository layout

```
app/
  main.py                        CLI entry point, exit codes, run summary and metrics
  config.py                      settings from environment variables
  observability.py               JSON logs, CloudWatch Embedded Metric Format
  clients/
    http_utils.py                thread-safe rate limiter, retry loop, Retry-After handling
    product_api.py               PIM client: concurrent pages, yielded in order
    warehouse_api.py             WMS client: which failures are safe to retry
  services/
    catalogue_export.py          PIM → CSV → S3; reuse of a published export; SKU de-duplication
    warehouse_sync.py            transform, skip unchanged, batch, concurrent send, record outcomes
    sync_ledger.py               SQLite ledger (local)
    dynamodb_sync_ledger.py      DynamoDB ledger (AWS)
  storage/s3.py                  local folder or S3
tests/                           unit and integration tests (no network, Docker or AWS needed)
scripts/smoke_test.py            end-to-end run against the real mock APIs over HTTP
terraform/                       AWS infrastructure (see "Terraform" below)
mock-services/                   supplied PIM and WMS mocks (unmodified by this work)
Dockerfile                       production image (runtime dependencies only, non-root)
docker-compose.yml               mock APIs, plus DynamoDB Local under the aws-integration profile
```

## Prerequisites

- Python 3.11 or newer (developed on 3.12)
- Optional: Docker, to run the mocks with Docker Compose or the DynamoDB Local test
- Optional: Terraform 1.5 or newer, to validate or plan the infrastructure

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate                 # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt       # runtime + mocks + test tools
```

`requirements.txt` holds only the runtime dependencies that go into the container image.

## Run the tests

```bash
pytest -q
```

This runs 63 tests in a few seconds, with no network, Docker or AWS account needed. The DynamoDB ledger runs against
moto's in-memory DynamoDB. The tests cover:

- **WMS failure rules:** 429 and 503 are retried; 500, 502, 504 and timeouts are never resent; other 4xx and
  connection failures count as "not applied".
- **Never sent twice:**
  - re-triggering a finished run sends nothing;
  - the next day sends only new and changed products;
  - rejected products are resent only when they change;
  - an ambiguous batch is not resent by the same run or a later one;
  - a crash mid-request is not resent by a later run;
  - a resume stops if its batches differ from the ledger.
- **Concurrency:**
  - every product is sent exactly once with 4 batches in flight;
  - a failure stops new work and settles the batches already in flight;
  - a failed batch is resent on resume without duplicates.
- **Export:**
  - it is reused on re-trigger, and restored from storage on a fresh container;
  - it is never published if the PIM fails mid-way;
  - duplicate SKUs are dropped;
  - unsafe run IDs are rejected.
- **PIM paging:** pages are fetched concurrently but yielded in order; the pagination cursor is validated.
- **Ledgers:** conditional claims, and re-claiming a `FAILED` batch only (SQLite and DynamoDB).
- **Exit codes, JSON logs, EMF metrics.**

I also deliberately broke six of the core safety rules, one at a time; each time at least one test failed.

## Run end to end (no Docker needed)

```bash
python scripts/smoke_test.py
```

The script starts both mock APIs with uvicorn on free ports and uses a temporary workspace. It then runs the real
CLI three times, re-running on exit code 1 just as Step Functions does:

```
--- day-1 (attempt 1): exit 0 in 279.8s
    batches_sent_this_attempt=2500, accepted_products=249750, rejected_products=250, unchanged_products=0, status=completed_with_rejections
--- day-1 (attempt 1): exit 0 in 1.7s
    batches_sent_this_attempt=0, accepted_products=249750, rejected_products=250, unchanged_products=0, status=completed_with_rejections
--- day-2 (attempt 1): exit 0 in 108.7s
    batches_sent_this_attempt=0, accepted_products=0, rejected_products=0, unchanged_products=250000, status=completed
smoke test passed
```

1. **day-1:** a full load of the mock's 250,000 products.
2. **day-1 again:** a re-trigger. The export is reused and nothing is sent.
3. **day-2:** a new export. Every product is unchanged, so nothing is sent.

This output is from the 250,000-product mock on an Apple-silicon laptop; timings vary by machine. Use `--keep` to keep the workspace (exports, ledger, mock logs) for inspection.

## Run it manually

**1. Start the mock APIs**, either with Docker:

```bash
docker compose up --build                 # PIM on :8001, WMS on :8002 (Swagger UI at /docs)
```

or without Docker, in two terminals:

```bash
(cd mock-services/product-api   && uvicorn app.main:app --port 8001)
(cd mock-services/warehouse-api && uvicorn app.main:app --port 8002)
```

**2. Run the sync** from the repository root. The defaults point at the local mocks, the local storage folder and
SQLite.

```bash
LOG_FORMAT=text python -m app.main                 # today's run (run ID = UTC date)
LOG_FORMAT=text python -m app.main --run-id day-2  # a specific run; re-running it resumes it
echo $?                                            # 0 ok · 1 failed, re-run to resume · 2 needs reconciliation
```

**3. Inspect the results.**

| What | Where |
|---|---|
| Export | `runtime/exports/<run-id>/catalogue.csv` |
| Local stand-in for S3 | `runtime/s3/exports/<run-id>/catalogue.csv` |
| Ledger | `runtime/sync-ledger.sqlite3` |

```bash
sqlite3 runtime/sync-ledger.sqlite3 "select run_id, status from sync_runs;"
sqlite3 runtime/sync-ledger.sqlite3 "select status, count(*) from sync_batches group by status;"
sqlite3 runtime/sync-ledger.sqlite3 "select status, count(*) from product_state group by status;"
```

Delete `runtime/` to start from scratch.

## Configuration

All settings are environment variables (see `app/config.py` and `.env.example`).

| Variable | Default | Purpose |
|---|---|---|
| `PRODUCT_API_URL`, `PRODUCT_API_KEY` | `http://localhost:8001`, mock key | PIM endpoint and key |
| `WAREHOUSE_API_URL`, `WAREHOUSE_API_KEY` | `http://localhost:8002`, mock key | WMS endpoint and key |
| `RUN_ID` | today's UTC date | Logical run to start or resume (or `--run-id`) |
| `PIM_REQUESTS_PER_SECOND`, `WMS_REQUESTS_PER_SECOND` | 9, 18 | Client-side rate limits (90% of documented) |
| `PIM_MAX_IN_FLIGHT`, `WMS_MAX_IN_FLIGHT` | 8, 8 | Concurrent requests per API |
| `HTTP_MAX_ATTEMPTS`, `HTTP_TIMEOUT_SECONDS` | 5, 30 | Retry attempts and per-request timeout |
| `STORAGE_BACKEND`, `S3_BUCKET` | `local`, none | `aws` uses S3 |
| `SYNC_LEDGER_BACKEND`, `DYNAMODB_LEDGER_TABLE` | `sqlite`, none | `dynamodb` uses the DynamoDB ledger |
| `EXPORT_DIR`, `S3_LOCAL_DIR`, `SYNC_LEDGER_PATH` | under `./runtime/` | Local paths |
| `LOG_FORMAT`, `LOG_LEVEL` | `json`, `INFO` | `text` is easier to read locally |

## Optional: DynamoDB Local integration test

```bash
docker compose --profile aws-integration up -d dynamodb-local
DYNAMODB_LOCAL_ENDPOINT=http://localhost:8003 pytest -q tests/test_dynamodb_local_integration.py
```

## Terraform

The `terraform/` directory describes the AWS deployment in [ARCHITECTURE.md](ARCHITECTURE.md):

| File | Contents |
|---|---|
| `storage.tf` | S3 exports bucket and DynamoDB ledger table |
| `compute.tf` | ECR, ECS cluster and Fargate task, log group |
| `iam.tf` | Execution, task, Step Functions and EventBridge roles |
| `orchestration.tf` | State machine and daily schedule |
| `monitoring.tf` | SNS topic and alarms |

The configuration passes `terraform validate`. It has not been applied to an account (UNFINISHED.md #1).

```bash
cd terraform
terraform init -backend=false
terraform fmt -check && terraform validate
terraform plan -var-file=example.tfvars      # needs AWS credentials; replace the example values first
```

To deploy:

1. Create the two API-key secrets in Secrets Manager.
2. Build and push the image:
   ```bash
   docker build -t <ecr-url>:<git-sha> . && docker push <ecr-url>:<git-sha>
   ```
3. Set `container_image`, the VPC, the subnets and the secret ARNs in a tfvars file, then run `terraform apply`.

The ECR repository is created by the same configuration. On a first deployment, either apply it alone first with
`-target=aws_ecr_repository.catalogue_sync`, or push to an existing registry.

To run a specific day by hand, start an execution with input `{"runId": "2026-10-22"}`. Without it, the execution
date is used.

## Exit codes

| Code | Meaning | Step Functions action |
|---|---|---|
| 0 | Succeeded (rejections are reported, not failures) | Succeed |
| 1 | Failed in a way that is safe to re-run | Wait 2 minutes and re-run the same run ID, up to 3 attempts |
| 2 | A batch's outcome is unknown | Fail with `NeedsReconciliation` and alert |
