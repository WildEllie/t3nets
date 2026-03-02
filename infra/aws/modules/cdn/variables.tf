variable "project" { type = string }
variable "environment" { type = string }

variable "api_gateway_url" {
  description = "HTTPS invoke URL of the API Gateway (without trailing slash)"
  type        = string
}
