###############################################################################
# ECR — Container registry for router image
###############################################################################

variable "project" { type = string }
variable "environment" { type = string }

locals {
  name_prefix = "${var.project}-${var.environment}"
}

resource "aws_ecr_repository" "router" {
  name                 = "${local.name_prefix}-router"
  image_tag_mutability = "MUTABLE"
  force_delete         = true  # Allow deletion even with images (dev convenience)

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = "${local.name_prefix}-router" }
}

# Keep only last 10 images to save storage costs
resource "aws_ecr_lifecycle_policy" "router" {
  repository = aws_ecr_repository.router.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 10 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

output "repository_url" {
  value = aws_ecr_repository.router.repository_url
}

output "repository_arn" {
  value = aws_ecr_repository.router.arn
}
