###############################################################################
# Compute — ECS Fargate (Router) + ALB
#
# The router runs as an always-warm Fargate task behind an internal ALB.
# API Gateway connects to the ALB via a VPC Link.
###############################################################################

variable "project" { type = string }
variable "environment" { type = string }
variable "aws_region" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "public_subnet_ids" { type = list(string) }
variable "ecr_repository_url" { type = string }
variable "router_image_tag" { type = string }
variable "dynamodb_table_arns" { type = list(string) }
variable "secrets_base_arn" { type = string }
variable "router_cpu" { type = number }
variable "router_memory" { type = number }
variable "bedrock_model_id" { type = string }
variable "cognito_user_pool_id" { type = string }
variable "cognito_app_client_id" { type = string }
variable "cognito_auth_domain" { type = string }

# --- Phase 3b: Async Skills ---
variable "pending_requests_table_arn" {
  description = "ARN of the pending-requests DynamoDB table"
  type        = string
}
variable "pending_requests_table_name" {
  description = "Name of the pending-requests DynamoDB table"
  type        = string
}
variable "secrets_prefix" {
  description = "Secrets Manager path prefix for tenant secrets"
  type        = string
}
variable "s3_bucket_arn" {
  description = "ARN of the S3 static bucket (for BlobStore practice persistence)"
  type        = string
  default     = ""
}
variable "lambda_memory_size" {
  description = "Memory (MB) for the skill executor Lambda"
  type        = number
  default     = 512
}
variable "use_async_skills" {
  description = "Feature flag: enable async skill execution via EventBridge+Lambda+SQS"
  type        = bool
  default     = false
}

# WebSocket API (real-time push)
variable "ws_api_endpoint" {
  description = "wss:// endpoint for browser WebSocket connections"
  type        = string
  default     = ""
}

variable "ws_connections_table_name" {
  description = "Name of the ws-connections DynamoDB table"
  type        = string
  default     = ""
}

# Phase 5c: Ollama sidecar
variable "use_ollama" {
  description = "Feature flag: run Ollama as a sidecar container for free AI"
  type        = bool
  default     = false
}

variable "ollama_model" {
  description = "Ollama model to pull on startup"
  type        = string
  default     = "llama3.2:3b"
}

variable "ollama_memory_mb" {
  description = "Memory (MB) for Ollama container. Task total = router_memory + this."
  type        = number
  default     = 4096
}


locals {
  name_prefix = "${var.project}-${var.environment}"

  # When Ollama is enabled the task must accommodate both containers.
  # Fargate requires CPU >= 1024 to support 4 GB+ memory configurations.
  effective_cpu    = var.use_ollama ? max(var.router_cpu, 1024) : var.router_cpu
  effective_memory = var.use_ollama ? var.router_memory + var.ollama_memory_mb : var.router_memory
}

data "aws_caller_identity" "current" {}

# --- CloudWatch Log Group ---

resource "aws_cloudwatch_log_group" "router" {
  name              = "/ecs/${local.name_prefix}-router"
  retention_in_days = 14

  tags = { Name = "${local.name_prefix}-router-logs" }
}

resource "aws_cloudwatch_log_group" "ollama" {
  count             = var.use_ollama ? 1 : 0
  name              = "/ecs/${local.name_prefix}-ollama"
  retention_in_days = 14

  tags = { Name = "${local.name_prefix}-ollama-logs" }
}

# --- Security Groups ---

resource "aws_security_group" "alb" {
  name_prefix = "${local.name_prefix}-alb-"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTP from anywhere (API Gateway VPC Link)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-alb-sg" }

  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "router" {
  name_prefix = "${local.name_prefix}-router-"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Traffic from ALB"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-router-sg" }

  lifecycle { create_before_destroy = true }
}

# --- ALB (internal, API Gateway connects via VPC Link) ---

resource "aws_lb" "router" {
  name               = "${local.name_prefix}-router"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.private_subnet_ids

  tags = { Name = "${local.name_prefix}-router-alb" }
}

resource "aws_lb_target_group" "router" {
  name        = "${local.name_prefix}-router"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/health"
    port                = "traffic-port"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
  }

  tags = { Name = "${local.name_prefix}-router-tg" }
}

resource "aws_lb_listener" "router" {
  load_balancer_arn = aws_lb.router.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.router.arn
  }
}

# --- VPC Link (for API Gateway → ALB) ---

resource "aws_apigatewayv2_vpc_link" "main" {
  name               = "${local.name_prefix}-vpc-link"
  subnet_ids         = var.private_subnet_ids
  security_group_ids = [aws_security_group.alb.id]

  tags = { Name = "${local.name_prefix}-vpc-link" }
}

