import json
import logging

from app import observability


def test_json_logs_carry_extra_fields():
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "export published", None, None)
    record.run_id = "2026-10-22"
    record.rows = 12
    entry = json.loads(observability.JsonFormatter().format(record))
    assert entry["message"] == "export published"
    assert entry["run_id"] == "2026-10-22"
    assert entry["rows"] == 12


def test_metrics_use_cloudwatch_embedded_metric_format(capsys):
    observability.emit_metrics({"ProductsSent": 7, "RunDurationSeconds": 1.5}, run_id="day-1",
                               units={"RunDurationSeconds": "Seconds"})
    document = json.loads(capsys.readouterr().out)
    directive = document["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == "CatalogueSync"
    assert directive["Dimensions"] == [["Service"]]
    assert {"Name": "RunDurationSeconds", "Unit": "Seconds"} in directive["Metrics"]
    assert document["ProductsSent"] == 7 and document["Service"] == "catalogue-sync"
