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
variable "cognito_user_pool_endpoint" {
  description = "Cognito issuer URL for JWT validation"
  type        = string
  default     = ""
}
variable "cognito_app_client_id" {
  description = "Cognito app client ID (audience for JWT)"
  type        = string
  default     = ""
}

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

# --- JWT Authorizer (Cognito) ---

resource "aws_apigatewayv2_authorizer" "cognito" {
  count = var.cognito_user_pool_endpoint != "" ? 1 : 0

  api_id           = aws_apigatewayv2_api.main.id
  authorizer_type  = "JWT"
  name             = "${local.name_prefix}-cognito"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    audience = [var.cognito_app_client_id]
    issuer   = var.cognito_user_pool_endpoint
  }
}

# --- Routes ---

# Public routes (no auth): health check, static pages, login, callback
resource "aws_apigatewayv2_route" "public_health" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "GET /health"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

resource "aws_apigatewayv2_route" "public_health_api" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "GET /api/health"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

resource "aws_apigatewayv2_route" "public_login" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "GET /login"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

resource "aws_apigatewayv2_route" "public_callback" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "GET /callback"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

# Public routes: UI pages that must load before auth (JS checks tokens client-side)
resource "aws_apigatewayv2_route" "public_root" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "GET /"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

resource "aws_apigatewayv2_route" "public_chat" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "GET /chat"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

resource "aws_apigatewayv2_route" "public_settings_page" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "GET /settings"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

resource "aws_apigatewayv2_route" "public_auth_config" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "GET /api/auth/config"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

# Authenticated catch-all (requires JWT when Cognito is configured)
resource "aws_apigatewayv2_route" "catch_all" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"

  # Attach JWT authorizer when Cognito is configured
  authorization_type = var.cognito_user_pool_endpoint != "" ? "JWT" : "NONE"
  authorizer_id      = var.cognito_user_pool_endpoint != "" ? aws_apigatewayv2_authorizer.cognito[0].id : null
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
