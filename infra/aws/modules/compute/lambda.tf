###############################################################################
# Lambda — Ping Skill
#
# Terraform-managed Lambda for the ping skill. Built from real source code
# via build_lambda_base.sh (not a placeholder). Domain-specific skills
# (sprint_status, release_notes) are deployed by scripts/deploy.sh.
#
# The shared IAM role is reused by deploy.sh for domain skill Lambdas.
###############################################################################

# --- CloudWatch Log Group ---

resource "aws_cloudwatch_log_group" "skill_ping" {
  name              = "/aws/lambda/${local.name_prefix}-skill-ping"
  retention_in_days = 14

  tags = { Name = "${local.name_prefix}-skill-ping-logs" }
}

# --- Shared IAM Role for all skill Lambdas ---

resource "aws_iam_role" "lambda_skill_executor" {
  name = "${local.name_prefix}-lambda-skill-executor"

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
        # Allow logging from any skill Lambda (deploy.sh creates log groups
        # named /aws/lambda/${name_prefix}-skill-*)
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.name_prefix}-skill-*:*"
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
        Resource = aws_sqs_queue.skill_results.arn
      },
    ]
  })
}

# --- Build the ping Lambda ZIP from source ---

locals {
  project_root = abspath("${path.module}/../../../..")

  # Source files that trigger a rebuild when changed
  lambda_source_files = [
    "${local.project_root}/adapters/aws/lambda_handler.py",
    "${local.project_root}/adapters/aws/pending_requests.py",
    "${local.project_root}/adapters/aws/secrets_manager.py",
    "${local.project_root}/agent/skills/registry.py",
    "${local.project_root}/agent/skills/ping/worker.py",
    "${local.project_root}/agent/skills/ping/skill.yaml",
  ]

  # Hash of all source files — triggers rebuild when any change
  lambda_source_hash = sha256(join(",", [
    for f in local.lambda_source_files : filesha256(f)
  ]))
}

resource "terraform_data" "build_lambda_base" {
  triggers_replace = [local.lambda_source_hash]

  provisioner "local-exec" {
    command     = "bash ${local.project_root}/scripts/build_lambda_base.sh ${abspath(path.module)}/lambda_base.zip"
    working_dir = local.project_root
  }
}

# --- Ping Lambda Function ---

resource "aws_lambda_function" "skill_ping" {
  function_name = "${local.name_prefix}-skill-ping"
  role          = aws_iam_role.lambda_skill_executor.arn
  handler       = "adapters.aws.lambda_handler.handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = var.lambda_memory_size

  filename         = "${path.module}/lambda_base.zip"
  source_code_hash = local.lambda_source_hash

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

  tags = { Name = "${local.name_prefix}-skill-ping" }

  depends_on = [
    aws_cloudwatch_log_group.skill_ping,
    terraform_data.build_lambda_base,
  ]
}
