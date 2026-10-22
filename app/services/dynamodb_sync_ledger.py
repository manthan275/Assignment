"""DynamoDB-backed run/batch ledger for multi-worker deployments.

The table uses (run_id, batch_id) as its key. A reserved batch_id='RUN' stores
run metadata; batches use IDs such as 'BATCH#000001'. Conditional writes prevent
competing workers from both claiming a previously unseen batch. Per-SKU delivery
records share the table under run_id='PRODUCT#<sku>', batch_id='STATE'.
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key

from app.services.sync_ledger import BatchAlreadyClaimed

__all__ = ["BatchAlreadyClaimed", "DynamoDBSyncLedger"]

_PRODUCT_STATE_SORT_KEY = "STATE"
_MAX_BATCH_GET_KEYS = 100


class DynamoDBSyncLedger:
    def __init__(self, table_name: str, *, dynamodb_resource=None):
        if dynamodb_resource is None:
            import boto3
            dynamodb_resource = boto3.resource("dynamodb")
        self.resource = dynamodb_resource
        self.table_name = table_name
        self.table = dynamodb_resource.Table(table_name)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _batch_id(batch_number: int) -> str:
        return f"BATCH#{batch_number:06d}"

    @staticmethod
    def _decode(item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item:
            return None
        result = dict(item)
        result.pop("batch_id", None)
        return result

    def start_run(self, run_id: str, export_path: str) -> None:
        now = self._now()
        self.table.update_item(
            Key={"run_id": run_id, "batch_id": "RUN"},
            UpdateExpression=("SET #status = :status, export_path = :path, "
                              "updated_at = :now, created_at = if_not_exists(created_at, :now) "
                              "REMOVE #error"),
            ExpressionAttributeNames={"#status": "status", "#error": "error"},
            ExpressionAttributeValues={":status": "RUNNING", ":path": export_path, ":now": now},
        )

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        values = {":status": status, ":now": self._now()}
        expression = "SET #status = :status, updated_at = :now"
        if error is not None:
            expression += ", #error = :error"
            values[":error"] = error
        else:
            expression += " REMOVE #error"
        self.table.update_item(
            Key={"run_id": run_id, "batch_id": "RUN"},
            UpdateExpression=expression,
            ExpressionAttributeNames={"#status": "status", "#error": "error"},
            ExpressionAttributeValues=values,
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        response = self.table.get_item(Key={"run_id": run_id, "batch_id": "RUN"}, ConsistentRead=True)
        return self._decode(response.get("Item"))

    def list_batches(self, run_id: str) -> list[dict[str, Any]]:
        response = self.table.query(
            KeyConditionExpression=Key("run_id").eq(run_id) & Key("batch_id").begins_with("BATCH#"),
            ConsistentRead=True,
        )
        items = response.get("Items", [])
        while response.get("LastEvaluatedKey"):
            response = self.table.query(
                KeyConditionExpression=Key("run_id").eq(run_id) & Key("batch_id").begins_with("BATCH#"),
                ExclusiveStartKey=response["LastEvaluatedKey"],
                ConsistentRead=True,
            )
            items.extend(response.get("Items", []))
        return [self._decode(item) for item in sorted(items, key=lambda item: item["batch_id"])]

    def get_batch(self, run_id: str, batch_number: int) -> dict[str, Any] | None:
        response = self.table.get_item(
            Key={"run_id": run_id, "batch_id": self._batch_id(batch_number)},
            ConsistentRead=True,
        )
        return self._decode(response.get("Item"))

    def mark_in_flight(self, run_id: str, batch_number: int, skus: list[str]) -> None:
        """Claim a new batch, or one that previously FAILED without reaching the WMS."""
        now = self._now()
        try:
            self.table.put_item(
                Item={
                    "run_id": run_id,
                    "batch_id": self._batch_id(batch_number),
                    "batch_number": batch_number,
                    "status": "IN_FLIGHT",
                    "skus": skus,
                    "created_at": now,
                    "updated_at": now,
                },
                ConditionExpression="attribute_not_exists(batch_id) OR #status = :failed",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":failed": "FAILED"},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise BatchAlreadyClaimed(
                    f"Run {run_id} batch {batch_number} already exists; refusing to claim it again"
                ) from exc
            raise

    def finish_batch(self, run_id: str, batch_number: int, status: str, accepted: list, rejected: list) -> None:
        self.table.update_item(
            Key={"run_id": run_id, "batch_id": self._batch_id(batch_number)},
            UpdateExpression="SET #status = :status, accepted = :accepted, rejected = :rejected, updated_at = :now REMOVE #error",
            ConditionExpression="#status = :in_flight",
            ExpressionAttributeNames={"#status": "status", "#error": "error"},
            ExpressionAttributeValues={
                ":status": status, ":accepted": accepted, ":rejected": rejected,
                ":now": self._now(), ":in_flight": "IN_FLIGHT",
            },
        )

    def fail_batch(self, run_id: str, batch_number: int, status: str, error: str) -> None:
        self.table.update_item(
            Key={"run_id": run_id, "batch_id": self._batch_id(batch_number)},
            UpdateExpression="SET #status = :status, #error = :error, updated_at = :now",
            ConditionExpression="#status = :in_flight",
            ExpressionAttributeNames={"#status": "status", "#error": "error"},
            ExpressionAttributeValues={":status": status, ":error": error, ":now": self._now(), ":in_flight": "IN_FLIGHT"},
        )

    @staticmethod
    def _product_key(sku: str) -> dict[str, str]:
        return {"run_id": f"PRODUCT#{sku}", "batch_id": _PRODUCT_STATE_SORT_KEY}

    def get_products(self, skus: list[str]) -> dict[str, dict[str, Any]]:
        """Return the last recorded delivery state for each known SKU."""
        found: dict[str, dict[str, Any]] = {}
        unique = list(dict.fromkeys(skus))
        for start in range(0, len(unique), _MAX_BATCH_GET_KEYS):
            request = {self.table_name: {
                "Keys": [self._product_key(sku) for sku in unique[start:start + _MAX_BATCH_GET_KEYS]],
                "ConsistentRead": True,
            }}
            for attempt in range(8):
                response = self.resource.batch_get_item(RequestItems=request)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    found[item["sku"]] = {
                        "sku": item["sku"], "fingerprint": item["fingerprint"],
                        "status": item["status"], "run_id": item["last_run_id"],
                    }
                request = response.get("UnprocessedKeys") or {}
                if not request:
                    break
                time.sleep(min(2.0, 0.05 * 2 ** attempt))
            else:
                raise RuntimeError("DynamoDB left product-state keys unprocessed after retries")
        return found

    def record_products(self, run_id: str, records: Iterable[tuple[str, str, str]]) -> None:
        """Upsert (sku, fingerprint, status) delivery records for this run."""
        now = self._now()
        with self.table.batch_writer(overwrite_by_pkeys=["run_id", "batch_id"]) as writer:
            for sku, fingerprint, status in records:
                writer.put_item(Item={
                    **self._product_key(sku), "sku": sku, "fingerprint": fingerprint,
                    "status": status, "last_run_id": run_id, "updated_at": now,
                })
