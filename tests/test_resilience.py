import csv
from pathlib import Path

import httpx
import pytest

from app.clients import http_utils, product_api, warehouse_api
from app.services.catalogue_export import export_catalogue
from app.services.warehouse_sync import iter_transformed_rows, transform


class NoWaitLimiter:
    def wait(self):
        pass


def response(status_code, *, headers=None, json_data=None):
    request = httpx.Request("GET", "https://example.test")
    return httpx.Response(status_code, headers=headers, json=json_data, request=request)


def test_request_with_retry_honours_retry_after(monkeypatch):
    sleeps = []
    responses = iter([
        response(429, headers={"Retry-After": "2"}, json_data={"detail": "slow down"}),
        response(200, json_data={"ok": True}),
    ])
    monkeypatch.setattr(http_utils.time, "sleep", sleeps.append)

    result = http_utils.request_with_retry(
        lambda: next(responses),
        max_attempts=2,
        rate_limiter=NoWaitLimiter(),
    )

    assert result.status_code == 200
    assert len(sleeps) == 1 and 2.0 <= sleeps[0] <= 2.5   # never earlier than Retry-After


def test_request_with_retry_does_not_retry_non_transient_4xx(monkeypatch):
    calls = []
    monkeypatch.setattr(http_utils.time, "sleep", lambda _: pytest.fail("should not sleep"))

    def request():
        calls.append(1)
        return response(401, json_data={"detail": "unauthorized"})

    with pytest.raises(httpx.HTTPStatusError):
        http_utils.request_with_retry(request, max_attempts=4, rate_limiter=NoWaitLimiter())
    assert len(calls) == 1


def test_chunked_keeps_batches_at_or_below_wms_limit():
    batches = list(warehouse_api.chunked(({"sku": str(i)} for i in range(205)), 100))
    assert [len(batch) for batch in batches] == [100, 100, 5]


