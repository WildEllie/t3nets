###############################################################################
# CDN — S3 + CloudFront
#
# Serves static HTML files from S3 via CloudFront edge locations.
# API calls (/api/*) and the Cognito callback (/callback) are routed
# through to API Gateway via a separate CloudFront origin.
#
# Architecture:
#   Browser → CloudFront
#               ├── /api/*      → API Gateway HTTP origin (no caching)
#               ├── /callback   → API Gateway HTTP origin (no caching)
#               └── /*          → S3 bucket (HTML files, short TTL)
###############################################################################

locals {
  name_prefix       = "${var.project}-${var.environment}"
  api_gateway_domain = trimsuffix(replace(var.api_gateway_url, "https://", ""), "/")
}

# --- S3 Bucket (private, static HTML) ---

resource "aws_s3_bucket" "static" {
  bucket = "${local.name_prefix}-static"

  tags = { Name = "${local.name_prefix}-static" }
}

resource "aws_s3_bucket_public_access_block" "static" {
  bucket = aws_s3_bucket.static.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "static" {
  bucket = aws_s3_bucket.static.id
  versioning_configuration {
    status = "Enabled"
  }
}

# --- CloudFront Origin Access Control (OAC) ---

resource "aws_cloudfront_origin_access_control" "static" {
  name                              = "${local.name_prefix}-s3-oac"
  description                       = "OAC for ${local.name_prefix} static S3 bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# --- Bucket Policy: allow CloudFront OAC only ---

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket_policy" "static" {
  bucket = aws_s3_bucket.static.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowCloudFrontOAC"
        Effect = "Allow"
        Principal = {
          Service = "cloudfront.amazonaws.com"
        }
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.static.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.main.arn
          }
        }
      }
    ]
  })

  # OAC bucket policy references the distribution ARN, so we must wait
  depends_on = [aws_cloudfront_distribution.main]
}

# --- CloudFront Cache Policies ---

# Short-TTL policy for S3 static files (5 min; invalidation handles immediate updates)
resource "aws_cloudfront_cache_policy" "static_short" {
  name        = "${local.name_prefix}-static-short"
  min_ttl     = 0
  default_ttl = 300
  max_ttl     = 3600

  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

# --- AWS Managed Policies for API Gateway origin ---
#
# CloudFront restricts Authorization in custom cache/origin-request policies:
#   - Custom origin request policies: cannot whitelist Authorization by name
#   - Custom cache policies with max_ttl=0: cannot specify header behavior
#
# Solution: use managed policies that handle these cases correctly.
#   CachingDisabled (4135ea2d-...): TTL=0, forwards all query strings/cookies
#   AllViewerExceptHostHeader (b689b0a8-...): forwards all viewer headers
#     (including Authorization) except Host, which must stay as the origin domain

locals {
  # AWS managed CloudFront policy IDs (stable, do not change)
  managed_caching_disabled            = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
  managed_all_viewer_except_host      = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
}

# --- CloudFront Function: rewrite extensionless paths to .html ---
#
# S3 stores files as chat.html, settings.html, etc.
# Browsers request /chat, /settings — this function appends .html so S3 finds them.

resource "aws_cloudfront_function" "rewrite_html" {
  name    = "${local.name_prefix}-rewrite-html"
  runtime = "cloudfront-js-2.0"
  publish = true

  code = <<-EOF
    async function handler(event) {
      var uri = event.request.uri;
      // If the path has no extension (no dot after the last slash), append .html
      var lastSegment = uri.split('/').pop();
      if (lastSegment && !lastSegment.includes('.')) {
        event.request.uri = uri + '.html';
      } else if (uri.endsWith('/')) {
        event.request.uri = uri + 'index.html';
      }
      return event.request;
    }
  EOF
}

# --- CloudFront Distribution ---

resource "aws_cloudfront_distribution" "main" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "${local.name_prefix} — static HTML + API proxy"

  # Origin 1: S3 (default — static HTML files)
  origin {
    domain_name              = aws_s3_bucket.static.bucket_regional_domain_name
    origin_id                = "s3-static"
    origin_access_control_id = aws_cloudfront_origin_access_control.static.id
  }

  # Origin 2: API Gateway (dynamic API calls)
  origin {
    domain_name = local.api_gateway_domain
    origin_id   = "api-gateway"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  # Default cache behavior → S3 (HTML files)
  default_cache_behavior {
    target_origin_id       = "s3-static"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = aws_cloudfront_cache_policy.static_short.id
    compress               = true

    # Rewrite /chat → /chat.html before forwarding to S3
    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.rewrite_html.arn
    }
  }

  # /api/* → API Gateway (no caching, forward all viewer headers + query strings)
  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = "api-gateway"
    viewer_protocol_policy   = "https-only"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = local.managed_caching_disabled
    origin_request_policy_id = local.managed_all_viewer_except_host
    compress                 = false
  }

  # /callback → API Gateway (Cognito OAuth redirect — no caching)
  ordered_cache_behavior {
    path_pattern             = "/callback"
    target_origin_id         = "api-gateway"
    viewer_protocol_policy   = "https-only"
    allowed_methods          = ["GET", "HEAD", "OPTIONS"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = local.managed_caching_disabled
    origin_request_policy_id = local.managed_all_viewer_except_host
    compress                 = false
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = { Name = "${local.name_prefix}-cdn" }
}
