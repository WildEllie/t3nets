#!/bin/bash
###############################################################################
# T3nets Deploy Script
#
# Builds the router container, pushes to ECR, updates ECS service.
#
# Usage:
#   ./scripts/deploy.sh              # Deploy with tag "latest"
#   ./scripts/deploy.sh v0.2.0       # Deploy with specific tag
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - Docker running
#   - Terraform already applied (ECR repo, ECS cluster exist)
###############################################################################

set -euo pipefail

TAG="${1:-latest}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
PROJECT="t3nets"
REGION="${AWS_REGION:-us-east-1}"

NAME_PREFIX="${PROJECT}-${ENVIRONMENT}"
ECR_REPO="${NAME_PREFIX}-router"

echo "╔══════════════════════════════════════╗"
echo "║  T3nets Deploy                       ║"
echo "║  Tag: $TAG"
echo "║  Env: $ENVIRONMENT"
echo "╚══════════════════════════════════════╝"
echo ""

# --- Get AWS account ID ---
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}"

echo "→ ECR: ${ECR_URI}"
echo ""

# --- Login to ECR ---
echo "→ Logging in to ECR..."
aws ecr get-login-password --region "${REGION}" | \
    docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
echo ""

# --- Build (always target linux/amd64 for Fargate) ---
echo "→ Building container (linux/amd64)..."
docker build --platform linux/amd64 -t "${ECR_REPO}:${TAG}" -f Dockerfile .
echo ""

# --- Tag & Push ---
echo "→ Pushing to ECR..."
docker tag "${ECR_REPO}:${TAG}" "${ECR_URI}:${TAG}"
docker push "${ECR_URI}:${TAG}"
echo ""

# --- Update ECS service (force new deployment) ---
echo "→ Updating ECS service..."
aws ecs update-service \
    --cluster "${NAME_PREFIX}-cluster" \
    --service "${NAME_PREFIX}-router" \
    --force-new-deployment \
    --region "${REGION}" \
    --no-cli-pager > /dev/null
echo ""

# --- Wait for deployment ---
echo "→ Waiting for deployment to stabilize..."
aws ecs wait services-stable \
    --cluster "${NAME_PREFIX}-cluster" \
    --services "${NAME_PREFIX}-router" \
    --region "${REGION}"

echo ""
echo "✅ Deployed ${ECR_REPO}:${TAG} to ${ENVIRONMENT}"
echo ""

# --- Show the API endpoint ---
API_ENDPOINT=$(cd infra/aws && terraform output -raw api_endpoint 2>/dev/null || echo "unknown")
echo "API: ${API_ENDPOINT}"
echo ""
