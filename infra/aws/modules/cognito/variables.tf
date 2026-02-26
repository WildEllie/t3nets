variable "project" { type = string }
variable "environment" { type = string }

variable "callback_urls" {
  description = "Allowed OAuth callback URLs"
  type        = list(string)
  default     = ["http://localhost:8080/callback"]
}

variable "logout_urls" {
  description = "Allowed logout redirect URLs"
  type        = list(string)
  default     = ["http://localhost:8080/login"]
}

variable "password_minimum_length" {
  description = "Minimum password length"
  type        = number
  default     = 8
}
