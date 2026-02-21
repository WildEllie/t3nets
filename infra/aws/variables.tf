variable "project" {
  description = "Project name"
  type        = string
  default     = "t3nets"
}

variable "environment" {
  description = "Environment (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "router_image_tag" {
  description = "Docker image tag for the router container"
  type        = string
  default     = "latest"
}

variable "router_cpu" {
  description = "CPU units for router Fargate task (256 = 0.25 vCPU)"
  type        = number
  default     = 256
}

variable "router_memory" {
  description = "Memory (MB) for router Fargate task"
  type        = number
  default     = 512
}

variable "bedrock_model_id" {
  description = "Bedrock model ID for Claude"
  type        = string
  default     = "us-east-1.anthropic.claude-3-5-sonnet-20241022-v2:0"
}
