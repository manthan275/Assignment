# Daily schedule → Step Functions → one Fargate task.
#
# The workflow passes a stable RUN_ID (the execution's UTC date, or "runId" from the
# execution input for a manual run) so every attempt within an execution resumes the same
# run. Exit codes from the container decide the next step:
#   0  success
#   1  failure that is safe to re-run (resumes from the ledger) → wait and retry
#   2  a batch outcome is unknown → stop and alert; an operator must reconcile first
resource "aws_cloudwatch_log_group" "workflow" {
  name              = "/aws/vendedlogs/states/${var.name_prefix}-workflow"
  retention_in_days = 90
}

locals {
  run_task_parameters = {
    Cluster        = aws_ecs_cluster.catalogue_sync.arn
    TaskDefinition = aws_ecs_task_definition.catalogue_sync.arn
    LaunchType     = "FARGATE"
    NetworkConfiguration = {
      AwsvpcConfiguration = {
        Subnets        = var.subnet_ids
        SecurityGroups = [aws_security_group.task.id]
        AssignPublicIp = var.assign_public_ip ? "ENABLED" : "DISABLED"
      }
    }
    Overrides = {
      ContainerOverrides = [{
        Name        = "catalogue-sync"
        Environment = [{ Name = "RUN_ID", "Value.$" = "$.runId" }]
      }]
    }
  }

  state_machine_definition = {
    Comment = "Daily catalogue export and WMS sync, resumable by run ID."
    StartAt = "HasRunId"
    States = {
      HasRunId = {
        Type    = "Choice"
        Choices = [{ Variable = "$.runId", IsPresent = true, Next = "Start" }]
        Default = "UseExecutionDate"
      }
      UseExecutionDate = {
        Type       = "Pass"
        Parameters = { "runId.$" = "States.ArrayGetItem(States.StringSplit($$.Execution.StartTime, 'T'), 0)" }
        Next       = "Start"
      }
      Start = {
        Type       = "Pass"
        Parameters = { "runId.$" = "$.runId", attempt = 1 }
        Next       = "RunSync"
      }
      RunSync = {
        Type       = "Task"
        Resource   = "arn:${local.partition}:states:::ecs:runTask.sync"
        Parameters = local.run_task_parameters
        ResultSelector = {
          "exitCode.$"      = "$.Containers[0].ExitCode"
          "stoppedReason.$" = "$.StoppedReason"
        }
        ResultPath = "$.task"
        # The task could not run or was stopped (capacity, host retirement): starting it
        # again resumes the same run.
        Retry = [{
          ErrorEquals     = ["States.TaskFailed", "ECS.AmazonECSException"]
          IntervalSeconds = 60
          MaxAttempts     = 2
          BackoffRate     = 2
        }]
        Next = "CheckExitCode"
      }
      CheckExitCode = {
        Type = "Choice"
        Choices = [
          { Variable = "$.task.exitCode", NumericEquals = 0, Next = "Succeeded" },
          { Variable = "$.task.exitCode", NumericEquals = 2, Next = "NeedsReconciliation" },
          { Variable = "$.attempt", NumericLessThan = var.max_task_attempts, Next = "WaitBeforeResume" },
        ]
        Default = "SyncFailed"
      }
      WaitBeforeResume = {
        Type    = "Wait"
        Seconds = 120
        Next    = "NextAttempt"
      }
      NextAttempt = {
        Type       = "Pass"
        Parameters = { "runId.$" = "$.runId", "attempt.$" = "States.MathAdd($.attempt, 1)" }
        Next       = "RunSync"
      }
      Succeeded = { Type = "Succeed" }
      NeedsReconciliation = {
        Type  = "Fail"
        Error = "NeedsReconciliation"
        Cause = "A WMS batch outcome is unknown. Reconcile it before re-running this run ID."
      }
      SyncFailed = {
        Type  = "Fail"
        Error = "SyncFailed"
        Cause = "The sync failed on every attempt. Re-running the same run ID resumes it."
      }
    }
  }
}

resource "aws_sfn_state_machine" "catalogue_sync" {
  name       = "${var.name_prefix}-workflow"
  role_arn   = aws_iam_role.step_functions.arn
  definition = jsonencode(local.state_machine_definition)

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.workflow.arn}:*"
    include_execution_data = false
    level                  = "ERROR"
  }
}

resource "aws_cloudwatch_event_rule" "daily_sync" {
  name                = "${var.name_prefix}-daily"
  description         = "Start the daily product catalogue synchronisation."
  schedule_expression = var.schedule_expression
}

resource "aws_cloudwatch_event_target" "daily_sync" {
  rule      = aws_cloudwatch_event_rule.daily_sync.name
  target_id = "catalogue-sync-workflow"
  arn       = aws_sfn_state_machine.catalogue_sync.arn
  role_arn  = aws_iam_role.eventbridge_start_workflow.arn
}
