variable "name_prefix" {
  description = "Prefix for catalogue sync resources."
  type        = string
  default     = "catalogue-sync"
}

variable "aws_region" {
  description = "AWS region for the deployment."
  type        = string
  default     = "ap-southeast-2"
}

variable "export_retention_days" {
  description = "Number of days to retain original catalogue exports (the brief requires 90)."
  type        = number
  default     = 90
}

variable "container_image" {
  description = "Immutable image URI for the catalogue-sync application (tag by commit SHA)."
  type        = string
}

variable "vpc_id" {
  description = "VPC used by the scheduled Fargate task."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets with outbound access (NAT) to reach the PIM and WMS APIs."
  type        = list(string)
}

variable "assign_public_ip" {
  description = "Give the task a public IP. Only for subnets without NAT; prefer private subnets."
  type        = bool
  default     = false
}

variable "product_api_url" {
  description = "Base URL of the PIM API reachable from the task."
  type        = string
}

variable "warehouse_api_url" {
  description = "Base URL of the WMS API reachable from the task."
  type        = string
}

variable "product_api_key_secret_arn" {
  description = "ARN of the Secrets Manager secret containing the PIM API key."
  type        = string
}

variable "warehouse_api_key_secret_arn" {
  description = "ARN of the Secrets Manager secret containing the WMS API key."
  type        = string
}

variable "schedule_expression" {
  description = "EventBridge schedule for the daily sync (UTC)."
  type        = string
  default     = "cron(0 2 * * ? *)"
}

variable "task_cpu" {
  description = "Fargate task CPU units. The work is I/O-bound; 1 vCPU is ample."
  type        = number
  default     = 1024
}

variable "task_memory" {
  description = "Fargate task memory in MiB (the export keeps a set of SKUs, ~70 MB at 1M)."
  type        = number
  default     = 2048
}

variable "pim_requests_per_second" {
  description = "Client-side PIM rate limit (documented limit 10/s)."
  type        = number
  default     = 9
}

variable "wms_requests_per_second" {
  description = "Client-side WMS rate limit (documented limit 20/s)."
  type        = number
  default     = 18
}

variable "max_task_attempts" {
  description = "How many times the workflow re-runs a failed task. Re-runs resume the same run."
  type        = number
  default     = 3
}

variable "run_duration_alarm_seconds" {
  description = "Alarm when a run takes longer than this (the target is 30 minutes)."
  type        = number
  default     = 1500
}

variable "alarm_email" {
  description = "Optional email address subscribed to alarm notifications."
  type        = string
  default     = ""
}
