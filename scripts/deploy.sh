#!/bin/bash
###############################################################################
# T3nets Deploy Script
#
# Builds the router container, pushes to ECR, updates ECS service.
# When USE_ASYNC_SKILLS=true, also deploys per-skill Lambda functions
# for domain skills (sprint_status, release_notes, etc.).
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
echo "║  Tag: $TAG                         ║"
echo "║  Env: $ENVIRONMENT                        ║"
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

# --- Deploy per-skill Lambdas (if async skills enabled) ---
USE_ASYNC="${USE_ASYNC_SKILLS:-false}"
if [ "$USE_ASYNC" = "true" ]; then
    echo "═══════════════════════════════════════"
    echo "  Deploying domain skill Lambdas"
    echo "═══════════════════════════════════════"
    echo ""

    # Read shared infrastructure from Terraform outputs
    TF_DIR="infra/aws"
    LAMBDA_ROLE_ARN=$(cd "${TF_DIR}" && terraform output -raw lambda_role_arn)
    EB_BUS_NAME=$(cd "${TF_DIR}" && terraform output -raw eventbridge_bus_name)
    EB_BUS_ARN=$(cd "${TF_DIR}" && terraform output -raw eventbridge_bus_arn)
    EB_DLQ_ARN=$(cd "${TF_DIR}" && terraform output -raw eventbridge_dlq_arn)
    SQS_QUEUE_URL=$(cd "${TF_DIR}" && terraform output -raw sqs_results_queue_url)
    SECRETS_PREFIX=$(cd "${TF_DIR}" && terraform output -raw secrets_prefix)
    PENDING_TABLE=$(cd "${TF_DIR}" && terraform output -raw pending_requests_table_name)

    echo "  Role:    ${LAMBDA_ROLE_ARN}"
    echo "  Bus:     ${EB_BUS_NAME}"
    echo "  Queue:   ${SQS_QUEUE_URL}"
    echo ""

    # Domain skills to deploy (ping is managed by Terraform).
    # Each entry is a skill name; we auto-locate it under agent/skills/
    # (legacy layout) or agent/practices/*/skills/ (practice-owned skills).
    DOMAIN_SKILLS=("sprint_status" "release_notes")

    for SKILL_NAME in "${DOMAIN_SKILLS[@]}"; do
        # Find the skill: first under agent/skills/, then under any practice
        SKILL_DIR=""
        PRACTICE_DIR=""
        if [ -f "agent/skills/${SKILL_NAME}/skill.yaml" ]; then
            SKILL_DIR="agent/skills/${SKILL_NAME}"
        else
            for p in agent/practices/*/skills/"${SKILL_NAME}"/skill.yaml; do
                [ -f "$p" ] || continue
                SKILL_DIR="$(dirname "$p")"
                PRACTICE_DIR="$(dirname "$(dirname "$SKILL_DIR")")"
                break
            done
        fi

        if [ -z "${SKILL_DIR}" ]; then
            echo "  ⚠ Skipping ${SKILL_NAME} — skill.yaml not found"
            continue
        fi

        FUNC_NAME="${NAME_PREFIX}-skill-${SKILL_NAME}"
        LOG_GROUP="/aws/lambda/${FUNC_NAME}"
        RULE_NAME="${NAME_PREFIX}-skill-invoke-${SKILL_NAME}"

        if [ -n "${PRACTICE_DIR}" ]; then
            echo "→ Deploying skill: ${SKILL_NAME} (practice: $(basename "${PRACTICE_DIR}"))"
        else
            echo "→ Deploying skill: ${SKILL_NAME}"
        fi

        # --- Step 1: Package Lambda ZIP ---
        LAMBDA_DIR=$(mktemp -d)

        # Copy AWS adapter files (lambda_handler pulls in these imports)
        mkdir -p "${LAMBDA_DIR}/adapters/aws"
        touch "${LAMBDA_DIR}/adapters/__init__.py"
        touch "${LAMBDA_DIR}/adapters/aws/__init__.py"
        cp adapters/aws/lambda_handler.py "${LAMBDA_DIR}/adapters/aws/"
        cp adapters/aws/pending_requests.py "${LAMBDA_DIR}/adapters/aws/"
        cp adapters/aws/secrets_manager.py "${LAMBDA_DIR}/adapters/aws/"
        cp adapters/aws/s3_blob_store.py "${LAMBDA_DIR}/adapters/aws/"
        cp adapters/aws/dynamodb_tenant_store.py "${LAMBDA_DIR}/adapters/aws/"

        # Copy agent framework
        mkdir -p "${LAMBDA_DIR}/agent"
        touch "${LAMBDA_DIR}/agent/__init__.py"
        cp -r agent/interfaces "${LAMBDA_DIR}/agent/"
        touch "${LAMBDA_DIR}/agent/interfaces/__init__.py"
        cp -r agent/models "${LAMBDA_DIR}/agent/"
        touch "${LAMBDA_DIR}/agent/models/__init__.py"

        # Skill registry is always needed; include ping as a filler so the
        # built-in skills dir is non-empty on the practice-skill path.
        mkdir -p "${LAMBDA_DIR}/agent/skills"
        touch "${LAMBDA_DIR}/agent/skills/__init__.py"
        cp agent/skills/registry.py "${LAMBDA_DIR}/agent/skills/"

        if [ -n "${PRACTICE_DIR}" ]; then
            # Practice skill: copy the practice registry + the full
            # practice directory. register_skills() will pick up the
            # target skill via worker_path.
            mkdir -p "${LAMBDA_DIR}/agent/practices"
            touch "${LAMBDA_DIR}/agent/practices/__init__.py"
            cp agent/practices/registry.py "${LAMBDA_DIR}/agent/practices/"
            cp -r "${PRACTICE_DIR}" "${LAMBDA_DIR}/agent/practices/"
        else
            # Legacy layout: copy just this skill under agent/skills/
            mkdir -p "${LAMBDA_DIR}/agent/skills/${SKILL_NAME}"
            cp -r "${SKILL_DIR}/" "${LAMBDA_DIR}/agent/skills/${SKILL_NAME}/"
        fi

        # Install deps with manylinux wheels so pydantic_core etc. match
        # the Lambda runtime (boto3 is already in the runtime). Two-phase
        # install: native wheels first, then pure-Python sdk with --no-deps.
        pip3 install \
            --platform manylinux2014_x86_64 \
            --python-version 3.12 \
            --only-binary=:all: \
            --implementation cp \
            pyyaml pydantic \
            -t "${LAMBDA_DIR}" \
            --upgrade \
            --quiet
        pip3 install ./sdk -t "${LAMBDA_DIR}" --no-deps --upgrade --quiet

        # Create ZIP
        LAMBDA_ZIP="/tmp/${FUNC_NAME}.zip"
        (cd "${LAMBDA_DIR}" && zip -r "${LAMBDA_ZIP}" . -x '*.pyc' '__pycache__/*' > /dev/null)
        echo "  Package: $(du -h "${LAMBDA_ZIP}" | cut -f1)"

        # --- Step 2: Create or update CloudWatch log group ---
        aws logs create-log-group \
            --log-group-name "${LOG_GROUP}" \
            --region "${REGION}" 2>/dev/null || true
        aws logs put-retention-policy \
            --log-group-name "${LOG_GROUP}" \
            --retention-in-days 14 \
            --region "${REGION}" 2>/dev/null || true

        # --- Step 3: Create or update Lambda function ---
        if aws lambda get-function --function-name "${FUNC_NAME}" --region "${REGION}" --no-cli-pager > /dev/null 2>&1; then
            # Update existing function
            aws lambda update-function-code \
                --function-name "${FUNC_NAME}" \
                --zip-file "fileb://${LAMBDA_ZIP}" \
                --region "${REGION}" \
                --no-cli-pager > /dev/null

            aws lambda wait function-updated \
                --function-name "${FUNC_NAME}" \
                --region "${REGION}"

            echo "  Lambda: updated"
        else
            # Create new function
            aws lambda create-function \
                --function-name "${FUNC_NAME}" \
                --role "${LAMBDA_ROLE_ARN}" \
                --handler "adapters.aws.lambda_handler.handler" \
                --runtime "python3.12" \
                --timeout 30 \
                --memory-size 512 \
                --zip-file "fileb://${LAMBDA_ZIP}" \
                --environment "Variables={T3NETS_PLATFORM=aws,T3NETS_STAGE=${ENVIRONMENT},AWS_REGION_NAME=${REGION},SECRETS_PREFIX=${SECRETS_PREFIX},SQS_RESULTS_QUEUE_URL=${SQS_QUEUE_URL},PENDING_REQUESTS_TABLE=${PENDING_TABLE}}" \
                --region "${REGION}" \
                --no-cli-pager > /dev/null

            aws lambda wait function-active-v2 \
                --function-name "${FUNC_NAME}" \
                --region "${REGION}"

            echo "  Lambda: created"
        fi

        # Get the Lambda ARN
        LAMBDA_ARN=$(aws lambda get-function \
            --function-name "${FUNC_NAME}" \
            --region "${REGION}" \
            --query 'Configuration.FunctionArn' \
            --output text)

        # --- Step 4: Create or update EventBridge rule ---
        aws events put-rule \
            --name "${RULE_NAME}" \
            --event-bus-name "${EB_BUS_NAME}" \
            --event-pattern "{\"source\":[\"agent.router\"],\"detail-type\":[\"skill.invoke\"],\"detail\":{\"skill_name\":[\"${SKILL_NAME}\"]}}" \
            --description "Route ${SKILL_NAME} skill invocations to Lambda" \
            --region "${REGION}" \
            --no-cli-pager > /dev/null

        # Set the target with retry policy and DLQ
        aws events put-targets \
            --rule "${RULE_NAME}" \
            --event-bus-name "${EB_BUS_NAME}" \
            --targets "[{\"Id\":\"skill-${SKILL_NAME}\",\"Arn\":\"${LAMBDA_ARN}\",\"RetryPolicy\":{\"MaximumRetryAttempts\":2,\"MaximumEventAgeInSeconds\":300},\"DeadLetterConfig\":{\"Arn\":\"${EB_DLQ_ARN}\"}}]" \
            --region "${REGION}" \
            --no-cli-pager > /dev/null

        echo "  EventBridge rule: ${RULE_NAME}"

        # --- Step 5: Add Lambda permission for EventBridge ---
        RULE_ARN="arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${EB_BUS_NAME}/${RULE_NAME}"
        aws lambda add-permission \
            --function-name "${FUNC_NAME}" \
            --statement-id "AllowEventBridgeInvoke" \
            --action "lambda:InvokeFunction" \
            --principal "events.amazonaws.com" \
            --source-arn "${RULE_ARN}" \
            --region "${REGION}" \
            --no-cli-pager > /dev/null 2>&1 || true  # Ignore if already exists

        # Cleanup temp files
        rm -rf "${LAMBDA_DIR}" "${LAMBDA_ZIP}"
        echo "  ✅ ${SKILL_NAME} deployed"
        echo ""
    done
else
    echo "→ Skipping Lambda deploy (USE_ASYNC_SKILLS=${USE_ASYNC})"
    echo ""
fi

# --- Pull Ollama model in sidecar (if enabled) ---
USE_OLLAMA_FLAG="${USE_OLLAMA:-false}"
OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.2:3b}"
if [ "$USE_OLLAMA_FLAG" = "true" ]; then
    echo "═══════════════════════════════════════"
    echo "  Ollama sidecar enabled"
    echo "  Model: ${OLLAMA_MODEL}"
    echo "═══════════════════════════════════════"
    echo "→ Ollama sidecar will pull '${OLLAMA_MODEL}' automatically on container start."
    echo "  Model download happens inside the task — first cold start may take several minutes."
    echo "  Monitor progress: aws logs tail /ecs/${NAME_PREFIX}-ollama --follow --region ${REGION}"
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
TF_DIR="infra/aws"
API_ENDPOINT=$(cd "${TF_DIR}" && terraform output -raw api_endpoint 2>/dev/null || echo "unknown")
echo "API: ${API_ENDPOINT}"
echo ""

# --- Upload HTML to S3 + invalidate CloudFront ---
S3_BUCKET=$(cd "${TF_DIR}" && terraform output -raw s3_bucket_name 2>/dev/null || echo "")
CF_DISTRIBUTION_ID=$(cd "${TF_DIR}" && terraform output -raw cloudfront_distribution_id 2>/dev/null || echo "")

if [ -n "${S3_BUCKET}" ]; then
    echo "→ Uploading static assets to S3 (${S3_BUCKET})..."
    aws s3 sync adapters/local/ "s3://${S3_BUCKET}/" \
        --exclude "*" \
        --include "*.html" \
        --include "*.png" \
        --include "*.css" \
        --include "*.js" \
        --delete \
        --region "${REGION}"
    echo ""

    # Upload built-in practice pages to s3://{bucket}/p/{name}/{file}.
    # Pages are declared in each practice.yaml's `pages:` list. Uploaded
    # practices are published separately by the AWS post-install hook.
    echo "→ Uploading built-in practice pages to S3..."
    PRACTICE_COUNT=0
    for pdir in agent/practices/*/; do
        [ -d "$pdir" ] || continue
        [ -f "${pdir}practice.yaml" ] || continue
        practice_name=$(basename "$pdir")
        # Extract `file:` paths from the manifest's pages list. PyYAML is
        # already a dev dependency so this is safe in the deploy environment.
        page_files=$(python3 -c "
import sys, yaml
with open('${pdir}practice.yaml') as f:
    d = yaml.safe_load(f) or {}
for p in d.get('pages', []):
    print(p['file'])
" 2>/dev/null || true)
        while IFS= read -r page_file; do
            [ -z "$page_file" ] && continue
            src="${pdir}${page_file}"
            if [ -f "$src" ]; then
                aws s3 cp "$src" "s3://${S3_BUCKET}/p/${practice_name}/${page_file}" \
                    --region "${REGION}" --no-cli-pager > /dev/null
                PRACTICE_COUNT=$((PRACTICE_COUNT + 1))
            fi
        done <<<"$page_files"
    done
    echo "   Uploaded ${PRACTICE_COUNT} practice page file(s)."
    echo ""
else
    echo "→ Skipping S3 upload (s3_bucket_name output not found)"
    echo ""
fi

if [ -n "${CF_DISTRIBUTION_ID}" ]; then
    echo "→ Invalidating CloudFront cache (${CF_DISTRIBUTION_ID})..."
    aws cloudfront create-invalidation \
        --distribution-id "${CF_DISTRIBUTION_ID}" \
        --paths "/*" \
        --region "${REGION}" \
        --no-cli-pager > /dev/null
    echo "   Cache invalidation submitted."
    echo ""
else
    echo "→ Skipping CloudFront invalidation (cloudfront_distribution_id output not found)"
    echo ""
fi

CF_DOMAIN=$(cd "${TF_DIR}" && terraform output -raw cloudfront_domain 2>/dev/null || echo "")
if [ -n "${CF_DOMAIN}" ]; then
    echo "Dashboard: https://${CF_DOMAIN}/chat"
    echo ""
fi
