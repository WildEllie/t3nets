# T3nets — Production environment
project          = "t3nets"
environment      = "prod"
aws_region       = "us-east-1"
router_image_tag = "latest"
router_cpu       = 512    # 0.5 vCPU (bump for production traffic)
router_memory    = 1024   # 1 GB
bedrock_model_id = "anthropic.claude-sonnet-4-5-20250929-v1:0"
