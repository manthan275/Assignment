"""Run one catalogue synchronisation: export the PIM, then send changes to the WMS.

Exit codes tell the orchestrator what to do next:
  0  succeeded (rejections are reported, not failures)
  1  failed; re-running the same run ID resumes it safely
  2  a batch outcome is unknown; an operator must reconcile it before re-running
"""
import argparse
import logging
import sys
import time

from app.observability import configure_logging, emit_metrics
from app.services.catalogue_export import default_run_id, export_catalogue
from app.services.warehouse_sync import AmbiguousBatchError, sync_catalogue

EXIT_OK, EXIT_FAILED, EXIT_NEEDS_RECONCILIATION = 0, 1, 2
logger = logging.getLogger("app.main")


def run(run_id: str | None = None) -> dict:
    run_id = run_id or default_run_id()
    started = time.monotonic()
    logger.info("run started", extra={"run_id": run_id})
    export_path = export_catalogue(run_id=run_id)
    export_seconds = time.monotonic() - started
    batch_results = sync_catalogue(export_path)
    accepted = sum(len(result["accepted"]) for result in batch_results)
    rejected = sum(len(result["rejected"]) for result in batch_results)
    sent_this_attempt = sum(1 for result in batch_results if not result.get("resumed"))
    summary = {
        "run_id": run_id,
        "export": export_path,
        "batches": len(batch_results),
        "batches_sent_this_attempt": sent_this_attempt,
        "accepted_products": accepted,
        "rejected_products": rejected,
        "unchanged_products": batch_results.unchanged,
        "status": "completed_with_rejections" if rejected else "completed",
        "duration_seconds": round(time.monotonic() - started, 1),
    }
    logger.info("run finished", extra=summary)
    emit_metrics(
        {
            "RunSucceeded": 1,
            "ProductsAccepted": accepted,
            "ProductsRejected": rejected,
            "ProductsUnchanged": batch_results.unchanged,
            "ExportDurationSeconds": round(export_seconds, 1),
            "RunDurationSeconds": summary["duration_seconds"],
        },
        run_id=run_id,
        units={"ExportDurationSeconds": "Seconds", "RunDurationSeconds": "Seconds"},
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the PIM catalogue and sync it to the WMS.")
    parser.add_argument("--run-id", help="logical run to start or resume (default: RUN_ID or today's UTC date)")
    args = parser.parse_args(argv)
    configure_logging()
    run_id = args.run_id or default_run_id()
    try:
        run(run_id)
    except AmbiguousBatchError:
        logger.exception("run needs reconciliation", extra={"run_id": run_id})
        emit_metrics({"RunNeedsReconciliation": 1}, run_id=run_id)
        return EXIT_NEEDS_RECONCILIATION
    except Exception:
        logger.exception("run failed", extra={"run_id": run_id})
        emit_metrics({"RunFailed": 1}, run_id=run_id)
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
