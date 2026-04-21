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

# --- WebSocket API ---

output "ws_endpoint" {
  description = "WebSocket endpoint (wss://) for browser connections"
  value       = var.use_async_skills ? module.websocket[0].ws_api_endpoint : ""
}

# --- CDN ---

output "cloudfront_domain" {
  description = "CloudFront distribution domain (user-facing URL)"
  value       = module.cdn.cloudfront_domain
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID (for cache invalidations in deploy.sh)"
  value       = module.cdn.cloudfront_distribution_id
}

output "s3_bucket_name" {
  description = "S3 bucket name for static HTML files"
  value       = module.cdn.s3_bucket_name
}

# --- Custom domain ---

output "custom_domain_url" {
  description = "Custom-domain dashboard URL (empty when root_domain is not set)"
  value       = local.custom_dashboard_url
}

output "route53_nameservers" {
  description = "Nameservers to set at your registrar the first time you enable a custom domain (empty when manage_route53_zone = false or root_domain is not set)"
  value       = var.root_domain != "" && var.manage_route53_zone ? module.dns[0].zone_nameservers : []
}

output "acm_validation_records" {
  description = "CNAMEs to add at your external DNS provider for ACM certificate validation (populated only when root_domain is set and manage_route53_zone = false)"
  value       = var.root_domain != "" && !var.manage_route53_zone ? module.dns[0].cert_validation_records : []
}
