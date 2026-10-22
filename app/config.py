import os

PRODUCT_API_URL = os.getenv("PRODUCT_API_URL", "http://localhost:8001")
WAREHOUSE_API_URL = os.getenv("WAREHOUSE_API_URL", "http://localhost:8002")
PRODUCT_API_KEY = os.getenv("PRODUCT_API_KEY", "challenge-product-key")
WAREHOUSE_API_KEY = os.getenv("WAREHOUSE_API_KEY", "challenge-warehouse-key")
EXPORT_DIR = os.getenv("EXPORT_DIR", "./runtime/exports")
S3_LOCAL_DIR = os.getenv("S3_LOCAL_DIR", "./runtime/s3")
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "500"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "100"))
PIM_REQUESTS_PER_SECOND = float(os.getenv("PIM_REQUESTS_PER_SECOND", "9"))
WMS_REQUESTS_PER_SECOND = float(os.getenv("WMS_REQUESTS_PER_SECOND", "18"))
# Requests kept in flight at once. Throughput is capped by the rate limiters above; these
# only need to cover API latency (in-flight ≈ rate × latency, e.g. 18 req/s × 0.4 s ≈ 8).
PIM_MAX_IN_FLIGHT = int(os.getenv("PIM_MAX_IN_FLIGHT", "8"))
WMS_MAX_IN_FLIGHT = int(os.getenv("WMS_MAX_IN_FLIGHT", "8"))
HTTP_MAX_ATTEMPTS = int(os.getenv("HTTP_MAX_ATTEMPTS", "5"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))
SYNC_LEDGER_PATH = os.getenv("SYNC_LEDGER_PATH", "./runtime/sync-ledger.sqlite3")
# One logical run per business day; empty means today's UTC date (YYYY-MM-DD).
RUN_ID = os.getenv("RUN_ID", "")

SYNC_LEDGER_BACKEND = os.getenv("SYNC_LEDGER_BACKEND", "sqlite").lower()
DYNAMODB_LEDGER_TABLE = os.getenv("DYNAMODB_LEDGER_TABLE", "")

STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local").lower()
S3_BUCKET = os.getenv("S3_BUCKET", "")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-2")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# "json" for CloudWatch (one JSON object per line), "text" for local reading.
LOG_FORMAT = os.getenv("LOG_FORMAT", "json").lower()
