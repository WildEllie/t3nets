###############################################################################
# Cognito — User Pool & App Client
#
# Provides authentication for the T3nets dashboard. Each user has a
# custom:tenant_id attribute that links them to a tenant in DynamoDB.
###############################################################################

locals {
  name_prefix = "${var.project}-${var.environment}"
}

# --- User Pool ---

resource "aws_cognito_user_pool" "main" {
  name = "${local.name_prefix}-users"

  # Sign-in: email only (no username)
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = var.password_minimum_length
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = false
  }

  # Custom attribute: tenant_id (links user to DynamoDB tenant)
  schema {
    name                = "tenant_id"
    attribute_data_type = "String"
    mutable             = true
    required            = false

    string_attribute_constraints {
      min_length = 1
      max_length = 64
    }
  }

  # Email verification
  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
    email_subject        = "T3nets — Verify your email"
    email_message        = "Your verification code is {####}"
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Admin can create users (for tenant onboarding)
  admin_create_user_config {
    allow_admin_create_user_only = false
  }

  tags = { Name = "${local.name_prefix}-user-pool" }
}

# --- User Pool Domain (for hosted signup page) ---

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${local.name_prefix}-auth"
  user_pool_id = aws_cognito_user_pool.main.id
}

# --- App Client (PKCE flow for SPA) ---

resource "aws_cognito_user_pool_client" "dashboard" {
  name         = "${local.name_prefix}-dashboard"
  user_pool_id = aws_cognito_user_pool.main.id

  # PKCE flow — no client secret needed (SPA-safe)
  generate_secret = false

  # OAuth 2.0 config
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]

  callback_urls = var.callback_urls
  logout_urls   = var.logout_urls

  # Token validity
  access_token_validity  = 1   # hours
  id_token_validity      = 1   # hours
  refresh_token_validity = 30  # days

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  # Read custom attributes in tokens
  read_attributes  = ["email", "custom:tenant_id"]
  write_attributes = ["email", "custom:tenant_id"]

  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]
}
