resource "aws_ecr_repository" "catalogue_sync" {
  name                 = var.name_prefix
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_cloudwatch_log_group" "application" {
  name              = "/${var.name_prefix}/application"
  retention_in_days = 90
}

resource "aws_ecs_cluster" "catalogue_sync" {
  name = "${var.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_security_group" "task" {
  name_prefix = "${var.name_prefix}-task-"
  description = "Outbound-only access for the catalogue sync task."
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS to the PIM, WMS and AWS APIs. Narrow further with VPC endpoints or an egress proxy."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_ecs_task_definition" "catalogue_sync" {
  family                   = "${var.name_prefix}-task"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.task_cpu)
  memory                   = tostring(var.task_memory)
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  # Writable scratch space; the root filesystem is read-only.
  volume {
    name = "scratch"
  }

  container_definitions = jsonencode([{
    name                   = "catalogue-sync"
    image                  = var.container_image
    essential              = true
    readonlyRootFilesystem = true
    mountPoints            = [{ sourceVolume = "scratch", containerPath = "/tmp", readOnly = false }]
    environment = [
      { name = "PRODUCT_API_URL", value = var.product_api_url },
      { name = "WAREHOUSE_API_URL", value = var.warehouse_api_url },
      { name = "PIM_REQUESTS_PER_SECOND", value = tostring(var.pim_requests_per_second) },
      { name = "WMS_REQUESTS_PER_SECOND", value = tostring(var.wms_requests_per_second) },
      { name = "SYNC_LEDGER_BACKEND", value = "dynamodb" },
      { name = "DYNAMODB_LEDGER_TABLE", value = aws_dynamodb_table.sync_ledger.name },
      { name = "STORAGE_BACKEND", value = "aws" },
      { name = "S3_BUCKET", value = aws_s3_bucket.catalogue_exports.bucket },
      { name = "EXPORT_DIR", value = "/tmp/catalogue-exports" },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "LOG_FORMAT", value = "json" },
    ]
    secrets = [
      { name = "PRODUCT_API_KEY", valueFrom = var.product_api_key_secret_arn },
      { name = "WAREHOUSE_API_KEY", valueFrom = var.warehouse_api_key_secret_arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.application.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "sync"
      }
    }
  }])
}
