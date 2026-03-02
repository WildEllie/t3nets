###############################################################################
# DynamoDB — WebSocket Connections
#
# Tracks active API Gateway WebSocket connection IDs. Enables:
# 1. Any ECS task to fan-out push events to all user connections
# 2. Horizontal scaling — no per-instance in-memory state
# 3. Auto-cleanup via TTL (2 hours = API Gateway max connection duration)
#
# Schema:
#   pk:       connection_id   (hash key)
#   user_key: email/sub       (GSI hash key for fan-out by user)
#   ttl:      Unix epoch (now + 7200s)
#
# GSI: user-connections-index — hash_key=user_key, projection=ALL
#   Enables Query by user to get all active connection IDs.
###############################################################################

resource "aws_dynamodb_table" "ws_connections" {
  name         = "${local.name_prefix}-ws-connections"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk" # connection_id
    type = "S"
  }

  attribute {
    name = "user_key" # email or sub — GSI hash key
    type = "S"
  }

  global_secondary_index {
    name            = "user-connections-index"
    hash_key        = "user_key"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = { Name = "${local.name_prefix}-ws-connections" }
}
