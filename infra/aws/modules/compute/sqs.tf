###############################################################################
# SQS — Skill Results Queue
#
# Lambda writes skill execution results here after completing.
# Router background thread long-polls this queue (WaitTimeSeconds=20),
# resolves the pending request, and routes the response to the channel.
###############################################################################

# --- Results Queue ---

resource "aws_sqs_queue" "skill_results" {
  name                       = "${local.name_prefix}-skill-results"
  visibility_timeout_seconds = 30  # Must exceed processing time
  message_retention_seconds  = 300 # 5 minutes — matches pending request TTL
  receive_wait_time_seconds  = 20  # Long-polling (maximum, near-zero cost when idle)

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.skill_results_dlq.arn
    maxReceiveCount     = 3 # After 3 failed processing attempts → DLQ
  })

  tags = { Name = "${local.name_prefix}-skill-results" }
}

# --- DLQ for results that fail processing ---

resource "aws_sqs_queue" "skill_results_dlq" {
  name                       = "${local.name_prefix}-skill-results-dlq"
  message_retention_seconds  = 1209600 # 14 days
  visibility_timeout_seconds = 30

  tags = { Name = "${local.name_prefix}-skill-results-dlq" }
}
