###############################################################################
# DynamoDB — Pending Requests
#
# Tracks in-flight async skill invocations. Enables:
# 1. Any router instance to pick up any result (horizontal scaling)
# 2. Lambda idempotency (check status before executing)
# 3. Channel context recovery (service_url for Teams, reply_target, etc.)
#
# Schema:
#   pk: {request_id}
#   Attributes: tenant_id, skill_name, channel, conversation_id,
#               reply_target, service_url, is_raw, status, user_key,
#               created_at
#   TTL: ttl (Unix epoch, 5 min after creation — auto-cleanup)
#
# Status flow: pending → completed
###############################################################################

resource "aws_dynamodb_table" "pending_requests" {
  name         = "${local.name_prefix}-pending-requests"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk" # {request_id}
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = { Name = "${local.name_prefix}-pending-requests" }
}
