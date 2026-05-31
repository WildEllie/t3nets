# GitHub Actions OIDC — deploy role for the sherpii branch.
#
# The OIDC provider (token.actions.githubusercontent.com) already exists in
# this account. This file adds the IAM role that the deploy workflow assumes.
# Scoped to pushes on the sherpii branch only.

data "aws_caller_identity" "oidc" {}

resource "aws_iam_role" "github_deploy_sherpii" {
  name = "github-deploy-sherpii"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = "arn:aws:iam::${data.aws_caller_identity.oidc.account_id}:oidc-provider/token.actions.githubusercontent.com"
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          # Scoped to pushes on the sherpii branch of this repo
          "token.actions.githubusercontent.com:sub" = "repo:WildEllie/t3nets:ref:refs/heads/sherpii"
        }
      }
    }]
  })

  tags = { Name = "github-deploy-sherpii" }
}

resource "aws_iam_role_policy" "github_deploy_sherpii" {
  name = "github-deploy-sherpii-policy"
  role = aws_iam_role.github_deploy_sherpii.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # ECR — build and push container images
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:DescribeRepositories",
          "ecr:CreateRepository",
        ]
        Resource = "arn:aws:ecr:us-east-1:${data.aws_caller_identity.oidc.account_id}:repository/t3nets-router"
      },
      # ECS — update service with new task definition
      {
        Effect = "Allow"
        Action = [
          "ecs:DescribeServices",
          "ecs:UpdateService",
          "ecs:RegisterTaskDefinition",
          "ecs:DescribeTaskDefinition",
          "ecs:DescribeTasks",
          "ecs:ListTasks",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:RequestedRegion" = "us-east-1"
          }
        }
      },
      # IAM PassRole — needed to register ECS task definitions
      {
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = "arn:aws:iam::${data.aws_caller_identity.oidc.account_id}:role/t3nets-sherpii-*"
      },
      # S3 — upload static HTML + practice pages
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:ListBucket",
        ]
        Resource = [
          "arn:aws:s3:::t3nets-sherpii-static",
          "arn:aws:s3:::t3nets-sherpii-static/*",
        ]
      },
      # CloudFront — invalidate cache after S3 upload
      {
        Effect   = "Allow"
        Action   = "cloudfront:CreateInvalidation"
        Resource = "*"
      },
      # CloudWatch Logs — ECS deployment stability check reads log groups
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:PutRetentionPolicy",
          "logs:DescribeLogGroups",
        ]
        Resource = "arn:aws:logs:us-east-1:${data.aws_caller_identity.oidc.account_id}:log-group:/ecs/t3nets-sherpii-*"
      },
    ]
  })
}

output "github_deploy_sherpii_role_arn" {
  description = "ARN of the GitHub Actions OIDC role — add as DEPLOY_ROLE_ARN secret in GitHub"
  value       = aws_iam_role.github_deploy_sherpii.arn
}
