#!/bin/bash
###############################################################################
# T3nets Seed Script
#
# Seeds the default tenant into DynamoDB and pushes Jira credentials
# into Secrets Manager. Run once after terraform apply.
#
# Usage:
#   ./scripts/seed.sh
#
# Reads Jira credentials from your local .env file.
###############################################################################

set -euo pipefail

ENVIRONMENT="${ENVIRONMENT:-dev}"
PROJECT="t3nets"

# --- Load .env ---
if [ -f .env ]; then
    set -a
    source .env
    set +a
else
    echo "ERROR: .env file not found. Copy from .env.example and fill in values."
    exit 1
fi

REGION="${AWS_REGION:-us-east-1}"
MODEL_ID="${BEDROCK_MODEL_ID:-anthropic.claude-sonnet-4-5-20250929-v1:0}"

NAME_PREFIX="${PROJECT}-${ENVIRONMENT}"
TENANTS_TABLE="${NAME_PREFIX}-tenants"
SECRETS_PREFIX="/${PROJECT}/${ENVIRONMENT}/tenants"
TENANT_ID="default"

echo "╔══════════════════════════════════════╗"
echo "║  T3nets Seed                         ║"
echo "║  Env: $ENVIRONMENT"
echo "╚══════════════════════════════════════╝"
echo ""
echo "→ Loaded .env"

# --- Seed tenant ---
echo "→ Seeding tenant '${TENANT_ID}' into DynamoDB..."
aws dynamodb put-item \
    --table-name "${TENANTS_TABLE}" \
    --region "${REGION}" \
    --item '{
        "pk": {"S": "TENANT#'"${TENANT_ID}"'"},
        "sk": {"S": "META"},
        "tenant_id": {"S": "'"${TENANT_ID}"'"},
        "name": {"S": "T3nets Default"},
        "status": {"S": "active"},
        "created_at": {"S": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"},
        "settings": {"S": "{\"enabled_skills\": [\"sprint_status\"], \"ai_model\": \"'"${MODEL_ID}"'\"}"}
    }' \
    --no-cli-pager
echo "  ✓ Tenant seeded"

# --- Seed admin user ---
echo "→ Seeding admin user..."
aws dynamodb put-item \
    --table-name "${TENANTS_TABLE}" \
    --region "${REGION}" \
    --item '{
        "pk": {"S": "TENANT#'"${TENANT_ID}"'"},
        "sk": {"S": "USER#admin"},
        "user_id": {"S": "admin"},
        "tenant_id": {"S": "'"${TENANT_ID}"'"},
        "email": {"S": "'"${JIRA_EMAIL:-admin@t3nets.dev}"'"},
        "display_name": {"S": "Admin"},
        "role": {"S": "admin"},
        "channel_identities": {"S": "{}"}
    }' \
    --no-cli-pager
echo "  ✓ Admin user seeded"

# --- Push Jira credentials ---
if [ -n "${JIRA_URL:-}" ] && [ -n "${JIRA_EMAIL:-}" ] && [ -n "${JIRA_API_TOKEN:-}" ]; then
    SECRET_ID="${SECRETS_PREFIX}/${TENANT_ID}/jira"
    SECRET_VALUE=$(cat <<JSONEOF
{
    "url": "${JIRA_URL}",
    "email": "${JIRA_EMAIL}",
    "api_token": "${JIRA_API_TOKEN}",
    "board_id": "${JIRA_BOARD_ID:-}"
}
JSONEOF
)

    echo "→ Pushing Jira credentials to Secrets Manager..."

    # Try update first, create if not found
    if aws secretsmanager describe-secret --secret-id "${SECRET_ID}" --region "${REGION}" --no-cli-pager 2>/dev/null; then
        aws secretsmanager update-secret \
            --secret-id "${SECRET_ID}" \
            --secret-string "${SECRET_VALUE}" \
            --region "${REGION}" \
            --no-cli-pager > /dev/null
    else
        aws secretsmanager create-secret \
            --name "${SECRET_ID}" \
            --secret-string "${SECRET_VALUE}" \
            --region "${REGION}" \
            --tags "Key=Project,Value=${PROJECT}" "Key=TenantId,Value=${TENANT_ID}" "Key=Integration,Value=jira" \
            --no-cli-pager > /dev/null
    fi
    echo "  ✓ Jira credentials stored at ${SECRET_ID}"
else
    echo "⚠ Jira credentials not found in .env — skipping"
fi

# --- Seed second tenant (for multi-tenancy testing) ---
TENANT2_ID="${SECOND_TENANT_ID:-acme}"
TENANT2_NAME="${SECOND_TENANT_NAME:-Acme Corp}"

echo "→ Seeding tenant '${TENANT2_ID}' into DynamoDB..."
aws dynamodb put-item \
    --table-name "${TENANTS_TABLE}" \
    --region "${REGION}" \
    --item '{
        "pk": {"S": "TENANT#'"${TENANT2_ID}"'"},
        "sk": {"S": "META"},
        "tenant_id": {"S": "'"${TENANT2_ID}"'"},
        "name": {"S": "'"${TENANT2_NAME}"'"},
        "status": {"S": "active"},
        "created_at": {"S": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"},
        "settings": {"S": "{\"enabled_skills\": [\"sprint_status\", \"ping\"], \"ai_model\": \"'"${MODEL_ID}"'\"}"}
    }' \
    --no-cli-pager
echo "  ✓ Tenant '${TENANT2_ID}' seeded"

echo ""
echo "✅ Seed complete. You can now deploy and test."
echo "   Tenants: ${TENANT_ID}, ${TENANT2_ID}"
echo ""
