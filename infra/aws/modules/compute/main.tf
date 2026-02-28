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


locals {
  name_prefix = "${var.project}-${var.environment}"
}

data "aws_caller_identity" "current" {}

# --- CloudWatch Log Group ---

resource "aws_cloudwatch_log_group" "router" {
  name              = "/ecs/${local.name_prefix}-router"
  retention_in_days = 14

  tags = { Name = "${local.name_prefix}-router-logs" }
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
  cpu                      = var.router_cpu
  memory                   = var.router_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
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

      environment = [
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
        # Phase 3b: Async skills
        { name = "USE_ASYNC_SKILLS", value = tostring(var.use_async_skills) },
        { name = "EVENTBRIDGE_BUS_NAME", value = aws_cloudwatch_event_bus.skills.name },
        { name = "SQS_RESULTS_QUEUE_URL", value = aws_sqs_queue.skill_results.id },
        { name = "PENDING_REQUESTS_TABLE", value = var.pending_requests_table_name },
        # WebSocket API (real-time push — derived from wss:// endpoint at runtime)
        { name = "WS_API_ENDPOINT", value = var.ws_api_endpoint },
      ]

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
  ])

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
