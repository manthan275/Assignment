"""SQLite run/batch ledger for restart-safe local execution.

SQLite is a local implementation of the persistence boundary. In AWS, this
boundary should be replaced by a durable shared store such as DynamoDB.

Besides runs and batches, the ledger keeps one record per SKU with the fingerprint
of the content last sent to the WMS, so later runs can skip unchanged products.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any


class BatchAlreadyClaimed(RuntimeError):
    """Another worker already created or claimed this batch record."""


class SyncLedger:
    def __init__(self, path: str = "./runtime/sync-ledger.sqlite3"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialise(self):
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS sync_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    export_path TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS sync_batches (
                    run_id TEXT NOT NULL,
                    batch_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    skus_json TEXT NOT NULL,
                    accepted_json TEXT,
                    rejected_json TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (run_id, batch_number),
                    FOREIGN KEY (run_id) REFERENCES sync_runs(run_id)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS product_state (
                    sku TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def start_run(self, run_id: str, export_path: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sync_runs(run_id,status,export_path) VALUES(?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET status='RUNNING', error=NULL, updated_at=CURRENT_TIMESTAMP",
                (run_id, "RUNNING", export_path),
            )

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE sync_runs SET status=?, error=?, updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (status, error, run_id),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Return the durable run summary for operational inspection."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sync_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _decode_batch(row) -> dict[str, Any]:
        result = dict(row)
        for field in ("skus_json", "accepted_json", "rejected_json"):
            raw = result.pop(field)
            result[field.removesuffix("_json")] = json.loads(raw) if raw is not None else None
        return result

    def list_batches(self, run_id: str) -> list[dict[str, Any]]:
        """Return batch outcomes in deterministic order for support/recovery."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sync_batches WHERE run_id=? ORDER BY batch_number", (run_id,)
            ).fetchall()
        return [self._decode_batch(row) for row in rows]

    def get_batch(self, run_id: str, batch_number: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sync_batches WHERE run_id=? AND batch_number=?", (run_id, batch_number)
            ).fetchone()
        return self._decode_batch(row) if row is not None else None

    def mark_in_flight(self, run_id: str, batch_number: int, skus: list[str]) -> None:
        """Claim a new batch, or one that previously FAILED without reaching the WMS."""
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO sync_batches(run_id,batch_number,status,skus_json) VALUES(?,?,?,?) "
                "ON CONFLICT(run_id,batch_number) DO UPDATE SET status='IN_FLIGHT', "
                "skus_json=excluded.skus_json, error=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE sync_batches.status='FAILED'",
                (run_id, batch_number, "IN_FLIGHT", json.dumps(skus)),
            )
            if cursor.rowcount != 1:
                raise BatchAlreadyClaimed(
                    f"Run {run_id} batch {batch_number} already exists; refusing to claim it again"
                )

    def finish_batch(self, run_id: str, batch_number: int, status: str, accepted: list, rejected: list) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE sync_batches SET status=?, accepted_json=?, rejected_json=?, error=NULL, "
                "updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND batch_number=?",
                (status, json.dumps(accepted), json.dumps(rejected), run_id, batch_number),
            )

    def fail_batch(self, run_id: str, batch_number: int, status: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE sync_batches SET status=?, error=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE run_id=? AND batch_number=?",
                (status, error, run_id, batch_number),
            )

    def get_products(self, skus: list[str]) -> dict[str, dict[str, Any]]:
        """Return the last recorded delivery state for each known SKU."""
        if not skus:
            return {}
        placeholders = ",".join("?" * len(skus))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT sku, fingerprint, status, run_id FROM product_state WHERE sku IN ({placeholders})",
                skus,
            ).fetchall()
        return {row["sku"]: dict(row) for row in rows}

    def record_products(self, run_id: str, records: Iterable[tuple[str, str, str]]) -> None:
        """Upsert (sku, fingerprint, status) delivery records for this run."""
        with self._connect() as connection:
            connection.executemany(
                "INSERT INTO product_state(sku,fingerprint,status,run_id) VALUES(?,?,?,?) "
                "ON CONFLICT(sku) DO UPDATE SET fingerprint=excluded.fingerprint, status=excluded.status, "
                "run_id=excluded.run_id, updated_at=CURRENT_TIMESTAMP",
                [(sku, fingerprint, status, run_id) for sku, fingerprint, status in records],
            )
