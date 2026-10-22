"""Small synchronous HTTP helpers for the external PIM and WMS APIs."""
from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

import httpx

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class RateLimiter:
    """Enforce a minimum interval between requests made through this instance.

    The limiter is thread-safe, so all concurrent requests to one API share it.
    It is process-local: if requests were spread across multiple processes, the
    aggregate rate would need a shared limiter.
    """

    def __init__(self, requests_per_second: float):
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be greater than zero")
        self._interval = 1.0 / requests_per_second
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._interval - (now - self._last_request_at)
            if delay > 0:
                time.sleep(delay)
            self._last_request_at = time.monotonic()


def _spread(seconds: float) -> float:
    """Add up to 25% jitter so concurrent requests throttled together do not retry in lockstep."""
    return seconds + random.uniform(0, seconds * 0.25)


def retry_delay(response: httpx.Response | None, attempt: int, *, base: float = 0.5, cap: float = 30.0) -> float:
    """Return Retry-After (never less) when valid, otherwise capped exponential backoff + jitter."""
    if response is not None:
        value = response.headers.get("Retry-After")
        if value:
            try:
                return _spread(max(0.0, min(float(value), cap)))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(value)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    return _spread(max(0.0, min((retry_at - datetime.now(timezone.utc)).total_seconds(), cap)))
                except (TypeError, ValueError, OverflowError):
                    pass
    exponential = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(exponential * 0.5, exponential)


def request_with_retry(
    request: Callable[[], httpx.Response],
    *,
    max_attempts: int,
    rate_limiter: RateLimiter,
    retryable_status_codes: set[int] | None = None,
    retry_network_errors: bool = True,
) -> httpx.Response:
    """Retry only explicitly retryable failures.

    For side-effecting requests, callers should disable network-error retries and
    choose status codes only when the upstream contract guarantees retry safety.
    """
    retryable_status_codes = RETRYABLE_STATUS_CODES if retryable_status_codes is None else retryable_status_codes
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        rate_limiter.wait()
        response: httpx.Response | None = None
        try:
            response = request()
            if response.status_code not in retryable_status_codes:
                response.raise_for_status()
                return response
            last_error = httpx.HTTPStatusError(
                f"Transient HTTP status {response.status_code}", request=response.request, response=response
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if not retry_network_errors:
                raise
            last_error = exc
        if attempt == max_attempts:
            assert last_error is not None
            raise last_error
        # Only the throttled request waits; the shared limiter keeps the aggregate rate
        # below the documented limit (see ASSUMPTIONS.md A5).
        time.sleep(retry_delay(response, attempt))
    raise RuntimeError("unreachable")
