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
    name = "pk"   # {tenant_id}#channel#{user_id}
    type = "S"
  }

  attribute {
    name = "sk"   # {session_id}
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
    name = "pk"   # TENANT#{tenant_id} or USER#{tenant_id}
    type = "S"
  }

  attribute {
    name = "sk"   # META, USER#{user_id}, SKILL#{skill_name}
    type = "S"
  }

  # GSI for channel-to-tenant resolution
  attribute {
    name = "gsi1pk"   # CHANNEL#{channel_type}#{channel_specific_id}
    type = "S"
  }

  global_secondary_index {
    name            = "channel-mapping"
    hash_key        = "gsi1pk"
    projection_type = "ALL"
  }

  tags = { Name = "${local.name_prefix}-tenants" }
}

# --- Outputs ---

output "table_names" {
  value = {
    conversations = aws_dynamodb_table.conversations.name
    tenants       = aws_dynamodb_table.tenants.name
  }
}

output "table_arns" {
  value = [
    aws_dynamodb_table.conversations.arn,
    aws_dynamodb_table.tenants.arn,
    "${aws_dynamodb_table.tenants.arn}/index/*",
  ]
}
