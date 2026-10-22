output "exports_bucket_name" {
  value = aws_s3_bucket.catalogue_exports.bucket
}

output "ledger_table_name" {
  value = aws_dynamodb_table.sync_ledger.name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.catalogue_sync.repository_url
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.catalogue_sync.name
}

output "ecs_task_definition_arn" {
  value = aws_ecs_task_definition.catalogue_sync.arn
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.catalogue_sync.arn
}

output "alarm_topic_arn" {
  value = aws_sns_topic.alarms.arn
}
