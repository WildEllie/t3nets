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

  bedrock_model_id = var.bedrock_model_id
}

# --- API Gateway ---
module "api" {
  source = "./modules/api"

  project     = var.project
  environment = var.environment

  alb_listener_arn = module.compute.alb_listener_arn
  alb_dns_name     = module.compute.alb_dns_name
  vpc_link_id      = module.compute.vpc_link_id
}
