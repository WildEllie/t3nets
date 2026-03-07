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
  description = "Base Bedrock model ID — aws_region is prepended automatically for inference profile"
  type        = string
  # No default — must be set in .tfvars per environment
  # Example: "anthropic.claude-sonnet-4-5-20250929-v1:0" (foundation model, single-region)
}

variable "cognito_callback_urls" {
  description = "Allowed OAuth callback URLs for Cognito"
  type        = list(string)
  default     = ["http://localhost:8080/callback"]
}

variable "cognito_logout_urls" {
  description = "Allowed logout redirect URLs for Cognito"
  type        = list(string)
  default     = ["http://localhost:8080/login"]
}

# Phase 3b: Async skills
variable "use_async_skills" {
  description = "Feature flag: enable async skill execution via EventBridge+Lambda+SQS"
  type        = bool
  default     = false
}

variable "use_ollama" {
  description = "Feature flag: run Ollama as a sidecar container in the ECS task for free local AI"
  type        = bool
  default     = false
}

variable "ollama_model" {
  description = "Ollama model to pull on container startup (e.g. llama3.2:3b, llama3.1:8b, mistral:7b)"
  type        = string
  default     = "llama3.2:3b"
}

variable "ollama_memory_mb" {
  description = "Memory (MB) allocated to the Ollama sidecar container. Task total = router_memory + ollama_memory_mb"
  type        = number
  default     = 4096
}
