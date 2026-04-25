###############################################################################
# T3nets — AWS Infrastructure (Phase 1)
#
# Deploys:
#   - VPC with public/private subnets
#   - ECS Fargate cluster + router service
#   - API Gateway (HTTP API)
#   - DynamoDB tables (conversations, tenants)
#   - Secrets Manager (tenant integration credentials)
#   - ECR repository (router container image)
#   - Cognito user pool + app client (authentication)
#   - CloudWatch log groups
#   - IAM roles and policies
#
# Usage:
#   cd infra/aws
#   terraform init
#   terraform plan -var-file=environments/dev.tfvars
#   terraform apply -var-file=environments/dev.tfvars
###############################################################################

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

locals {
  # Map AWS region to Bedrock geographic inference profile prefix.
  # Newer models (Sonnet 4.5+, Nova) require geographic prefixes, not region-specific ones.
  bedrock_geo_prefix = (
    startswith(var.aws_region, "us-") ? "us" :
    startswith(var.aws_region, "eu-") ? "eu" :
    startswith(var.aws_region, "ap-") ? "apac" :
    startswith(var.aws_region, "ca-") ? "us" :
    startswith(var.aws_region, "sa-") ? "us" :
    "us"
  )
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "t3nets"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# CloudFront ACM certificates must live in us-east-1 regardless of aws_region.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "t3nets"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

locals {
  # Custom-domain dashboard URL (empty when root_domain is not set).
  custom_dashboard_url = var.root_domain != "" ? "https://${var.dashboard_subdomain}.${var.root_domain}" : ""

  # Append the custom-domain callback/logout URLs so Cognito accepts them
  # in addition to whatever is in the tfvars (localhost, the raw CloudFront URL).
  cognito_callback_urls = local.custom_dashboard_url != "" ? concat(var.cognito_callback_urls, ["${local.custom_dashboard_url}/callback"]) : var.cognito_callback_urls
  cognito_logout_urls   = local.custom_dashboard_url != "" ? concat(var.cognito_logout_urls, ["${local.custom_dashboard_url}/login"]) : var.cognito_logout_urls
}

# --- Networking ---
module "networking" {
  source = "./modules/networking"

  project     = var.project
  environment = var.environment
  aws_region  = var.aws_region
}

# --- Data stores ---
module "data" {
  source = "./modules/data"

  project     = var.project
  environment = var.environment
}

# --- Secrets ---
module "secrets" {
  source = "./modules/secrets"

  project     = var.project
  environment = var.environment
}

# --- Container registry ---
module "ecr" {
  source = "./modules/ecr"

  project     = var.project
  environment = var.environment
}

# --- DNS (Route 53 zone + ACM certificate in us-east-1) ---
module "dns" {
  count  = var.root_domain != "" ? 1 : 0
  source = "./modules/dns"

  providers = {
    aws.cloudfront = aws.us_east_1
  }

  root_domain         = var.root_domain
  dashboard_subdomain = var.dashboard_subdomain
  manage_zone         = var.manage_route53_zone
}

# --- Authentication (Cognito) ---
module "cognito" {
  source = "./modules/cognito"

  project     = var.project
  environment = var.environment

  callback_urls = local.cognito_callback_urls
  logout_urls   = local.cognito_logout_urls
}

# --- Compute (ECS Fargate) ---
module "compute" {
  source = "./modules/compute"

  project     = var.project
  environment = var.environment
  aws_region  = var.aws_region

  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids
  public_subnet_ids  = module.networking.public_subnet_ids

  ecr_repository_url = module.ecr.repository_url
  router_image_tag   = var.router_image_tag

  dynamodb_table_arns = module.data.table_arns
  secrets_base_arn    = module.secrets.base_arn

  router_cpu    = var.router_cpu
  router_memory = var.router_memory

  # Geographic prefix for Bedrock inference profiles: us., eu., apac.
  # Single-region prefixes (e.g. us-east-1.) are NOT valid for newer models.
  bedrock_model_id = "${local.bedrock_geo_prefix}.${var.bedrock_model_id}"

  # Cognito (for login URLs in the application)
  cognito_user_pool_id  = module.cognito.user_pool_id
  cognito_app_client_id = module.cognito.app_client_id
  cognito_auth_domain   = module.cognito.auth_domain

  # Phase 3b: Async skills
  pending_requests_table_arn  = module.data.pending_requests_table_arn
  pending_requests_table_name = module.data.pending_requests_table_name
  secrets_prefix              = module.secrets.secrets_prefix
  use_async_skills            = var.use_async_skills

  # WebSocket API endpoint (wss://) — injected as ECS env var
  ws_api_endpoint = var.use_async_skills ? module.websocket[0].ws_api_endpoint : ""

  # DynamoDB-backed WebSocket connection registry (cross-task fan-out)
  ws_connections_table_name = module.data.ws_connections_table_name

  # S3 BlobStore (practice persistence)
  s3_bucket_arn = module.cdn.s3_bucket_arn

  # CloudFront distribution — for ECS to invalidate /p/* after publishing
  # practice pages to S3 on install.
  cloudfront_distribution_id = module.cdn.cloudfront_distribution_id

  # Phase 5c: Ollama sidecar
  use_ollama       = var.use_ollama
  ollama_model     = var.ollama_model
  ollama_memory_mb = var.ollama_memory_mb
}

# --- WebSocket API (real-time push, replaces SSE for AWS) ---
module "websocket" {
  count  = var.use_async_skills ? 1 : 0
  source = "./modules/websocket"

  project     = var.project
  environment = var.environment
  aws_region  = var.aws_region

  vpc_id                = module.networking.vpc_id
  private_subnet_ids    = module.networking.private_subnet_ids
  alb_dns_name          = module.compute.alb_dns_name
  alb_security_group_id = module.compute.alb_security_group_id
}

# IAM: allow ECS task to push via WebSocket Management API
resource "aws_iam_role_policy" "ecs_ws_manage_connections" {
  count = var.use_async_skills ? 1 : 0
  name  = "${var.project}-${var.environment}-ecs-ws-manage"
  role  = module.compute.ecs_task_role_id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WebSocketManageConnections"
        Effect   = "Allow"
        Action   = ["execute-api:ManageConnections"]
        Resource = "${module.websocket[0].ws_api_arn}/prod/POST/@connections/*"
      }
    ]
  })
}

