output "api_endpoint" {
  description = "API Gateway endpoint URL"
  value       = module.api.api_endpoint
}

output "ecr_repository_url" {
  description = "ECR repository URL for pushing router images"
  value       = module.ecr.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = module.compute.cluster_name
}

output "ecs_service_name" {
  description = "ECS service name"
  value       = module.compute.service_name
}

output "dynamodb_tables" {
  description = "DynamoDB table names"
  value       = module.data.table_names
}

# --- Shared infrastructure for deploy.sh (per-skill Lambdas) ---

output "lambda_role_arn" {
  description = "Shared IAM role ARN for all skill Lambdas"
  value       = module.compute.lambda_role_arn
}

output "eventbridge_bus_name" {
  description = "EventBridge bus name for skill invocations"
  value       = module.compute.eventbridge_bus_name
}

output "eventbridge_bus_arn" {
  description = "EventBridge bus ARN for skill invocations"
  value       = module.compute.eventbridge_bus_arn
}

output "eventbridge_dlq_arn" {
  description = "EventBridge DLQ ARN for failed skill invocations"
  value       = module.compute.eventbridge_dlq_arn
}

output "sqs_results_queue_url" {
  description = "SQS results queue URL for skill execution results"
  value       = module.compute.sqs_results_queue_url
}

output "pending_requests_table_name" {
  description = "DynamoDB table name for pending async skill requests"
  value       = module.compute.pending_requests_table_name
}

output "secrets_prefix" {
  description = "Secrets Manager path prefix for tenant secrets"
  value       = module.compute.secrets_prefix
}
