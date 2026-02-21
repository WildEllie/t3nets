###############################################################################
# API Gateway — HTTP API (v2)
#
# Routes traffic to the internal ALB via VPC Link.
# Uses $default catch-all route — all paths forwarded to ALB.
###############################################################################

variable "project" { type = string }
variable "environment" { type = string }
variable "alb_listener_arn" { type = string }
variable "alb_dns_name" { type = string }
variable "vpc_link_id" { type = string }

locals {
  name_prefix = "${var.project}-${var.environment}"
}

# --- HTTP API ---

resource "aws_apigatewayv2_api" "main" {
  name          = "${local.name_prefix}-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["Content-Type", "Authorization"]
    max_age       = 3600
  }

  tags = { Name = "${local.name_prefix}-api" }
}

# --- Integration (ALB via VPC Link) ---

resource "aws_apigatewayv2_integration" "alb" {
  api_id             = aws_apigatewayv2_api.main.id
  integration_type   = "HTTP_PROXY"
  integration_method = "ANY"
  connection_type    = "VPC_LINK"
  connection_id      = var.vpc_link_id
  integration_uri    = var.alb_listener_arn
}

# --- Catch-all route (forward everything to ALB) ---

resource "aws_apigatewayv2_route" "catch_all" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

# --- Stage (auto-deploy) ---

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api.arn
    format = jsonencode({
      requestId   = "$context.requestId"
      ip          = "$context.identity.sourceIp"
      method      = "$context.httpMethod"
      path        = "$context.path"
      status      = "$context.status"
      latency     = "$context.responseLatency"
      integration = "$context.integrationLatency"
    })
  }

  tags = { Name = "${local.name_prefix}-api-default" }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/apigateway/${local.name_prefix}"
  retention_in_days = 14
}

# --- Outputs ---

output "api_endpoint" {
  value = aws_apigatewayv2_stage.default.invoke_url
}

output "api_id" {
  value = aws_apigatewayv2_api.main.id
}
