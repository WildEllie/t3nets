###############################################################################
# WebSocket API Gateway — real-time push to browser
#
# API Gateway WebSocket API with a thin Lambda proxy for $connect/$disconnect.
# The Lambda forwards connection lifecycle events to ECS via the internal ALB.
# ECS tracks connections in-memory and pushes results via the Management API.
#
# Lambda is NOT in the data path — it only handles connect/disconnect (~2 calls
# per session). The actual push (skill result → browser) goes directly from
# ECS via ApiGatewayManagementApi.post_to_connection().
###############################################################################

variable "project" { type = string }
variable "environment" { type = string }
variable "aws_region" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "alb_dns_name" { type = string }
variable "alb_security_group_id" { type = string }

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

# --- Lambda: thin proxy for $connect/$disconnect ---

data "archive_file" "ws_handler" {
  type        = "zip"
  output_path = "${path.module}/ws_handler.zip"

  source {
    content  = <<-PYTHON
import json
import urllib.request
import os
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ALB_DNS = os.environ.get("ALB_DNS", "")


def handler(event, context):
    rc = event.get("requestContext", {})
    route = rc.get("routeKey", "")
    connection_id = rc.get("connectionId", "")

    logger.info(f"WS proxy: route={route} conn={connection_id[:12]}")

    if route == "$connect":
        token = (event.get("queryStringParameters") or {}).get("token", "")
        ok = _forward(connection_id, route, token)
        if not ok:
            # Reject the WebSocket — browser onclose will reconnect
            return {"statusCode": 500, "body": "ECS registration failed"}
    elif route == "$disconnect":
        _forward(connection_id, route)

    return {"statusCode": 200}


def _forward(connection_id, route, token=""):
    """Forward to ECS via ALB. Returns True on success, False on failure."""
    if not ALB_DNS:
        logger.error("ALB_DNS not configured")
        return False
    headers = {
        "X-WS-Connection-Id": connection_id,
        "X-WS-Route": route,
        "Content-Type": "application/json",
    }
    body = json.dumps({"token": token}).encode() if token else b"{}"
    req = urllib.request.Request(
        f"http://{ALB_DNS}", data=body, headers=headers, method="POST"
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        status = resp.getcode()
        logger.info(f"WS proxy: forwarded {route} for {connection_id[:12]} status={status}")
        return 200 <= status < 300
    except Exception as e:
        logger.error(f"WS proxy: forward failed for {route} {connection_id[:12]}: {e}")
        return False
    PYTHON
    filename = "ws_handler.py"
  }
}

resource "aws_iam_role" "ws_lambda" {
  name = "${local.name_prefix}-ws-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action    = "sts:AssumeRole"
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ws_lambda_basic" {
  role       = aws_iam_role.ws_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "ws_lambda_vpc" {
  role       = aws_iam_role.ws_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_security_group" "ws_lambda" {
  name_prefix = "${local.name_prefix}-ws-lambda-"
  vpc_id      = var.vpc_id

  egress {
    description = "Reach ALB on port 80"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    security_groups = [var.alb_security_group_id]
  }

  tags = { Name = "${local.name_prefix}-ws-lambda-sg" }

  lifecycle { create_before_destroy = true }
}

resource "aws_cloudwatch_log_group" "ws_lambda" {
  name              = "/aws/lambda/${local.name_prefix}-ws-proxy"
  retention_in_days = 14
}

resource "aws_lambda_function" "ws_proxy" {
  function_name = "${local.name_prefix}-ws-proxy"
  role          = aws_iam_role.ws_lambda.arn
  handler       = "ws_handler.handler"
  runtime       = "python3.12"
  timeout       = 10
  memory_size   = 128

  filename         = data.archive_file.ws_handler.output_path
  source_code_hash = data.archive_file.ws_handler.output_base64sha256

  environment {
    variables = {
      ALB_DNS = var.alb_dns_name
    }
  }

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.ws_lambda.id]
  }

  depends_on = [aws_cloudwatch_log_group.ws_lambda]

  tags = { Name = "${local.name_prefix}-ws-proxy" }
}

# --- Integration (Lambda AWS_PROXY) ---

resource "aws_apigatewayv2_integration" "lambda" {
  api_id             = aws_apigatewayv2_api.websocket.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.ws_proxy.invoke_arn
}

# --- Lambda permission for API Gateway ---

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ws_proxy.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.websocket.execution_arn}/*/*"
}

# --- Routes ---

resource "aws_apigatewayv2_route" "connect" {
  api_id    = aws_apigatewayv2_api.websocket.id
  route_key = "$connect"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "disconnect" {
  api_id    = aws_apigatewayv2_api.websocket.id
  route_key = "$disconnect"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.websocket.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
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
