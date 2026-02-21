# Uncomment to use S3 remote state (recommended for team use).
# First create the bucket manually:
#   aws s3 mb s3://t3nets-terraform-state
#   aws s3api put-bucket-versioning --bucket t3nets-terraform-state --versioning-configuration Status=Enabled

# terraform {
#   backend "s3" {
#     bucket = "t3nets-terraform-state"
#     key    = "dev/terraform.tfstate"
#     region = "us-east-1"
#   }
# }