def test_export_streams_rows_and_creates_run_scoped_store_copy(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.catalogue_export.EXPORT_DIR", str(tmp_path / "exports"))

    class Store:
        def __init__(self):
            self.key = None
            self.content = None

        def put_file(self, source, key):
            self.key = key
            self.content = Path(source).read_bytes()
            return "local-copy"

        def exists(self, key):
            return False

    store = Store()
    rows = [
        {"id": "P1", "name": "One", "category": "HOME", "price": 10, "currency": "INR", "updated_at": "now"},
        {"id": "P2", "name": "Two", "category": "HOME", "price": 20, "currency": "INR", "updated_at": "now"},
    ]
    path = Path(export_catalogue(products=iter(rows), store=store))
    assert path.exists()
    assert store.key.startswith("exports/")
    assert store.key.endswith("/catalogue.csv")
    with path.open(newline="", encoding="utf-8") as handle:
        assert [row["id"] for row in csv.DictReader(handle)] == ["P1", "P2"]
    assert store.content == path.read_bytes()


def test_transform_rejects_invalid_price():
    row = {"id": "P1", "name": "One", "category": "HOME", "price": "bad", "currency": "INR", "updated_at": "now"}
    with pytest.raises(ValueError, match="Invalid product price"):
        transform(row)


def test_sync_row_reader_transforms_incrementally(tmp_path):
    path = tmp_path / "catalogue.csv"
    path.write_text(
        "id,name,category,price,currency,updated_at\nP1,One,HOME,10,INR,now\n",
        encoding="utf-8",
    )
    rows = list(iter_transformed_rows(str(path)))
    assert rows == [{
        "sku": "P1",
        "description": "One",
        "selling_price": 10.0,
        "currency": "INR",
        "category_code": "HOME",
        "source_updated_at": "now",
    }]


def test_transport_error_can_be_configured_as_non_retryable(monkeypatch):
    calls = []
    monkeypatch.setattr(http_utils.time, "sleep", lambda _: pytest.fail("should not sleep"))

    def request():
        calls.append(1)
        raise httpx.ReadTimeout("response timed out")

    with pytest.raises(httpx.ReadTimeout):
        http_utils.request_with_retry(
            request,
            max_attempts=5,
            rate_limiter=NoWaitLimiter(),
            retryable_status_codes={429},
            retry_network_errors=False,
        )
    assert len(calls) == 1


def test_sync_ledger_skips_confirmed_success_on_resume(tmp_path, monkeypatch):
    from app.services import warehouse_sync
    from app.services.sync_ledger import SyncLedger

    export_dir = tmp_path / "run-123"
    export_dir.mkdir()
    export_path = export_dir / "catalogue.csv"
    export_path.write_text(
        "id,name,category,price,currency,updated_at\nP1,One,HOME,10,INR,now\n",
        encoding="utf-8",
    )
    ledger = SyncLedger(str(tmp_path / "ledger.sqlite3"))
    calls = []

    def fake_send(batch, **kwargs):
        calls.append(batch)
        return {"accepted": [item["sku"] for item in batch], "rejected": []}

    monkeypatch.setattr(warehouse_sync, "send_batch", fake_send)
    assert warehouse_sync.sync_catalogue(str(export_path), ledger=ledger)[0]["accepted"] == ["P1"]
    assert warehouse_sync.sync_catalogue(str(export_path), ledger=ledger)[0]["resumed"] is True
    assert len(calls) == 1


def test_sync_refuses_to_resend_ambiguous_batch(tmp_path, monkeypatch):
    from app.services import warehouse_sync
    from app.services.sync_ledger import SyncLedger

    export_dir = tmp_path / "run-ambiguous"
    export_dir.mkdir()
    export_path = export_dir / "catalogue.csv"
    export_path.write_text(
        "id,name,category,price,currency,updated_at\nP1,One,HOME,10,INR,now\n",
        encoding="utf-8",
    )
    ledger = SyncLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.start_run(export_dir.name, str(export_path))
    ledger.mark_in_flight(export_dir.name, 1, ["P1"])
    monkeypatch.setattr(warehouse_sync, "send_batch", lambda *args, **kwargs: pytest.fail("must not resend"))

    with pytest.raises(warehouse_sync.AmbiguousBatchError, match="reconcile with WMS"):
        warehouse_sync.sync_catalogue(str(export_path), ledger=ledger)


def test_s3_store_copies_export_and_rejects_unsafe_keys(tmp_path):
    from app.storage.s3 import S3Store

    source = tmp_path / "catalogue.csv"
    source.write_bytes(b"id,name\nP1,One\n")
    store = S3Store(tmp_path / "objects")
    destination = store.put_file(source, "exports/run-1/catalogue.csv")
    assert Path(destination).read_bytes() == source.read_bytes()
    with pytest.raises(ValueError, match="relative path"):
        store.put_file(source, "../outside.csv")


def test_ledger_exposes_run_and_batch_status_across_instances(tmp_path):
    from app.services.sync_ledger import SyncLedger

    path = str(tmp_path / "ledger.sqlite3")
    ledger = SyncLedger(path)
    ledger.start_run("run-1", "/exports/run-1/catalogue.csv")
    ledger.mark_in_flight("run-1", 1, ["P1", "P2"])
    ledger.finish_batch("run-1", 1, "PARTIAL", ["P1"], [{"sku": "P2", "reason": "invalid"}])
    ledger.finish_run("run-1", "COMPLETED_WITH_REJECTIONS")

    reopened = SyncLedger(path)
    assert reopened.get_run("run-1")["status"] == "COMPLETED_WITH_REJECTIONS"
    batches = reopened.list_batches("run-1")
    assert batches[0]["status"] == "PARTIAL"
    assert batches[0]["skus"] == ["P1", "P2"]
    assert batches[0]["accepted"] == ["P1"]
    assert batches[0]["rejected"] == [{"sku": "P2", "reason": "invalid"}]


def test_pim_pagination_is_incremental_and_respects_page_cap(monkeypatch):
    class MockLimiter:
        def __init__(self, _rate):
            pass

        def wait(self):
            pass

    monkeypatch.setattr(product_api, "RateLimiter", MockLimiter)
    requested = []

    def handler(request):
        requested.append(dict(request.url.params))
        page = int(request.url.params["page"])
        payload = {
            1: {"products": [{"id": "P1"}], "has_next": True, "next_page": 2},
            2: {"products": [{"id": "P2"}], "has_next": False},
        }[page]
        return httpx.Response(200, json=payload, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert [row["id"] for row in product_api.iter_products(client=client)] == ["P1", "P2"]

    assert requested == [{"page": "1", "page_size": "500"}, {"page": "2", "page_size": "500"}]


def test_sync_does_not_resend_confirmed_success(tmp_path, monkeypatch):
    from app.services.sync_ledger import SyncLedger
    from app.services import warehouse_sync

    export_dir = tmp_path / "run-123"
    export_dir.mkdir()
    path = export_dir / "catalogue.csv"
    path.write_text(
        "id,name,category,price,currency,updated_at\nP1,One,HOME,10,INR,now\n",
        encoding="utf-8",
    )
    ledger = SyncLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.start_run("run-123", str(path))
    ledger.mark_in_flight("run-123", 1, ["P1"])
    ledger.finish_batch("run-123", 1, "SUCCEEDED", ["P1"], [])
    calls = []
    monkeypatch.setattr(warehouse_sync, "send_batch", lambda *args, **kwargs: calls.append(1))

    results = warehouse_sync.sync_catalogue(str(path), ledger=ledger)

    assert calls == []
    assert results == [{"accepted": ["P1"], "rejected": [], "resumed": True}]
    assert ledger.get_run("run-123")["status"] == "COMPLETED"


def test_sync_leaves_unreconciled_batch_in_flight(tmp_path):
    from app.services.sync_ledger import SyncLedger
    from app.services.warehouse_sync import AmbiguousBatchError, sync_catalogue

    export_dir = tmp_path / "run-ambiguous"
    export_dir.mkdir()
    path = export_dir / "catalogue.csv"
    path.write_text(
        "id,name,category,price,currency,updated_at\nP1,One,HOME,10,INR,now\n",
        encoding="utf-8",
    )
    ledger = SyncLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.start_run("run-ambiguous", str(path))
    ledger.mark_in_flight("run-ambiguous", 1, ["P1"])

    with pytest.raises(AmbiguousBatchError, match="reconcile with WMS"):
        sync_catalogue(str(path), ledger=ledger)

    assert ledger.get_batch("run-ambiguous", 1)["status"] == "IN_FLIGHT"


def test_dynamodb_ledger_uses_conditional_claim_and_persists_terminal_result():
    from botocore.exceptions import ClientError
    from app.services.dynamodb_sync_ledger import BatchAlreadyClaimed, DynamoDBSyncLedger

    class FakeTable:
        def __init__(self):
            self.items = {}

        def put_item(self, *, Item, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues):
            key = (Item["run_id"], Item["batch_id"])
            if key in self.items and self.items[key].get("status") != ExpressionAttributeValues[":failed"]:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}}, "PutItem")
            self.items[key] = dict(Item)

        def get_item(self, *, Key, ConsistentRead=False):
            item = self.items.get((Key["run_id"], Key["batch_id"]))
            return {"Item": dict(item)} if item else {}

        def update_item(self, *, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues, ConditionExpression=None):
            item = self.items.setdefault((Key["run_id"], Key["batch_id"]), dict(Key))
            if ConditionExpression and item.get("status") != ExpressionAttributeValues[":in_flight"]:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "state mismatch"}}, "UpdateItem")
            values = ExpressionAttributeValues
            names = ExpressionAttributeNames
            item[names["#status"]] = values[":status"]
            item["updated_at"] = values[":now"]
            if ":accepted" in values:
                item["accepted"] = values[":accepted"]
                item["rejected"] = values[":rejected"]
                item.pop(names["#error"], None)
            if ":error" in values:
                item[names["#error"]] = values[":error"]
            elif "REMOVE #error" in UpdateExpression:
                item.pop(names["#error"], None)

        def query(self, **kwargs):
            return {"Items": [dict(item) for (run, batch), item in self.items.items() if run == "run-1" and batch.startswith("BATCH#")]}

    class FakeResource:
        def __init__(self):
            self.table = FakeTable()

        def Table(self, _name):
            return self.table

    resource = FakeResource()
    ledger = DynamoDBSyncLedger("test-ledger", dynamodb_resource=resource)
    ledger.start_run("run-1", "exports/run-1/catalogue.csv")
    ledger.mark_in_flight("run-1", 1, ["P1", "P2"])
    with pytest.raises(BatchAlreadyClaimed):
        ledger.mark_in_flight("run-1", 1, ["P1", "P2"])
    ledger.finish_batch("run-1", 1, "PARTIAL", ["P1"], [{"sku": "P2", "reason": "invalid"}])

    batch = ledger.get_batch("run-1", 1)
    assert batch["status"] == "PARTIAL"
    assert batch["skus"] == ["P1", "P2"]
    assert batch["accepted"] == ["P1"]
    assert batch["rejected"] == [{"sku": "P2", "reason": "invalid"}]
    assert ledger.list_batches("run-1")[0]["batch_number"] == 1


def test_aws_s3_store_uses_upload_file_and_validates_key(tmp_path):
    from app.storage.s3 import AwsS3Store

    class FakeS3:
        def __init__(self):
            self.upload = None

        def upload_file(self, source, bucket, key):
            self.upload = (source, bucket, key)

    source = tmp_path / "catalogue.csv"
    source.write_text("id\nP1\n", encoding="utf-8")
    client = FakeS3()
    store = AwsS3Store("bucket-name", s3_client=client)
    assert store.put_file(source, "exports/run-1/catalogue.csv") == "s3://bucket-name/exports/run-1/catalogue.csv"
    assert client.upload == (str(source), "bucket-name", "exports/run-1/catalogue.csv")
    with pytest.raises(ValueError, match="relative path"):
        store.put_file(source, "../outside.csv")
