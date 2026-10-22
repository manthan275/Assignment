"""DynamoDB ledger against moto's in-memory DynamoDB (no Docker or AWS account needed)."""
import boto3
import pytest

moto = pytest.importorskip("moto")

from app.services.dynamodb_sync_ledger import BatchAlreadyClaimed, DynamoDBSyncLedger  # noqa: E402


@pytest.fixture
def ledger(monkeypatch):
    for name, value in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                        "AWS_DEFAULT_REGION": "ap-southeast-2"}.items():
        monkeypatch.setenv(name, value)
    with moto.mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="ap-southeast-2")
        dynamodb.create_table(
            TableName="ledger",
            KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}, {"AttributeName": "batch_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"},
                                  {"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DynamoDBSyncLedger("ledger", dynamodb_resource=dynamodb)


def test_batch_claims_are_conditional_and_failed_batches_can_be_reclaimed(ledger):
    ledger.start_run("run-1", "exports/run-1/catalogue.csv")
    ledger.mark_in_flight("run-1", 1, ["P1"])
    with pytest.raises(BatchAlreadyClaimed):
        ledger.mark_in_flight("run-1", 1, ["P1"])
    ledger.fail_batch("run-1", 1, "FAILED", "HTTP 429")
    ledger.mark_in_flight("run-1", 1, ["P1"])
    ledger.finish_batch("run-1", 1, "SUCCEEDED", ["P1"], [])
    with pytest.raises(BatchAlreadyClaimed):
        ledger.mark_in_flight("run-1", 1, ["P1"])
    assert [batch["status"] for batch in ledger.list_batches("run-1")] == ["SUCCEEDED"]


def test_product_state_round_trips_in_chunks_and_stays_out_of_batch_queries(ledger):
    skus = [f"P{i:04d}" for i in range(250)]
    ledger.record_products("run-1", [(sku, f"fp-{sku}", "ACCEPTED") for sku in skus])
    ledger.record_products("run-2", [("P0001", "fp-new", "SENT")])

    found = ledger.get_products(skus + ["UNKNOWN", "P0001"])
    assert len(found) == 250
    assert found["P0000"] == {"sku": "P0000", "fingerprint": "fp-P0000", "status": "ACCEPTED", "run_id": "run-1"}
    assert found["P0001"]["fingerprint"] == "fp-new"
    assert ledger.list_batches("run-1") == []
