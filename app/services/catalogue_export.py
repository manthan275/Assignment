"""Stream a PIM catalogue into a run-scoped CSV export, reusing it if the run is re-triggered."""
import csv
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from app.clients.product_api import iter_products
from app.config import EXPORT_DIR, RUN_ID, S3_LOCAL_DIR, STORAGE_BACKEND, S3_BUCKET
from app.storage.s3 import S3Store

CSV_FIELDS = ["id", "name", "category", "price", "currency", "updated_at"]
logger = logging.getLogger(__name__)
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def default_run_id() -> str:
    """One logical run per business day, so a re-trigger resumes instead of starting over."""
    return RUN_ID or datetime.now(timezone.utc).strftime("%Y-%m-%d")


def export_key(run_id: str) -> str:
    return f"exports/{run_id}/catalogue.csv"


def _default_store():
    if STORAGE_BACKEND == "aws":
        from app.storage.s3 import AwsS3Store
        return AwsS3Store(S3_BUCKET)
    if STORAGE_BACKEND == "local":
        return S3Store(S3_LOCAL_DIR)
    raise ValueError(f"Unsupported STORAGE_BACKEND: {STORAGE_BACKEND}")


def export_catalogue(*, products=None, store=None, run_id: str | None = None) -> str:
    """Return the local path of the complete export for ``run_id``.

    If the run already has a complete export (locally, or in the store after a task
    restart) it is reused, so a resumed run sends the same batches. Otherwise the PIM is
    streamed to a ``.partial`` file, which is published to the store under
    ``exports/<run-id>/catalogue.csv`` and only then renamed into place.
    """
    run_id = run_id or default_run_id()
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"Invalid run id: {run_id!r}")
    output_dir = Path(EXPORT_DIR) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "catalogue.csv"
    if path.exists():
        logger.info("export reused from local disk", extra={"run_id": run_id})
        return str(path)
    store = store or _default_store()
    key = export_key(run_id)
    if store.exists(key):
        store.download_file(key, path)
        logger.info("export reused from storage", extra={"run_id": run_id, "key": key})
        return str(path)

    partial = output_dir / "catalogue.csv.partial"
    source = iter_products() if products is None else products
    # A failure leaves only the .partial file (kept for diagnosis), which is never published.
    # The PIM paginates by page number, so a product can appear twice if the catalogue
    # shifts mid-export. Keeping the first occurrence means a SKU is never sent twice in
    # one run. The set holds SKUs only (about 70 MB at 1M products).
    seen: set[str] = set()
    rows = duplicates = 0
    with partial.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for product in source:
            sku = str(product.get("id"))
            if sku in seen:
                duplicates += 1
                continue
            seen.add(sku)
            writer.writerow(product)
            rows += 1
    store.put_file(partial, key)
    os.replace(partial, path)
    log = logger.warning if duplicates else logger.info
    log("export published", extra={"run_id": run_id, "key": key, "rows": rows, "duplicates_dropped": duplicates})
    return str(path)
