import json
import threading

import httpx
import pytest

from app.clients import http_utils, product_api, warehouse_api


def test_page_size_is_configured():
    assert 0 < product_api.PAGE_SIZE <= 500


class NoWaitLimiter:
    def wait(self):
        pass


def wms_client(statuses, calls):
    """A WMS stand-in that answers with the given statuses in order, then 200."""
    remaining = list(statuses)

    def handler(request):
        calls.append(request)
        if remaining:
            status = remaining.pop(0)
            headers = {"Retry-After": "1"} if status in {429, 503} else {}
            return httpx.Response(status, json={"detail": "fault"}, headers=headers)
        skus = [product["sku"] for product in json.loads(request.content)["products"]]
        return httpx.Response(200, json={"accepted": skus, "rejected": []})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status", [429, 503])
def test_wms_retries_statuses_that_guarantee_the_batch_was_not_applied(monkeypatch, status):
    monkeypatch.setattr(http_utils.time, "sleep", lambda _: None)
    calls = []
    with wms_client([status], calls) as client:
        result = warehouse_api.send_batch([{"sku": "P1"}], client=client, limiter=NoWaitLimiter())
    assert result == {"accepted": ["P1"], "rejected": []}
    assert len(calls) == 2


@pytest.mark.parametrize("status", [500, 502, 504])
def test_wms_never_resends_after_an_ambiguous_status(monkeypatch, status):
    monkeypatch.setattr(http_utils.time, "sleep", lambda _: pytest.fail("must not retry"))
    calls = []
    with wms_client([status], calls) as client:
        with pytest.raises(httpx.HTTPStatusError) as raised:
            warehouse_api.send_batch([{"sku": "P1"}], client=client, limiter=NoWaitLimiter())
    assert len(calls) == 1
    assert warehouse_api.was_not_applied(raised.value) is False


def test_wms_never_resends_after_a_timeout(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("no response", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout) as raised:
            warehouse_api.send_batch([{"sku": "P1"}], client=client, limiter=NoWaitLimiter())
    assert len(calls) == 1
    assert warehouse_api.was_not_applied(raised.value) is False


@pytest.mark.parametrize("status, not_applied", [(400, True), (401, True), (413, True), (429, True),
                                                 (503, True), (500, False), (502, False), (504, False)])
def test_was_not_applied_classifies_http_failures(status, not_applied):
    request = httpx.Request("POST", "https://wms.test/products/batch")
    exc = httpx.HTTPStatusError("failed", request=request, response=httpx.Response(status, request=request))
    assert warehouse_api.was_not_applied(exc) is not_applied


def test_connection_failures_are_not_applied():
    request = httpx.Request("POST", "https://wms.test/products/batch")
    assert warehouse_api.was_not_applied(httpx.ConnectError("refused", request=request)) is True
    assert warehouse_api.was_not_applied(httpx.RemoteProtocolError("dropped", request=request)) is False


def pim_handler(total, requested, page_size=500):
    lock = threading.Lock()

    def handler(request):
        page = int(request.url.params["page"])
        with lock:
            requested.append(page)
        start = (page - 1) * page_size
        items = [{"id": f"P{i}"} for i in range(start + 1, min(start + page_size, total) + 1)]
        has_next = start + len(items) < total
        return httpx.Response(200, json={"products": items, "total": total, "has_next": has_next,
                                         "next_page": page + 1 if has_next else None})
    return handler


def test_pim_pages_are_fetched_concurrently_but_yielded_in_order(monkeypatch):
    monkeypatch.setattr(product_api, "RateLimiter", lambda _rate: NoWaitLimiter())
    requested = []
    with httpx.Client(transport=httpx.MockTransport(pim_handler(2_250, requested))) as client:
        ids = [row["id"] for row in product_api.iter_products(client=client, max_in_flight=3)]
    assert ids == [f"P{i}" for i in range(1, 2_251)]
    assert sorted(requested) == [1, 2, 3, 4, 5]

