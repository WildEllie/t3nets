###############################################################################
# WebSocket API Gateway — real-time push to browser
#
# API Gateway WebSocket API routed to the existing ECS container via VPC Link.
# No Lambda, no DynamoDB — connection tracking is in-memory on ECS.
# API Gateway converts WebSocket frames ↔ HTTP POST requests to ECS.
###############################################################################

variable "project" { type = string }
variable "environment" { type = string }
variable "aws_region" { type = string }
variable "vpc_link_id" { type = string }
variable "alb_listener_arn" { type = string }

locals {
  name_prefix = "${var.project}-${var.environment}"
}

# --- WebSocket API ---

resource "aws_apigatewayv2_api" "websocket" {
  name                       = "${local.name_prefix}-ws"
  protocol_type              = "WEBSOCKET"
  route_selection_expression = "$request.body.action"

  tags = { Name = "${local.name_prefix}-ws" }
}

# --- Integration (ALB via VPC Link) ---
# WebSocket API Gateway sends HTTP POST to ECS for each route.

resource "aws_apigatewayv2_integration" "alb" {
  api_id             = aws_apigatewayv2_api.websocket.id
  integration_type   = "HTTP_PROXY"
  integration_method = "POST"
  connection_type    = "VPC_LINK"
  connection_id      = var.vpc_link_id
  integration_uri    = var.alb_listener_arn

  # Pass route key and connection ID to ECS via request parameters
  request_parameters = {
    "integration.request.header.X-WS-Route"         = "context.routeKey"
    "integration.request.header.X-WS-Connection-Id"  = "context.connectionId"
    "integration.request.header.X-WS-Event-Type"     = "context.eventType"
    "integration.request.querystring.token"           = "route.request.querystring.token"
  }
}

# --- Routes ---

resource "aws_apigatewayv2_route" "connect" {
  api_id    = aws_apigatewayv2_api.websocket.id
  route_key = "$connect"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

resource "aws_apigatewayv2_route" "disconnect" {
  api_id    = aws_apigatewayv2_api.websocket.id
  route_key = "$disconnect"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.websocket.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

# --- Stage (auto-deploy) ---

resource "aws_apigatewayv2_stage" "prod" {
  api_id      = aws_apigatewayv2_api.websocket.id
  name        = "prod"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 100
    throttling_rate_limit  = 50
  }

  tags = { Name = "${local.name_prefix}-ws-prod" }
}

# --- Outputs ---

output "ws_api_id" {
  description = "WebSocket API Gateway ID"
  value       = aws_apigatewayv2_api.websocket.id
}

output "ws_api_endpoint" {
  description = "WebSocket endpoint (wss://) for browser connections"
  value       = aws_apigatewayv2_stage.prod.invoke_url
}

output "ws_management_endpoint" {
  description = "HTTPS endpoint for post_to_connection() calls"
  value       = "https://${aws_apigatewayv2_api.websocket.id}.execute-api.${var.aws_region}.amazonaws.com/prod"
}

output "ws_api_arn" {
  description = "WebSocket API ARN for IAM policy"
  value       = aws_apigatewayv2_api.websocket.execution_arn
}
