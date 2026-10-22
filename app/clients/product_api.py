"""PIM API client with paginated, memory-bounded product iteration."""
from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor

import httpx

from app.clients.http_utils import RateLimiter, request_with_retry
from app.config import (
    HTTP_MAX_ATTEMPTS,
    HTTP_TIMEOUT_SECONDS,
    PAGE_SIZE,
    PIM_MAX_IN_FLIGHT,
    PIM_REQUESTS_PER_SECOND,
    PRODUCT_API_KEY,
    PRODUCT_API_URL,
)

logger = logging.getLogger(__name__)


def _fetch_page(client: httpx.Client, limiter: RateLimiter, page: int, page_size: int) -> dict:
    response = request_with_retry(
        lambda: client.get(
            f"{PRODUCT_API_URL}/products",
            params={"page": page, "page_size": page_size},
            headers={"X-API-Key": PRODUCT_API_KEY},
        ),
        max_attempts=HTTP_MAX_ATTEMPTS,
        rate_limiter=limiter,
    )
    payload = response.json()
    if not isinstance(payload.get("products"), list):
        raise ValueError("PIM response field 'products' must be a list")
    return payload


def _check_cursor(payload: dict, page: int) -> None:
    next_page = payload.get("next_page")
    if not isinstance(next_page, int) or next_page <= page:
        raise ValueError(f"Invalid PIM pagination cursor: {next_page!r}")


def iter_products(client: httpx.Client | None = None, *, max_in_flight: int | None = None) -> Iterator[dict]:
    """Yield products in page order without retaining the full catalogue in memory.

    Page 1 reports the catalogue ``total``. When it does, the following pages are fetched
    concurrently (up to ``max_in_flight`` at once, all sharing one rate limiter) and yielded
    in order, so throughput is set by the rate limit rather than by API latency. Without a
    total, pages are fetched one at a time by following ``next_page``.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    limiter = RateLimiter(PIM_REQUESTS_PER_SECOND)
    page_size = min(PAGE_SIZE, 500)
    max_in_flight = max(1, max_in_flight or PIM_MAX_IN_FLIGHT)
    pool: ThreadPoolExecutor | None = None
    try:
        payload = _fetch_page(client, limiter, 1, page_size)
        yield from payload["products"]
        if not payload.get("has_next"):
            return
        _check_cursor(payload, 1)
        total = payload.get("total")
        if not isinstance(total, int) or total < 0:
            page = payload["next_page"]
            while True:
                payload = _fetch_page(client, limiter, page, page_size)
                yield from payload["products"]
                if not payload.get("has_next"):
                    return
                _check_cursor(payload, page)
                page = payload["next_page"]

        last_page = max(2, math.ceil(total / page_size))
        logger.info("pim pagination planned", extra={"pim_total": total, "pages": last_page})
        pool = ThreadPoolExecutor(max_workers=max_in_flight, thread_name_prefix="pim")
        window: deque[tuple[int, Future]] = deque()
        next_to_submit = 2
        while True:
            while len(window) < max_in_flight and next_to_submit <= last_page:
                window.append((next_to_submit, pool.submit(_fetch_page, client, limiter, next_to_submit, page_size)))
                next_to_submit += 1
            if not window:
                # The catalogue grew past the total reported on page 1: keep following has_next.
                last_page += 1
                continue
            page, future = window.popleft()
            payload = future.result()
            yield from payload["products"]
            if not payload.get("has_next"):
                return
            if payload.get("next_page") != page + 1:
                raise ValueError(f"Invalid PIM pagination cursor: {payload.get('next_page')!r}")
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        if owns_client:
            client.close()


def fetch_all_products(client: httpx.Client | None = None) -> list[dict]:
    """Compatibility helper for callers that explicitly need an in-memory list."""
    return list(iter_products(client=client))
