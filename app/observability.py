"""Structured logging and CloudWatch metrics.

Logs are one JSON object per line so CloudWatch Logs Insights can query fields such as
``run_id`` or ``batch``. Metrics use the CloudWatch Embedded Metric Format (EMF): a log
line with an ``_aws`` block that CloudWatch turns into metrics, with no agent or
PutMetricData permission needed.
"""
from __future__ import annotations

import json
import logging
import sys
import time

from app.config import LOG_FORMAT, LOG_LEVEL

METRICS_NAMESPACE = "CatalogueSync"
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update({key: value for key, value in vars(record).items() if key not in _STANDARD_ATTRS})
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = " ".join(f"{key}={value}" for key, value in vars(record).items() if key not in _STANDARD_ATTRS)
        line = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.getMessage()}"
        line = f"{line} {extras}" if extras else line
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if LOG_FORMAT == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(LOG_LEVEL)
    for noisy in ("httpx", "httpcore", "botocore", "boto3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def emit_metrics(metrics: dict[str, float], *, run_id: str, units: dict[str, str] | None = None) -> None:
    """Write one EMF line; CloudWatch extracts each entry as a metric with dimension Service."""
    units = units or {}
    document = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": METRICS_NAMESPACE,
                "Dimensions": [["Service"]],
                "Metrics": [{"Name": name, "Unit": units.get(name, "Count")} for name in metrics],
            }],
        },
        "Service": "catalogue-sync",
        "run_id": run_id,
        **metrics,
    }
    # Printed directly: EMF must be the whole log line, not wrapped by a formatter.
    print(json.dumps(document), flush=True)
