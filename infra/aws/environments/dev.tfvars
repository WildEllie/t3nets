# T3nets — Dev environment
project          = "t3nets"
environment      = "dev"
aws_region       = "us-east-1"
router_image_tag = "latest"
router_cpu       = 256    # 0.25 vCPU (minimum, cheap)
router_memory    = 512    # 512 MB
bedrock_model_id = "anthropic.claude-sonnet-4-5-20250929-v1:0"

# Cognito callback URLs — update after first deploy to include the API Gateway URL
# e.g. ["https://<api-id>.execute-api.us-east-1.amazonaws.com/callback"]
cognito_callback_urls = ["http://localhost:8080/callback"]
cognito_logout_urls   = ["http://localhost:8080/login"]