# --- IAM Role for ECS Task ---

resource "aws_iam_role" "ecs_task_execution" {
  name = "${local.name_prefix}-ecs-task-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action    = "sts:AssumeRole"
        Effect    = "Allow"
        Principal = { Service = "ecs-tasks.amazonaws.com" }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task role (what the container can do at runtime)
resource "aws_iam_role" "ecs_task" {
  name = "${local.name_prefix}-ecs-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action    = "sts:AssumeRole"
        Effect    = "Allow"
        Principal = { Service = "ecs-tasks.amazonaws.com" }
      }
    ]
  })
}

resource "aws_iam_role_policy" "ecs_task" {
  name = "${local.name_prefix}-ecs-task-policy"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDB"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        Resource = var.dynamodb_table_arns
      },
      {
        Sid    = "SecretsManagerList"
        Effect = "Allow"
        Action = [
          "secretsmanager:ListSecrets",
        ]
        Resource = "*"
      },
      {
        Sid    = "SecretsManagerGetPut"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:CreateSecret",
          "secretsmanager:UpdateSecret",
          "secretsmanager:TagResource",
        ]
        Resource = var.secrets_base_arn
      },
      {
        Sid    = "Bedrock"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:Converse",
        ]
        # Geographic cross-region inference (us. prefix) routes to us-east-1,
        # us-east-2, and us-west-2 — need access in all three regions.
        Resource = [
          "arn:aws:bedrock:us-east-1::foundation-model/*",
          "arn:aws:bedrock:us-east-2::foundation-model/*",
          "arn:aws:bedrock:us-west-2::foundation-model/*",
          "arn:aws:bedrock:us-east-1:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:us-east-2:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:us-west-2:${data.aws_caller_identity.current.account_id}:inference-profile/*",
        ]
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.router.arn}:*"
      },
      # Phase 3b: Async skill execution
      {
        Sid    = "EventBridgePublish"
        Effect = "Allow"
        Action = [
          "events:PutEvents",
        ]
        Resource = aws_cloudwatch_event_bus.skills.arn
      },
      {
        Sid    = "SQSReceiveResults"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
        ]
        Resource = aws_sqs_queue.skill_results.arn
      },
      {
        Sid    = "DynamoDBPendingRequests"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
        ]
        Resource = var.pending_requests_table_arn
      },
      {
        Sid    = "S3BlobStore"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
        ]
        Resource = var.s3_bucket_arn != "" ? [
          var.s3_bucket_arn,
          "${var.s3_bucket_arn}/*",
        ] : []
      },
    ]
  })
}

# --- ECS Cluster ---

resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "disabled" # Enable in prod
  }

  tags = { Name = "${local.name_prefix}-cluster" }
}

# --- ECS Task Definition ---

