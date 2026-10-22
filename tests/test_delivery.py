"""Run-level guarantees: stable runs, export reuse, delta sync and safe resume."""
import csv
import threading
import time
from pathlib import Path

import httpx
import pytest

from app.services import catalogue_export, warehouse_sync
from app.services.sync_ledger import BatchAlreadyClaimed, SyncLedger
from app.storage.s3 import S3Store


def product(sku, price=10):
    return {"id": sku, "name": f"Product {sku}", "category": "HOME", "price": price,
            "currency": "INR", "updated_at": "2026-09-25T08:30:00Z"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(catalogue_export, "EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setattr(warehouse_sync, "BATCH_SIZE", 2)

    class Env:
        store = S3Store(tmp_path / "s3")
        ledger = SyncLedger(str(tmp_path / "ledger.sqlite3"))
        sent = []          # every product the fake WMS received, in order
        failures = []      # exceptions to raise for the next sends

        def export(self, run_id, products):
            return catalogue_export.export_catalogue(products=iter(products), store=self.store, run_id=run_id)

        def sync(self, path, max_in_flight=1):
            # One batch at a time keeps send order deterministic; concurrency has its own tests.
            return warehouse_sync.sync_catalogue(path, ledger=self.ledger, max_in_flight=max_in_flight)

    test_env = Env()
    test_env.fail_on = {}  # sku -> exception raised by the batch containing it
    lock = threading.Lock()

    def fake_send(batch, **kwargs):
        with lock:
            if test_env.failures:
                raise test_env.failures.pop(0)
            for item in batch:
                if item["sku"] in test_env.fail_on:
                    raise test_env.fail_on.pop(item["sku"])
        time.sleep(0.005)  # long enough for concurrent batches to overlap
        with lock:
            test_env.sent.extend(item["sku"] for item in batch)
        return {"accepted": [item["sku"] for item in batch if not item["sku"].endswith("999")],
                "rejected": [{"sku": item["sku"], "reason": "invalid"} for item in batch if item["sku"].endswith("999")]}

    monkeypatch.setattr(warehouse_sync, "send_batch", fake_send)
    return test_env


def http_error(status):
    request = httpx.Request("POST", "https://wms.test/products/batch")
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=httpx.Response(status, request=request))


# --- export -------------------------------------------------------------------

def test_export_of_a_run_is_reused_instead_of_rebuilt(env):
    first = env.export("2026-10-22", [product("P1")])
    second = catalogue_export.export_catalogue(
        products=iter(pytest.fail("must not re-read the PIM") for _ in [1]), store=env.store, run_id="2026-10-22")
    assert first == second
    assert env.store.exists("exports/2026-10-22/catalogue.csv")


def test_export_is_restored_from_the_store_after_losing_local_disk(env):
    path = Path(env.export("2026-10-22", [product("P1")]))
    path.unlink()  # a new Fargate task starts with an empty disk
    restored = catalogue_export.export_catalogue(products=iter([]), store=env.store, run_id="2026-10-22")
    with open(restored, newline="", encoding="utf-8") as handle:
        assert [row["id"] for row in csv.DictReader(handle)] == ["P1"]


def test_failed_export_is_never_published(env):
    def broken_pim():
        yield product("P1")
        raise httpx.ReadTimeout("PIM went away")

    with pytest.raises(httpx.ReadTimeout):
        catalogue_export.export_catalogue(products=broken_pim(), store=env.store, run_id="2026-10-22")
    assert not env.store.exists("exports/2026-10-22/catalogue.csv")
    assert not (Path(catalogue_export.EXPORT_DIR) / "2026-10-22" / "catalogue.csv").exists()
    # The re-trigger exports again rather than reusing the partial file.
    path = env.export("2026-10-22", [product("P1"), product("P2")])
    assert sum(1 for _ in open(path, encoding="utf-8")) == 3


@pytest.mark.parametrize("run_id", ["../etc", "a/b", ".hidden", "x" * 65])
def test_unsafe_run_ids_are_rejected(env, run_id):
    with pytest.raises(ValueError, match="Invalid run id"):
        catalogue_export.export_catalogue(products=iter([]), store=env.store, run_id=run_id)


def test_default_run_id_is_the_business_date(monkeypatch):
    monkeypatch.setattr(catalogue_export, "RUN_ID", "")
    assert len(catalogue_export.default_run_id()) == len("2026-10-22")
    monkeypatch.setattr(catalogue_export, "RUN_ID", "manual-1")
    assert catalogue_export.default_run_id() == "manual-1"


# --- never sent twice ---------------------------------------------------------

def test_retriggering_a_completed_run_sends_nothing(env):
    path = env.export("day-1", [product("P1"), product("P2"), product("P3")])
    env.sync(path)
    again = env.sync(env.export("day-1", []))
    assert env.sent == ["P1", "P2", "P3"]
    assert all(result.get("resumed") for result in again)


def test_next_run_sends_only_new_and_changed_products(env):
    env.sync(env.export("day-1", [product("P1"), product("P2"), product("P3")]))
    env.sent.clear()
    results = env.sync(env.export("day-2", [product("P1"), product("P2", price=12), product("P4")]))
    assert env.sent == ["P2", "P4"]
    assert results.unchanged == 1


def test_rejected_products_are_resent_only_when_they_change(env):
    env.sync(env.export("day-1", [product("P999")]))
    env.sync(env.export("day-2", [product("P999")]))
    env.sync(env.export("day-3", [product("P999", price=11)]))
    assert env.sent == ["P999", "P999"]


def test_failed_batch_that_never_reached_the_wms_is_resent_on_resume(env):
    path = env.export("day-1", [product("P1"), product("P2"), product("P3")])
    env.failures.append(http_error(429))   # retries exhausted: certainly not applied
    with pytest.raises(httpx.HTTPStatusError):
        env.sync(path)
    assert env.ledger.get_batch("day-1", 1)["status"] == "FAILED"

    env.sync(path)
    assert env.sent == ["P1", "P2", "P3"]
    assert env.ledger.get_run("day-1")["status"] == "COMPLETED"


def test_failed_batch_is_sent_by_the_next_run_if_never_resumed(env):
    env.failures.append(http_error(503))
    with pytest.raises(httpx.HTTPStatusError):
        env.sync(env.export("day-1", [product("P1")]))
    env.sync(env.export("day-2", [product("P1")]))
    assert env.sent == ["P1"]


def test_ambiguous_batch_is_not_resent_by_the_same_or_a_later_run(env):
    path = env.export("day-1", [product("P1"), product("P2"), product("P3")])
    env.failures.append(httpx.ReadTimeout("no response"))
    with pytest.raises(warehouse_sync.AmbiguousBatchError):
        env.sync(path)
    assert env.ledger.get_batch("day-1", 1)["status"] == "AMBIGUOUS"

    with pytest.raises(warehouse_sync.AmbiguousBatchError):
        env.sync(path)                      # same run: blocked until reconciled
    env.sync(env.export("day-2", [product("P1"), product("P2"), product("P3")]))
    assert env.sent == ["P3"]               # P1 and P2 may already be in the WMS


def test_crash_after_claiming_a_batch_is_not_resent_by_a_later_run(env):
    path = env.export("day-1", [product("P1"), product("P2")])

    def crash(batch, **kwargs):
        raise KeyboardInterrupt  # the process dies mid-request; no outcome is recorded

    original = warehouse_sync.send_batch
    warehouse_sync.send_batch = crash
    try:
        with pytest.raises(KeyboardInterrupt):
            env.sync(path)
    finally:
        warehouse_sync.send_batch = original
    assert env.ledger.get_batch("day-1", 1)["status"] == "IN_FLIGHT"
    env.sync(env.export("day-2", [product("P1"), product("P2")]))
    assert env.sent == []


def test_resume_refuses_when_batches_no_longer_match_the_ledger(env):
    path = env.export("day-1", [product("P1"), product("P2")])
    env.ledger.start_run("day-1", path)
    env.ledger.mark_in_flight("day-1", 1, ["P1", "OTHER"])
    with pytest.raises(RuntimeError, match="does not match the ledger"):
        env.sync(path)
    assert env.sent == []


def test_sqlite_ledger_only_reclaims_failed_batches(tmp_path):
    ledger = SyncLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.start_run("run-1", "path")
    ledger.mark_in_flight("run-1", 1, ["P1"])
    with pytest.raises(BatchAlreadyClaimed):
        ledger.mark_in_flight("run-1", 1, ["P1"])
    ledger.fail_batch("run-1", 1, "FAILED", "429")
    ledger.mark_in_flight("run-1", 1, ["P1"])
    assert ledger.get_batch("run-1", 1)["status"] == "IN_FLIGHT"


# --- concurrency -------------------------------------------------------------

def catalogue(count):
    return [product(f"P{i:04d}") for i in range(1, count + 1)]


def test_concurrent_sync_sends_every_product_exactly_once(env, monkeypatch):
    active, peak = [0], [0]
    lock = threading.Lock()
    original = warehouse_sync.send_batch

    def tracking_send(batch, **kwargs):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            time.sleep(0.05)  # a realistic WMS response time, so batches overlap
            return original(batch, **kwargs)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(warehouse_sync, "send_batch", tracking_send)
    results = env.sync(env.export("day-1", catalogue(50)), max_in_flight=4)

    assert sorted(env.sent) == [f"P{i:04d}" for i in range(1, 51)]
    assert [result["accepted"][0] for result in results] == [f"P{i:04d}" for i in range(1, 51, 2)]
    assert {batch["status"] for batch in env.ledger.list_batches("day-1")} == {"SUCCEEDED"}
    assert peak[0] > 1


def test_concurrent_ambiguous_batch_stops_new_work_and_settles_the_rest(env):
    env.fail_on["P0013"] = httpx.ReadTimeout("no response")   # batch 7 of 25
    with pytest.raises(warehouse_sync.AmbiguousBatchError):
        env.sync(env.export("day-1", catalogue(50)), max_in_flight=4)

    batches = {batch["batch_number"]: batch["status"] for batch in env.ledger.list_batches("day-1")}
    assert batches[7] == "AMBIGUOUS"
    assert "IN_FLIGHT" not in batches.values()          # every posted batch was settled
    assert len(batches) < 25                            # no new batches after the failure
    assert env.ledger.get_run("day-1")["status"] == "FAILED"

    first_run = set(env.sent)
    env.sync(env.export("day-2", catalogue(50)), max_in_flight=4)
    assert not {"P0013", "P0014"} & set(env.sent)       # possibly applied: never resent
    assert len(env.sent) == len(set(env.sent))          # nothing sent twice
    assert set(env.sent) | {"P0013", "P0014"} == {f"P{i:04d}" for i in range(1, 51)}
    assert first_run <= set(env.sent)


def test_concurrent_failed_batch_is_resent_on_resume_without_duplicates(env):
    env.fail_on["P0013"] = http_error(503)
    path = env.export("day-1", catalogue(50))
    with pytest.raises(httpx.HTTPStatusError):
        env.sync(path, max_in_flight=4)
    env.sync(path, max_in_flight=4)
    assert sorted(env.sent) == [f"P{i:04d}" for i in range(1, 51)]
    assert {batch["status"] for batch in env.ledger.list_batches("day-1")} == {"SUCCEEDED"}


def test_export_keeps_the_first_occurrence_of_a_duplicate_sku(env):
    path = env.export("day-1", [product("P1"), product("P2"), product("P1", price=99)])
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [(row["id"], row["price"]) for row in rows] == [("P1", "10"), ("P2", "10")]
