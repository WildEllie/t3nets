###############################################################################
# DNS — Route 53 hosted zone + ACM certificate for the custom domain
#
# Provisions:
#   - Route 53 hosted zone (optional; skipped when manage_zone = false)
#   - ACM certificate in us-east-1 (required for CloudFront) with DNS validation
#   - Validation CNAMEs in Route 53 when manage_zone = true
#
# The dashboard A-alias record that points <subdomain>.<root_domain> at
# CloudFront lives in the root main.tf, since it needs outputs from both
# this module and the cdn module.
###############################################################################

terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      configuration_aliases = [aws.cloudfront]
    }
  }
}

variable "root_domain" {
  description = "Root domain (e.g. t3nets.dev)"
  type        = string
}

variable "dashboard_subdomain" {
  description = "Subdomain for the dashboard (e.g. www)"
  type        = string
  default     = "www"
}

variable "manage_zone" {
  description = "If true, create the Route 53 hosted zone. If false, DNS is managed externally."
  type        = bool
  default     = true
}

locals {
  dashboard_fqdn = "${var.dashboard_subdomain}.${var.root_domain}"
}

# --- Hosted zone (optional) ---

resource "aws_route53_zone" "main" {
  count = var.manage_zone ? 1 : 0
  name  = var.root_domain
}

# --- ACM certificate (must be in us-east-1 for CloudFront) ---

resource "aws_acm_certificate" "main" {
  provider                  = aws.cloudfront
  domain_name               = local.dashboard_fqdn
  subject_alternative_names = [var.root_domain]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# --- DNS validation records (only when we own the zone) ---

resource "aws_route53_record" "cert_validation" {
  for_each = var.manage_zone ? {
    for dvo in aws_acm_certificate.main.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  } : {}

  zone_id = aws_route53_zone.main[0].zone_id
  name    = each.value.name
  type    = each.value.type
  records = [each.value.record]
  ttl     = 60

  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "main" {
  provider                = aws.cloudfront
  certificate_arn         = aws_acm_certificate.main.arn
  validation_record_fqdns = var.manage_zone ? [for r in aws_route53_record.cert_validation : r.fqdn] : null

  timeouts {
    create = "30m"
  }
}

# --- Outputs ---

output "dashboard_fqdn" {
  description = "Full DNS name of the dashboard (e.g. www.t3nets.dev)"
  value       = local.dashboard_fqdn
}

output "apex_fqdn" {
  description = "Apex / root domain (e.g. t3nets.dev)"
  value       = var.root_domain
}

output "certificate_arn" {
  description = "ARN of the validated ACM certificate (us-east-1, for CloudFront)"
  value       = aws_acm_certificate_validation.main.certificate_arn
}

output "zone_id" {
  description = "Route 53 zone ID (empty when manage_zone = false)"
  value       = var.manage_zone ? aws_route53_zone.main[0].zone_id : ""
}

output "zone_nameservers" {
  description = "Name servers to set at the registrar on first-time delegation (empty when manage_zone = false)"
  value       = var.manage_zone ? aws_route53_zone.main[0].name_servers : []
}

output "cert_validation_records" {
  description = "CNAMEs to add at the external DNS provider for ACM validation (populated only when manage_zone = false)"
  value = [
    for dvo in aws_acm_certificate.main.domain_validation_options : {
      name  = dvo.resource_record_name
      type  = dvo.resource_record_type
      value = dvo.resource_record_value
    }
  ]
}
