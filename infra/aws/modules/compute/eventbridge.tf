###############################################################################
# EventBridge — Skill Invocation Bus
#
# Custom event bus receives skill.invoke events from the router.
# Terraform manages the ping skill rule. Domain skill rules (sprint_status,
# release_notes) are created by scripts/deploy.sh.
# Failed invocations go to a dead-letter SQS queue.
###############################################################################

# --- Event Bus ---

resource "aws_cloudwatch_event_bus" "skills" {
  name = "${local.name_prefix}-skills"

  tags = { Name = "${local.name_prefix}-skills-bus" }
}

# --- DLQ for failed EventBridge → Lambda invocations ---

resource "aws_sqs_queue" "eventbridge_dlq" {
  name                       = "${local.name_prefix}-eventbridge-dlq"
  message_retention_seconds  = 1209600 # 14 days
  visibility_timeout_seconds = 30

  tags = { Name = "${local.name_prefix}-eventbridge-dlq" }
}

# --- EventBridge Rule → Ping Lambda ---

resource "aws_cloudwatch_event_rule" "skill_invoke_ping" {
  name           = "${local.name_prefix}-skill-invoke-ping"
  event_bus_name = aws_cloudwatch_event_bus.skills.name
  description    = "Route ping skill invocations to the ping Lambda"

  event_pattern = jsonencode({
    source      = ["agent.router"]
    detail-type = ["skill.invoke"]
    detail = {
      skill_name = ["ping"]
    }
  })

  tags = { Name = "${local.name_prefix}-skill-invoke-ping-rule" }
}

resource "aws_cloudwatch_event_target" "skill_ping_lambda" {
  rule           = aws_cloudwatch_event_rule.skill_invoke_ping.name
  event_bus_name = aws_cloudwatch_event_bus.skills.name
  target_id      = "skill-ping"
  arn            = aws_lambda_function.skill_ping.arn

  # Retry policy: 2 retries (default). Safe because Lambda checks
  # idempotency via DynamoDB pending request status before executing.
  retry_policy {
    maximum_retry_attempts       = 2
    maximum_event_age_in_seconds = 300 # 5 minutes, matches pending request TTL
  }

  dead_letter_config {
    arn = aws_sqs_queue.eventbridge_dlq.arn
  }
}

# --- Lambda permission for EventBridge to invoke ---

resource "aws_lambda_permission" "eventbridge_invoke_ping" {
  statement_id  = "AllowEventBridgeInvokePing"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.skill_ping.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.skill_invoke_ping.arn
}

# --- IAM: allow EventBridge to send to DLQ ---

resource "aws_sqs_queue_policy" "eventbridge_dlq" {
  queue_url = aws_sqs_queue.eventbridge_dlq.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowEventBridgeSend"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.eventbridge_dlq.arn
        Condition = {
          ArnLike = {
            "aws:SourceArn" = "arn:aws:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:rule/${aws_cloudwatch_event_bus.skills.name}/*"
          }
        }
      }
    ]
  })
}
