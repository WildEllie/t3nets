# T3nets — Sherpii environment
# Deploys to sherpii.ellieportugali.com
project          = "t3nets"
environment      = "sherpii"
aws_region       = "us-east-1"
router_image_tag = "latest"
router_cpu       = 256
router_memory    = 512
bedrock_model_id = "anthropic.claude-sonnet-4-5-20250929-v1:0"

# Async skills disabled for sherpii (no EventBridge/SQS/Lambda infra needed yet)
use_async_skills = false

# Ollama disabled
use_ollama = false

# Custom domain — subdomain of ellieportugali.com (zone already in Route53)
root_domain         = "ellieportugali.com"
dashboard_subdomain = "sherpii"
manage_route53_zone = false # zone ZBJ8I6KXWHW44 already exists; Terraform adds records only

cognito_callback_urls = [
  "http://localhost:8080/callback",
  "https://sherpii.ellieportugali.com/callback",
]
cognito_logout_urls = [
  "http://localhost:8080/login",
  "https://sherpii.ellieportugali.com/login",
]
