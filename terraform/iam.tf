data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:ecs:${var.aws_region}:${local.account_id}:*"]
    }
  }
}

# Execution role: used by ECS itself to pull the image, write logs and inject secrets.
resource "aws_iam_role" "ecs_task_execution" {
  name               = "${var.name_prefix}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.product_api_key_secret_arn, var.warehouse_api_key_secret_arn]
  }
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name   = "read-api-keys"
  role   = aws_iam_role.ecs_task_execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# Task role: what the application code may do. No secrets access (keys arrive as env vars),
# no delete permissions on exports or ledger.
resource "aws_iam_role" "ecs_task" {
  name               = "${var.name_prefix}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "task_access" {
  statement {
    sid = "Ledger"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
    ]
    resources = [aws_dynamodb_table.sync_ledger.arn]
  }

  statement {
    sid = "ReadWriteExports"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${aws_s3_bucket.catalogue_exports.arn}/exports/*"]
  }

  # Without ListBucket, HeadObject on a missing export returns 403 instead of 404.
  statement {
    sid       = "ListExports"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.catalogue_exports.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["exports/*"]
    }
  }
}

resource "aws_iam_role_policy" "ecs_task_access" {
  name   = "ledger-and-exports"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.task_access.json
}

# Step Functions: run and watch the task.
resource "aws_iam_role" "step_functions" {
  name = "${var.name_prefix}-sfn"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

data "aws_iam_policy_document" "step_functions" {
  statement {
    sid       = "RunTask"
    actions   = ["ecs:RunTask"]
    resources = ["arn:${local.partition}:ecs:${var.aws_region}:${local.account_id}:task-definition/${aws_ecs_task_definition.catalogue_sync.family}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.catalogue_sync.arn]
    }
  }

  statement {
    sid       = "WatchTask"
    actions   = ["ecs:StopTask", "ecs:DescribeTasks"]
    resources = ["arn:${local.partition}:ecs:${var.aws_region}:${local.account_id}:task/${aws_ecs_cluster.catalogue_sync.name}/*"]
  }

  statement {
    sid       = "PassTaskRoles"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.ecs_task.arn, aws_iam_role.ecs_task_execution.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }

  # The .sync integration uses a managed EventBridge rule to learn when the task stops.
  statement {
    sid       = "SyncIntegrationRule"
    actions   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
    resources = ["arn:${local.partition}:events:${var.aws_region}:${local.account_id}:rule/StepFunctionsGetEventsForECSTaskRule"]
  }

  # Log delivery APIs do not support resource-level permissions.
  statement {
    sid = "ExecutionLogs"
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "step_functions" {
  name   = "run-sync-task"
  role   = aws_iam_role.step_functions.id
  policy = data.aws_iam_policy_document.step_functions.json
}

# EventBridge: start the workflow on schedule.
resource "aws_iam_role" "eventbridge_start_workflow" {
  name = "${var.name_prefix}-schedule"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "eventbridge_start_workflow" {
  name = "start-workflow"
  role = aws_iam_role.eventbridge_start_workflow.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "states:StartExecution", Resource = aws_sfn_state_machine.catalogue_sync.arn }]
  })
}
