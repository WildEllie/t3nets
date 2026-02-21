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
