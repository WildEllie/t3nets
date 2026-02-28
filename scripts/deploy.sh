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

# --- Package & deploy Lambda (if async skills enabled) ---
USE_ASYNC="${USE_ASYNC_SKILLS:-false}"
if [ "$USE_ASYNC" = "true" ]; then
    echo "→ Packaging Lambda skill executor..."
    LAMBDA_DIR=$(mktemp -d)
    LAMBDA_FUNC="${NAME_PREFIX}-skill-executor"

    # Copy agent code (skills, interfaces, models) + AWS adapters needed by Lambda
    mkdir -p "${LAMBDA_DIR}/agent" "${LAMBDA_DIR}/adapters/aws"
    cp -r agent/skills agent/interfaces agent/models "${LAMBDA_DIR}/agent/"
    touch "${LAMBDA_DIR}/agent/__init__.py"
    touch "${LAMBDA_DIR}/adapters/__init__.py"
    touch "${LAMBDA_DIR}/adapters/aws/__init__.py"
    cp adapters/aws/lambda_handler.py "${LAMBDA_DIR}/adapters/aws/"
    cp adapters/aws/pending_requests.py "${LAMBDA_DIR}/adapters/aws/"
    cp adapters/aws/secrets_manager.py "${LAMBDA_DIR}/adapters/aws/"

    # Install dependencies into package (only PyYAML — boto3 is in Lambda runtime)
    pip install pyyaml -t "${LAMBDA_DIR}" --quiet

    # Create ZIP
    LAMBDA_ZIP="/tmp/${LAMBDA_FUNC}.zip"
    (cd "${LAMBDA_DIR}" && zip -r "${LAMBDA_ZIP}" . -x '*.pyc' '__pycache__/*' > /dev/null)
    echo "  Lambda package: $(du -h "${LAMBDA_ZIP}" | cut -f1)"

    # Deploy Lambda
    echo "→ Updating Lambda function code..."
    aws lambda update-function-code \
        --function-name "${LAMBDA_FUNC}" \
        --zip-file "fileb://${LAMBDA_ZIP}" \
        --region "${REGION}" \
        --no-cli-pager > /dev/null

    # Wait for update to complete
    aws lambda wait function-updated \
        --function-name "${LAMBDA_FUNC}" \
        --region "${REGION}"

    # Cleanup
    rm -rf "${LAMBDA_DIR}" "${LAMBDA_ZIP}"
    echo "  ✅ Lambda updated"
    echo ""
else
    echo "→ Skipping Lambda deploy (USE_ASYNC_SKILLS=${USE_ASYNC})"
    echo ""
fi

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
start=$(date +%s)
echo "→ Waiting for deployment to stabilize... $(date +%H:%M:%S)"
aws ecs wait services-stable \
    --cluster "${NAME_PREFIX}-cluster" \
    --services "${NAME_PREFIX}-router" \
    --region "${REGION}"

echo ""
echo "✅ Deployed ${ECR_REPO}:${TAG} to ${ENVIRONMENT}"
echo ""

end=$(date +%s)
echo "Execution time: $((end - start)) seconds"

# --- Show the API endpoint ---
API_ENDPOINT=$(cd infra/aws && terraform output -raw api_endpoint 2>/dev/null || echo "unknown")
echo "API: ${API_ENDPOINT}"
echo ""