"""CSV-to-WMS transformation and restart-aware synchronisation."""
import csv
import hashlib
import json
import logging
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
import httpx
from collections.abc import Iterable, Iterator

from app.clients.http_utils import RateLimiter
from app.clients.warehouse_api import chunked, send_batch, was_not_applied
from app.config import (
    BATCH_SIZE, SYNC_LEDGER_PATH, HTTP_TIMEOUT_SECONDS, WMS_MAX_IN_FLIGHT, WMS_REQUESTS_PER_SECOND,
    SYNC_LEDGER_BACKEND, DYNAMODB_LEDGER_TABLE,
)
from app.services.sync_ledger import BatchAlreadyClaimed, SyncLedger

# Product delivery states. SENT is written before the POST and stays if the outcome is
# never confirmed, so a later run treats the product as possibly delivered.
SENT, ACCEPTED, REJECTED, NOT_SENT = "SENT", "ACCEPTED", "REJECTED", "NOT_SENT"
_DELIVERED_STATES = {SENT, ACCEPTED, REJECTED}

logger = logging.getLogger(__name__)


class AmbiguousBatchError(RuntimeError):
    """A WMS request may have been processed; reconciliation is required."""


class SyncResults(list):
    """Per-batch outcomes, plus the number of products skipped as unchanged."""
    unchanged: int = 0


