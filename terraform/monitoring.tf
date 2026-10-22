# Alarms notify one SNS topic. Application metrics come from CloudWatch Embedded Metric
# Format log lines written by app/observability.py (namespace CatalogueSync).
resource "aws_sns_topic" "alarms" {
  name              = "${var.name_prefix}-alarms"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count     = var.alarm_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

locals {
  app_dimensions = { Service = "catalogue-sync" }
}

resource "aws_cloudwatch_metric_alarm" "workflow_failed" {
  alarm_name          = "${var.name_prefix}-workflow-failed"
  alarm_description   = "A catalogue sync execution ended in failure (all attempts failed, or reconciliation needed)."
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.catalogue_sync.arn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "needs_reconciliation" {
  alarm_name          = "${var.name_prefix}-needs-reconciliation"
  alarm_description   = "A WMS batch outcome is unknown. Check the AMBIGUOUS batches in the ledger before re-running."
  namespace           = "CatalogueSync"
  metric_name         = "RunNeedsReconciliation"
  dimensions          = local.app_dimensions
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "no_successful_run" {
  alarm_name          = "${var.name_prefix}-no-successful-run"
  alarm_description   = "No catalogue sync has succeeded in the last 24 hours (failed, stuck or never scheduled)."
  namespace           = "CatalogueSync"
  metric_name         = "RunSucceeded"
  dimensions          = local.app_dimensions
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "run_too_slow" {
  alarm_name          = "${var.name_prefix}-run-too-slow"
  alarm_description   = "A run took longer than the target window; check API latency, throttling and change volume."
  namespace           = "CatalogueSync"
  metric_name         = "RunDurationSeconds"
  dimensions          = local.app_dimensions
  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = var.run_duration_alarm_seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}
