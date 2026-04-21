# T3nets — Dev environment
project          = "t3nets"
environment      = "dev"
aws_region       = "us-east-1"
router_image_tag = "latest"
router_cpu       = 256 # 0.25 vCPU (minimum, cheap)
router_memory    = 512 # 512 MB
bedrock_model_id = "anthropic.claude-sonnet-4-5-20250929-v1:0"

# Phase 3b: Async skill execution via EventBridge + Lambda + SQS
use_async_skills = true

# Phase 5c: Ollama sidecar — disabled on AWS (Bedrock Llama 3.2 1B is cheaper at low volume)
use_ollama = false

# Cognito callback URLs — localhost for local dev, API Gateway for deployed app
cognito_callback_urls = [
  "http://localhost:8080/callback",
  "https://i9yxlqqro8.execute-api.us-east-1.amazonaws.com/callback",
  "https://d3ma51b4qocpkj.cloudfront.net/callback"
]
cognito_logout_urls = [
  "http://localhost:8080/login",
  "https://i9yxlqqro8.execute-api.us-east-1.amazonaws.com/login",
  "https://d3ma51b4qocpkj.cloudfront.net/login"
]

# Custom domain — dashboard served at https://www.t3nets.dev
# (the callback/logout URLs for the custom domain are appended automatically).
root_domain         = "t3nets.dev"
dashboard_subdomain = "www"
manage_route53_zone = true
