###############################################################################
# Data — DynamoDB tables
#
# Tables:
#   conversations  — chat history per tenant/user
#   tenants        — tenant metadata, settings, channel mappings
###############################################################################

variable "project" { type = string }
variable "environment" { type = string }

locals {
  name_prefix = "${var.project}-${var.environment}"
}

# --- Conversations table ---

resource "aws_dynamodb_table" "conversations" {
  name         = "${local.name_prefix}-conversations"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk" # {tenant_id}#channel#{user_id}
    type = "S"
  }

  attribute {
    name = "sk" # {session_id}
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = { Name = "${local.name_prefix}-conversations" }
}

# --- Tenants table ---

resource "aws_dynamodb_table" "tenants" {
  name         = "${local.name_prefix}-tenants"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk" # TENANT#{tenant_id} or USER#{tenant_id}
    type = "S"
  }

  attribute {
    name = "sk" # META, USER#{user_id}, SKILL#{skill_name}
    type = "S"
  }

  # GSI for channel-to-tenant resolution
  attribute {
    name = "gsi1pk" # CHANNEL#{channel_type}#{channel_specific_id}
    type = "S"
  }

  global_secondary_index {
    name            = "channel-mapping"
    hash_key        = "gsi1pk"
    projection_type = "ALL"
  }

  # GSI for cognito_sub → user lookup (cross-tenant)
  attribute {
    name = "gsi2pk" # COGNITO#{cognito_sub}
    type = "S"
  }

  global_secondary_index {
    name            = "cognito-sub-lookup"
    hash_key        = "gsi2pk"
    projection_type = "ALL"
  }

  # TTL for invitation auto-cleanup
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = { Name = "${local.name_prefix}-tenants" }
}

# --- Outputs ---

output "table_names" {
  value = {
    conversations    = aws_dynamodb_table.conversations.name
    tenants          = aws_dynamodb_table.tenants.name
    pending_requests = aws_dynamodb_table.pending_requests.name
  }
}

output "table_arns" {
  value = [
    aws_dynamodb_table.conversations.arn,
    aws_dynamodb_table.tenants.arn,
    "${aws_dynamodb_table.tenants.arn}/index/*",
    aws_dynamodb_table.ws_connections.arn,
    "${aws_dynamodb_table.ws_connections.arn}/index/*",
  ]
}

output "pending_requests_table_arn" {
  value = aws_dynamodb_table.pending_requests.arn
}

output "pending_requests_table_name" {
  value = aws_dynamodb_table.pending_requests.name
}

output "ws_connections_table_arn" {
  value = aws_dynamodb_table.ws_connections.arn
}

output "ws_connections_table_name" {
  value = aws_dynamodb_table.ws_connections.name
}
