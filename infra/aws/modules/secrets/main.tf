###############################################################################
# Secrets — AWS Secrets Manager
#
# Per-tenant secrets stored at:
#   /t3nets/{environment}/tenants/{tenant_id}/{integration}
###############################################################################

variable "project" { type = string }
variable "environment" { type = string }

locals {
  secrets_prefix = "/${var.project}/${var.environment}/tenants"
}

# We don't create individual secrets here — they're created
# dynamically by the admin API when tenants connect integrations.
# This module just defines the base path and IAM policy.

output "base_arn" {
  description = "Base ARN for Secrets Manager path-based IAM policies"
  value       = "arn:aws:secretsmanager:*:*:secret:${local.secrets_prefix}/*"
}

output "secrets_prefix" {
  value = local.secrets_prefix
}
