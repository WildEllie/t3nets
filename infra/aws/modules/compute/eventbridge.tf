###############################################################################
# EventBridge — Skill Invocation Bus
#
# Custom event bus receives skill.invoke events from the router.
# A rule routes all events to the Lambda skill executor.
# Failed invocations go to a dead-letter SQS queue.
###############################################################################

# --- Event Bus ---

resource "aws_cloudwatch_event_bus" "skills" {
  name = "${local.name_prefix}-skills"

  tags = { Name = "${local.name_prefix}-skills-bus" }
}

# --- DLQ for failed EventBridge → Lambda invocations ---

resource "aws_sqs_queue" "eventbridge_dlq" {
  name                      = "${local.name_prefix}-eventbridge-dlq"
  message_retention_seconds = 1209600  # 14 days
  visibility_timeout_seconds = 30

  tags = { Name = "${local.name_prefix}-eventbridge-dlq" }
}

# --- EventBridge Rule → Lambda ---

resource "aws_cloudwatch_event_rule" "skill_invoke" {
  name           = "${local.name_prefix}-skill-invoke"
  event_bus_name = aws_cloudwatch_event_bus.skills.name
  description    = "Route skill.invoke events to Lambda executor"

  event_pattern = jsonencode({
    source      = ["agent.router"]
    detail-type = ["skill.invoke"]
  })

  tags = { Name = "${local.name_prefix}-skill-invoke-rule" }
}

resource "aws_cloudwatch_event_target" "skill_lambda" {
  rule           = aws_cloudwatch_event_rule.skill_invoke.name
  event_bus_name = aws_cloudwatch_event_bus.skills.name
  target_id      = "skill-executor"
  arn            = aws_lambda_function.skill_executor.arn

  # Retry policy: 2 retries (default). Safe because Lambda checks
  # idempotency via DynamoDB pending request status before executing.
  retry_policy {
    maximum_retry_attempts       = 2
    maximum_event_age_in_seconds = 300  # 5 minutes, matches pending request TTL
  }

  dead_letter_config {
    arn = aws_sqs_queue.eventbridge_dlq.arn
  }
}

# --- Lambda permission for EventBridge to invoke ---

resource "aws_lambda_permission" "eventbridge_invoke" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.skill_executor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.skill_invoke.arn
}

# --- IAM: allow EventBridge to send to DLQ ---

resource "aws_sqs_queue_policy" "eventbridge_dlq" {
  queue_url = aws_sqs_queue.eventbridge_dlq.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowEventBridgeSend"
        Effect = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.eventbridge_dlq.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_cloudwatch_event_rule.skill_invoke.arn
          }
        }
      }
    ]
  })
}
