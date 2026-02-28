###############################################################################
# Lambda — Skill Executor
#
# Single Lambda function that executes all skills. Dispatches by skill_name
# from the EventBridge event payload. Checks DynamoDB for idempotency
# before executing, then publishes results to SQS.
#
# Packaging: ZIP from the project root (agent/ + adapters/aws/ dirs).
# Dependencies are lazy-loaded per skill to minimize cold start.
###############################################################################

# --- CloudWatch Log Group ---

resource "aws_cloudwatch_log_group" "skill_executor" {
  name              = "/aws/lambda/${local.name_prefix}-skill-executor"
  retention_in_days = 14

  tags = { Name = "${local.name_prefix}-skill-executor-logs" }
}

# --- IAM Role for Lambda ---

resource "aws_iam_role" "lambda_skill_executor" {
  name = "${local.name_prefix}-lambda-skill-executor"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
      }
    ]
  })

  tags = { Name = "${local.name_prefix}-lambda-skill-executor" }
}

resource "aws_iam_role_policy" "lambda_skill_executor" {
  name = "${local.name_prefix}-lambda-skill-executor-policy"
  role = aws_iam_role.lambda_skill_executor.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.skill_executor.arn}:*"
      },
      {
        Sid    = "SecretsManagerRead"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
        ]
        Resource = var.secrets_base_arn
      },
      {
        Sid    = "DynamoDBPendingRequests"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
        ]
        Resource = var.pending_requests_table_arn
      },
      {
        Sid    = "SQSSendResult"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
        ]
        # SQS queue is in sqs.tf within the same module
        Resource = aws_sqs_queue.skill_results.arn
      },
    ]
  })
}

# --- Lambda Function ---

# Placeholder: the actual deployment package is built by scripts/deploy.sh
# and uploaded via `aws lambda update-function-code`. The initial creation
# uses a dummy ZIP so Terraform can create the resource.

data "archive_file" "lambda_placeholder" {
  type        = "zip"
  output_path = "${path.module}/lambda_placeholder.zip"

  source {
    content  = "# Placeholder — real code deployed via scripts/deploy.sh"
    filename = "lambda_handler.py"
  }
}

resource "aws_lambda_function" "skill_executor" {
  function_name = "${local.name_prefix}-skill-executor"
  role          = aws_iam_role.lambda_skill_executor.arn
  handler       = "adapters.aws.lambda_handler.handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = var.lambda_memory_size

  filename         = data.archive_file.lambda_placeholder.output_path
  source_code_hash = data.archive_file.lambda_placeholder.output_base64sha256

  environment {
    variables = {
      T3NETS_PLATFORM        = "aws"
      T3NETS_STAGE           = var.environment
      AWS_REGION_NAME        = var.aws_region
      SECRETS_PREFIX         = var.secrets_prefix
      SQS_RESULTS_QUEUE_URL  = aws_sqs_queue.skill_results.id
      PENDING_REQUESTS_TABLE = var.pending_requests_table_name
    }
  }

  tags = { Name = "${local.name_prefix}-skill-executor" }

  depends_on = [aws_cloudwatch_log_group.skill_executor]
}