resource "aws_ecs_task_definition" "router" {
  family                   = "${local.name_prefix}-router"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = local.effective_cpu
  memory                   = local.effective_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode(concat(
    [
      {
        name      = "router"
        image     = "${var.ecr_repository_url}:${var.router_image_tag}"
        essential = true

        portMappings = [
          {
            containerPort = 8080
            protocol      = "tcp"
          }
        ]

        # Wait for Ollama to be healthy before starting router (only when use_ollama=true)
        dependsOn = var.use_ollama ? [
          { containerName = "ollama", condition = "HEALTHY" }
        ] : []

        environment = concat(
          [
            { name = "T3NETS_PLATFORM", value = "aws" },
            { name = "T3NETS_STAGE", value = var.environment },
            { name = "AWS_REGION", value = var.aws_region },
            { name = "BEDROCK_MODEL_ID", value = var.bedrock_model_id },
            { name = "DYNAMODB_CONVERSATIONS_TABLE", value = "${local.name_prefix}-conversations" },
            { name = "DYNAMODB_TENANTS_TABLE", value = "${local.name_prefix}-tenants" },
            { name = "SECRETS_PREFIX", value = "/${var.project}/${var.environment}/tenants" },
            { name = "COGNITO_USER_POOL_ID", value = var.cognito_user_pool_id },
            { name = "COGNITO_APP_CLIENT_ID", value = var.cognito_app_client_id },
            { name = "COGNITO_AUTH_DOMAIN", value = var.cognito_auth_domain },
            { name = "USE_ASYNC_SKILLS", value = tostring(var.use_async_skills) },
            { name = "EVENTBRIDGE_BUS_NAME", value = aws_cloudwatch_event_bus.skills.name },
            { name = "SQS_RESULTS_QUEUE_URL", value = aws_sqs_queue.skill_results.id },
            { name = "PENDING_REQUESTS_TABLE", value = var.pending_requests_table_name },
            { name = "WS_API_ENDPOINT", value = var.ws_api_endpoint },
            { name = "WS_CONNECTIONS_TABLE", value = var.ws_connections_table_name },
          ],
          # Phase 5c: wire Ollama sidecar URL only when enabled
          var.use_ollama ? [
            { name = "OLLAMA_API_URL", value = "http://localhost:11434" }
          ] : []
        )

        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.router.name
            "awslogs-region"        = var.aws_region
            "awslogs-stream-prefix" = "router"
          }
        }

        healthCheck = {
          command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8080/health')\" || exit 1"]
          interval    = 30
          timeout     = 5
          retries     = 3
          startPeriod = 10
        }
      }
    ],

    # Phase 5c: Ollama sidecar — only included when use_ollama = true.
    # ECS awsvpc mode: containers in the same task share the network namespace.
    # The router reaches Ollama via localhost:11434 — no security group changes needed.
    var.use_ollama ? [
      {
        name      = "ollama"
        image     = "ollama/ollama:latest"
        essential = false # Graceful degradation: router stays up if Ollama crashes post-startup

        environment = [
          # Keep model loaded indefinitely (no auto-unload)
          { name = "OLLAMA_KEEP_ALIVE", value = "-1" }
        ]

        # Start serve, wait until ready, pull the configured model, then keep serving.
        # Terraform interpolates var.ollama_model at plan time → literal model name in the command.
        entryPoint = ["sh", "-c"]
        command    = ["/bin/ollama serve & until /bin/ollama list >/dev/null 2>&1; do sleep 2; done && /bin/ollama pull ${var.ollama_model} && echo 'Ollama: model ready' && wait"]

        healthCheck = {
          command     = ["CMD-SHELL", "/bin/ollama list >/dev/null 2>&1 || exit 1"]
          interval    = 30
          timeout     = 10
          retries     = 5
          startPeriod = 180 # Allow up to 3 min for model download on first start
        }

        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.ollama[0].name
            "awslogs-region"        = var.aws_region
            "awslogs-stream-prefix" = "ollama"
          }
        }

        portMappings = []
        mountPoints  = []
        volumesFrom  = []
      }
    ] : []
  ))

  tags = { Name = "${local.name_prefix}-router" }
}

# --- ECS Service ---

resource "aws_ecs_service" "router" {
  name            = "${local.name_prefix}-router"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.router.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.router.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.router.arn
    container_name   = "router"
    container_port   = 8080
  }

  # Allow rolling deployments
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  depends_on = [aws_lb_listener.router]

  tags = { Name = "${local.name_prefix}-router" }
}

# --- Outputs ---

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "service_name" {
  value = aws_ecs_service.router.name
}

output "alb_dns_name" {
  value = aws_lb.router.dns_name
}

output "alb_listener_arn" {
  value = aws_lb_listener.router.arn
}

output "vpc_link_id" {
  value = aws_apigatewayv2_vpc_link.main.id
}

output "router_security_group_id" {
  value = aws_security_group.router.id
}

output "alb_security_group_id" {
  value = aws_security_group.alb.id
}

# Phase 3b outputs
output "skill_ping_function_name" {
  value = aws_lambda_function.skill_ping.function_name
}

output "skill_ping_function_arn" {
  value = aws_lambda_function.skill_ping.arn
}

output "lambda_role_arn" {
  description = "Shared IAM role ARN for all skill Lambdas (used by deploy.sh)"
  value       = aws_iam_role.lambda_skill_executor.arn
}

output "eventbridge_bus_name" {
  value = aws_cloudwatch_event_bus.skills.name
}

output "eventbridge_bus_arn" {
  value = aws_cloudwatch_event_bus.skills.arn
}

output "eventbridge_dlq_arn" {
  value = aws_sqs_queue.eventbridge_dlq.arn
}

output "sqs_results_queue_url" {
  value = aws_sqs_queue.skill_results.id
}

output "sqs_results_queue_arn" {
  value = aws_sqs_queue.skill_results.arn
}

output "pending_requests_table_name" {
  value = var.pending_requests_table_name
}

output "secrets_prefix" {
  value = var.secrets_prefix
}

output "ecs_task_role_id" {
  description = "ECS task IAM role ID — for attaching additional policies from root module"
  value       = aws_iam_role.ecs_task.id
}

output "ecs_task_definition_family" {
  description = "ECS task definition family name"
  value       = aws_ecs_task_definition.router.family
}
