"""Opt-in service integration test: set DYNAMODB_LOCAL_ENDPOINT to run it."""
import os
import uuid

import boto3
import pytest

from app.services.dynamodb_sync_ledger import BatchAlreadyClaimed, DynamoDBSyncLedger


@pytest.mark.skipif(not os.getenv("DYNAMODB_LOCAL_ENDPOINT"), reason="DynamoDB Local endpoint not configured")
def test_dynamodb_local_ledger_claims_and_persists_state():
    endpoint = os.environ["DYNAMODB_LOCAL_ENDPOINT"]
    region = os.getenv("AWS_REGION", "us-east-1")
    dynamodb = boto3.resource(
        "dynamodb", endpoint_url=endpoint, region_name=region,
        aws_access_key_id="local", aws_secret_access_key="local",
    )
    table_name = f"catalogue-ledger-test-{uuid.uuid4().hex[:8]}"
    table = dynamodb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}, {"AttributeName": "batch_id", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}, {"AttributeName": "batch_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    try:
        ledger = DynamoDBSyncLedger(table_name, dynamodb_resource=dynamodb)
        ledger.start_run("integration-run", "exports/integration-run/catalogue.csv")
        ledger.mark_in_flight("integration-run", 1, ["SKU-1"])
        with pytest.raises(BatchAlreadyClaimed):
            ledger.mark_in_flight("integration-run", 1, ["SKU-1"])
        ledger.finish_batch("integration-run", 1, "SUCCEEDED", ["SKU-1"], [])
        assert ledger.get_batch("integration-run", 1)["status"] == "SUCCEEDED"
        assert ledger.get_run("integration-run")["status"] == "RUNNING"
        assert len(ledger.list_batches("integration-run")) == 1
    finally:
        table.delete()
