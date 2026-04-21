variable "project" { type = string }
variable "environment" { type = string }

variable "api_gateway_url" {
  description = "HTTPS invoke URL of the API Gateway (without trailing slash)"
  type        = string
}

variable "aliases" {
  description = "Alternate domain names (CNAMEs) to attach to the CloudFront distribution. Empty = default CloudFront domain only."
  type        = list(string)
  default     = []
}

variable "acm_certificate_arn" {
  description = "ACM certificate ARN (must be in us-east-1) when serving a custom domain. Empty = use CloudFront default certificate."
  type        = string
  default     = ""
}
