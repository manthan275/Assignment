"""WMS API client. Outcomes are returned per batch, including business rejects."""
from __future__ import annotations

from collections.abc import Iterable, Iterator

import httpx

from app.clients.http_utils import RateLimiter, request_with_retry
from app.config import (
    BATCH_SIZE,
    HTTP_MAX_ATTEMPTS,
    HTTP_TIMEOUT_SECONDS,
    WAREHOUSE_API_KEY,
    WAREHOUSE_API_URL,
    WMS_REQUESTS_PER_SECOND,
)

# Statuses the WMS returns before it processes a batch (throttling and "temporarily
# unavailable"), so resending is safe. Other 5xx and transport failures may arrive
# after the batch was applied and must not be retried (see ASSUMPTIONS.md A1, A12).
WMS_RETRYABLE_STATUS_CODES = frozenset({429, 503})


def chunked(items: Iterable[dict], size: int) -> Iterator[list[dict]]:
    if size < 1 or size > 100:
        raise ValueError("batch size must be between 1 and the WMS maximum of 100")
    batch: list[dict] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def send_batch(products: list[dict], client: httpx.Client | None = None, limiter: RateLimiter | None = None) -> dict:
    """Submit one batch, retrying only responses that guarantee it was not applied."""
    if len(products) > 100:
        raise ValueError("WMS batch cannot contain more than 100 products")
    owns_client = client is None
    client = client or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    limiter = limiter or RateLimiter(WMS_REQUESTS_PER_SECOND)
    try:
        response = request_with_retry(
            lambda: client.post(
                f"{WAREHOUSE_API_URL}/products/batch",
                json={"products": products},
                headers={"X-API-Key": WAREHOUSE_API_KEY},
            ),
            max_attempts=HTTP_MAX_ATTEMPTS,
            rate_limiter=limiter,
            retryable_status_codes=set(WMS_RETRYABLE_STATUS_CODES),
            retry_network_errors=False,
        )
        payload = response.json()
        accepted = payload.get("accepted", [])
        rejected = payload.get("rejected", [])
        if not isinstance(accepted, list) or not isinstance(rejected, list):
            raise ValueError("WMS response must contain list fields 'accepted' and 'rejected'")
        return {"accepted": accepted, "rejected": rejected}
    finally:
        if owns_client:
            client.close()


def was_not_applied(exc: Exception) -> bool:
    """True when a failed send certainly did not reach the WMS's processing logic."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return 400 <= status < 500 or status in WMS_RETRYABLE_STATUS_CODES
    # The connection was never established, so the request was never sent.
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))


def send_products(products: Iterable[dict], client: httpx.Client | None = None) -> list[dict]:
    """Send an iterable in bounded batches and return each batch's outcome."""
    owns_client = client is None
    client = client or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    limiter = RateLimiter(WMS_REQUESTS_PER_SECOND)
    try:
        return [send_batch(batch, client=client, limiter=limiter) for batch in chunked(products, min(BATCH_SIZE, 100))]
    finally:
        if owns_client:
            client.close()