# --- API Gateway ---
module "api" {
  source = "./modules/api"

  project     = var.project
  environment = var.environment

  alb_listener_arn = module.compute.alb_listener_arn
  alb_dns_name     = module.compute.alb_dns_name
  vpc_link_id      = module.compute.vpc_link_id

  cognito_user_pool_endpoint = module.cognito.user_pool_endpoint
  cognito_app_client_id      = module.cognito.app_client_id
}

# --- CDN (S3 + CloudFront) ---
module "cdn" {
  source = "./modules/cdn"

  project     = var.project
  environment = var.environment

  api_gateway_url = module.api.api_endpoint

  # Custom-domain wiring (no-op when root_domain is empty).
  # Both the subdomain (www.t3nets.dev) and the apex (t3nets.dev) are served.
  aliases             = var.root_domain != "" ? [module.dns[0].dashboard_fqdn, module.dns[0].apex_fqdn] : []
  acm_certificate_arn = var.root_domain != "" ? module.dns[0].certificate_arn : ""
}

# Route 53 A-alias records for the custom domain.
# Lives in the root module because they need outputs from both dns and cdn.

resource "aws_route53_record" "dashboard_alias" {
  count   = var.root_domain != "" && var.manage_route53_zone ? 1 : 0
  zone_id = module.dns[0].zone_id
  name    = module.dns[0].dashboard_fqdn
  type    = "A"

  alias {
    name                   = module.cdn.cloudfront_domain
    zone_id                = module.cdn.cloudfront_hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "apex_alias" {
  count   = var.root_domain != "" && var.manage_route53_zone ? 1 : 0
  zone_id = module.dns[0].zone_id
  name    = module.dns[0].apex_fqdn
  type    = "A"

  alias {
    name                   = module.cdn.cloudfront_domain
    zone_id                = module.cdn.cloudfront_hosted_zone_id
    evaluate_target_health = false
  }
}