def transform(row: dict) -> dict:
    try:
        price = float(row["price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid product price for product {row.get('id', '<unknown>')}") from exc
    required = ("id", "name", "currency", "category", "updated_at")
    missing = [field for field in required if not row.get(field)]
    if missing:
        raise ValueError(f"Product {row.get('id')} missing required fields: {', '.join(missing)}")
    return {
        "sku": row["id"],
        "description": row["name"],
        "selling_price": price,
        "currency": row["currency"],
        "category_code": row["category"],
        "source_updated_at": row["updated_at"],
    }


def fingerprint(product: dict) -> str:
    """Stable hash of the exact payload the WMS receives for a product."""
    canonical = json.dumps(product, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def iter_transformed_rows(path: str) -> Iterator[dict]:
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            yield transform(row)


def iter_changed_products(products: Iterable[dict], ledger, run_id: str, results: SyncResults) -> Iterator[dict]:
    """Drop products whose identical content a previous run already sent.

    Products recorded by *this* run are kept, so a resumed run rebuilds exactly the
    same batches and the batch ledger decides what still needs sending.
    """
    for chunk in chunked(products, 100):
        known = ledger.get_products([str(product["sku"]) for product in chunk])
        for product in chunk:
            record = known.get(str(product["sku"]))
            if (
                record is not None
                and record["run_id"] != run_id
                and record["status"] in _DELIVERED_STATES
                and record["fingerprint"] == fingerprint(product)
            ):
                results.unchanged += 1
                continue
            yield product


def _default_ledger():
    if SYNC_LEDGER_BACKEND == "dynamodb":
        if not DYNAMODB_LEDGER_TABLE:
            raise ValueError("DYNAMODB_LEDGER_TABLE is required when SYNC_LEDGER_BACKEND=dynamodb")
        from app.services.dynamodb_sync_ledger import DynamoDBSyncLedger
        return DynamoDBSyncLedger(DYNAMODB_LEDGER_TABLE)
    if SYNC_LEDGER_BACKEND == "sqlite":
        return SyncLedger(SYNC_LEDGER_PATH)
    raise ValueError(f"Unsupported SYNC_LEDGER_BACKEND: {SYNC_LEDGER_BACKEND}")


def sync_catalogue(path: str, *, ledger: SyncLedger | None = None, max_in_flight: int | None = None) -> SyncResults:
    """Send new or changed products in bounded batches, recording every step in the ledger.

    The run ID is the export's directory name, so re-running the same export resumes it.
    Up to ``max_in_flight`` batches are posted concurrently, all sharing one rate limiter.
    Only this thread reads or writes the ledger; worker threads only perform the POST.
    Each batch is claimed and its products are recorded as SENT before it is submitted,
    and its outcome is recorded when the POST completes.

    An IN_FLIGHT or AMBIGUOUS batch is never resent automatically: the request may have
    reached WMS. Operators must reconcile it with the WMS before explicitly resetting
    the ledger record. A FAILED batch was certainly not applied and is resent on resume.
    After the first failed batch no new batches are started; batches already posted are
    allowed to finish and their outcomes are recorded.
    """
    ledger = ledger or _default_ledger()
    max_in_flight = max(1, max_in_flight or WMS_MAX_IN_FLIGHT)
    export_path = Path(path)
    run_id = export_path.parent.name
    ledger.start_run(run_id, str(export_path))
    results = SyncResults()
    outcomes: dict[int, dict] = {}
    errors: list[Exception] = []
    in_flight: dict[Future, tuple[int, dict[str, str]]] = {}
    client = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, limits=httpx.Limits(max_connections=max_in_flight))
    limiter = RateLimiter(WMS_REQUESTS_PER_SECOND)
    pool = ThreadPoolExecutor(max_workers=max_in_flight, thread_name_prefix="wms")

    def settle(done: Iterable[Future]) -> None:
        """Record the outcome of finished POSTs (called on this thread only)."""
        for future in done:
            batch_number, fingerprints = in_flight.pop(future)
            exc = future.exception()
            if exc is None:
                result = future.result()
                rejected = result["rejected"]
                rejected_skus = {str(item.get("sku")) for item in rejected if isinstance(item, dict)}
                records = [(str(sku), fingerprints[str(sku)], ACCEPTED) for sku in result["accepted"] if str(sku) in fingerprints]
                records += [(sku, fingerprints[sku], REJECTED) for sku in rejected_skus if sku in fingerprints]
                # Products the WMS did not mention keep their SENT record.
                ledger.record_products(run_id, records)
                ledger.finish_batch(run_id, batch_number, "PARTIAL" if rejected else "SUCCEEDED", result["accepted"], rejected)
                outcomes[batch_number] = result
            elif not isinstance(exc, Exception):
                raise exc  # KeyboardInterrupt/SystemExit: stop like a crash, without recording
            elif was_not_applied(exc):
                ledger.record_products(run_id, [(sku, fp, NOT_SENT) for sku, fp in fingerprints.items()])
                ledger.fail_batch(run_id, batch_number, "FAILED", str(exc))
                logger.warning("batch not applied", extra={"run_id": run_id, "batch": batch_number, "error": str(exc)})
                errors.append(exc)
            else:
                # Conservative: any unconfirmed POST outcome needs reconciliation.
                ledger.fail_batch(run_id, batch_number, "AMBIGUOUS", str(exc))
                logger.error("batch outcome unknown", extra={"run_id": run_id, "batch": batch_number, "error": str(exc)})
                error = AmbiguousBatchError(
                    f"WMS outcome for run {run_id} batch {batch_number} is unknown; "
                    "reconcile before retrying"
                )
                error.__cause__ = exc
                errors.append(error)

    try:
        try:
            changed = iter_changed_products(iter_transformed_rows(str(export_path)), ledger, run_id, results)
            for batch_number, batch in enumerate(chunked(changed, min(BATCH_SIZE, 100)), start=1):
                if errors:
                    break
                skus = [str(product["sku"]) for product in batch]
                existing = ledger.get_batch(run_id, batch_number)
                if existing:
                    if existing["skus"] != skus:
                        raise RuntimeError(
                            f"Run {run_id} batch {batch_number} does not match the ledger; "
                            "the export or delivery records changed during the run"
                        )
                    if existing["status"] in {"SUCCEEDED", "PARTIAL"}:
                        outcomes[batch_number] = {"accepted": existing["accepted"] or [], "rejected": existing["rejected"] or [], "resumed": True}
                        continue
                    if existing["status"] in {"IN_FLIGHT", "AMBIGUOUS"}:
                        raise AmbiguousBatchError(
                            f"Run {run_id} batch {batch_number} has status {existing['status']}; "
                            "reconcile with WMS before resubmitting"
                        )
                try:
                    ledger.mark_in_flight(run_id, batch_number, skus)
                except BatchAlreadyClaimed as exc:
                    # A conditional claim may lose a race with another worker.
                    # Never proceed to the external POST unless this worker owns the claim.
                    raise AmbiguousBatchError(str(exc)) from exc
                fingerprints = {str(product["sku"]): fingerprint(product) for product in batch}
                ledger.record_products(run_id, [(sku, fp, SENT) for sku, fp in fingerprints.items()])
                future = pool.submit(send_batch, batch, client=client, limiter=limiter)
                in_flight[future] = (batch_number, fingerprints)
                if len(in_flight) >= max_in_flight:
                    settle(wait(in_flight, return_when=FIRST_COMPLETED).done)
        finally:
            # Record every batch already posted, even when stopping early.
            while in_flight:
                settle(wait(in_flight, return_when=FIRST_COMPLETED).done)
        if errors:
            ambiguous = [error for error in errors if isinstance(error, AmbiguousBatchError)]
            raise (ambiguous or errors)[0]
        results.extend(outcomes[number] for number in sorted(outcomes))
        rejected_total = sum(len(result["rejected"]) for result in results)
        ledger.finish_run(run_id, "COMPLETED_WITH_REJECTIONS" if rejected_total else "COMPLETED")
        return results
    except Exception as exc:
        ledger.finish_run(run_id, "FAILED", str(exc))
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        client.close()
