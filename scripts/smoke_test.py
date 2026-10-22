"""End-to-end check against the supplied mocks over real HTTP. No Docker or AWS needed.

Starts both mock APIs with uvicorn on spare ports, then runs the CLI three times in a
temporary workspace:

  1. day-1           full load: every product is exported and sent
  2. day-1 again     re-trigger: the export is reused and nothing is sent
  3. day-2           next day: the catalogue is unchanged, so nothing is sent

Usage:  python scripts/smoke_test.py [--keep]
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_mock(name: str, port: int, log_dir: Path) -> subprocess.Popen:
    log = open(log_dir / f"{name}.log", "w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT / "mock-services" / name, stdout=log, stderr=subprocess.STDOUT,
    )
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return process
        except OSError:
            time.sleep(0.25)
    process.kill()
    raise SystemExit(f"{name} did not start; see {log_dir / (name + '.log')}")


def run_cli(run_id: str, env: dict, max_attempts: int = 3) -> dict:
    """Run the CLI like the Step Functions workflow does: exit 1 is re-run, which resumes."""
    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        completed = subprocess.run(
            [sys.executable, "-m", "app.main", "--run-id", run_id],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        lines = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
        finished = next((line for line in lines if line.get("message") == "run finished"), None)
        print(f"--- {run_id} (attempt {attempt}): exit {completed.returncode} in {time.monotonic() - started:.1f}s")
        if completed.returncode == 1 and attempt < max_attempts:
            failure = next((line for line in lines if line.get("level") in {"WARNING", "ERROR"}), {})
            print(f"    {failure.get('message', 'failed')}: {failure.get('error', '')[-120:]}; re-running to resume")
            continue
        break
    if completed.returncode != 0 or finished is None:
        print(completed.stdout[-4000:], completed.stderr[-4000:], sep="\n")
        raise SystemExit("smoke test failed")
    keys = ("batches_sent_this_attempt", "accepted_products", "rejected_products", "unchanged_products", "status")
    print("    " + ", ".join(f"{key}={finished[key]}" for key in keys))
    return finished


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep", action="store_true", help="keep the temporary workspace for inspection")
    args = parser.parse_args()
    workspace = Path(tempfile.mkdtemp(prefix="catalogue-sync-smoke-"))
    pim_port, wms_port = free_port(), free_port()
    mocks = [start_mock("product-api", pim_port, workspace), start_mock("warehouse-api", wms_port, workspace)]
    env = {
        **os.environ,
        "PRODUCT_API_URL": f"http://127.0.0.1:{pim_port}",
        "WAREHOUSE_API_URL": f"http://127.0.0.1:{wms_port}",
        "EXPORT_DIR": str(workspace / "exports"),
        "S3_LOCAL_DIR": str(workspace / "s3"),
        "SYNC_LEDGER_PATH": str(workspace / "ledger.sqlite3"),
        "STORAGE_BACKEND": "local",
        "SYNC_LEDGER_BACKEND": "sqlite",
        "LOG_FORMAT": "json",
    }
    try:
        first = run_cli("day-1", env)
        assert first["unchanged_products"] == 0
        again = run_cli("day-1", env)
        assert again["batches_sent_this_attempt"] == 0, "a re-triggered run must not send anything"
        assert again["accepted_products"] == first["accepted_products"]
        nextday = run_cli("day-2", env)
        assert nextday["batches"] == 0, "an unchanged catalogue must not be sent again"
        assert nextday["unchanged_products"] == first["accepted_products"] + first["rejected_products"]
        print("smoke test passed")
    finally:
        for mock in mocks:
            mock.terminate()
        print(f"workspace: {workspace}" if args.keep else "", end="")
        if not args.keep:
            subprocess.run(["rm", "-rf", str(workspace)])


if __name__ == "__main__":
    main()
