output "cloudfront_domain" {
  description = "CloudFront distribution domain name (user-facing URL)"
  value       = aws_cloudfront_distribution.main.domain_name
}

output "cloudfront_hosted_zone_id" {
  description = "CloudFront hosted zone ID (used when creating Route 53 ALIAS records)"
  value       = aws_cloudfront_distribution.main.hosted_zone_id
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID (for cache invalidations)"
  value       = aws_cloudfront_distribution.main.id
}

output "s3_bucket_name" {
  description = "S3 bucket name for static HTML files"
  value       = aws_s3_bucket.static.id
}

output "s3_bucket_arn" {
  description = "S3 bucket ARN for IAM policies"
  value       = aws_s3_bucket.static.arn
}
