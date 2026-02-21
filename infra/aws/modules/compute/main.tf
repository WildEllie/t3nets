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
        Action = "sts:AssumeRole"
        Effect = "Allow"
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
        Action = "sts:AssumeRole"
        Effect = "Allow"
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
        ]
        Resource = var.secrets_base_arn
      },
      {
        Sid    = "Bedrock"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = [
                  "arn:aws:bedrock:*::foundation-model/*",
                  "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*"
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
    ]
  })
}

# --- ECS Cluster ---

resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "disabled"  # Enable in prod
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
        { name = "T3NETS_ENV", value = "aws" },
        { name = "AWS_REGION", value = var.aws_region },
        { name = "BEDROCK_MODEL_ID", value = var.bedrock_model_id },
        { name = "DYNAMODB_CONVERSATIONS_TABLE", value = "${local.name_prefix}-conversations" },
        { name = "DYNAMODB_TENANTS_TABLE", value = "${local.name_prefix}-tenants" },
        { name = "SECRETS_PREFIX", value = "/${var.project}/${var.environment}/tenants" },
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
