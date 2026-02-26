#!/bin/bash
###############################################################################
# T3nets Seed Script
#
# Seeds tenants into DynamoDB, creates Cognito users, links them via
# cognito_sub, and pushes integration credentials to Secrets Manager.
# Run once after terraform apply.
#
# Usage:
#   ./scripts/seed.sh
#
# Required env vars (from .env):
#   COGNITO_USER_POOL_ID  — Cognito user pool ID
#   COGNITO_CLIENT_ID     — Cognito app client ID
#   ADMIN_EMAIL           — Primary admin email (default: JIRA_EMAIL)
#   ADMIN_PASSWORD        — Primary admin password
#
# Optional:
#   SECOND_TENANT_ID, SECOND_TENANT_NAME, SECOND_TENANT_EMAIL,
#   SECOND_TENANT_PASSWORD
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

# Tenant config
TENANT_ID="${TENANT_ID:-default}"
ADMIN_EMAIL="${ADMIN_EMAIL:-${JIRA_EMAIL:-admin@t3nets.dev}}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"

TENANT2_ID="${SECOND_TENANT_ID:-acme}"
TENANT2_NAME="${SECOND_TENANT_NAME:-Acme Corp}"
TENANT2_EMAIL="${SECOND_TENANT_EMAIL:-admin@acme.dev}"
TENANT2_PASSWORD="${SECOND_TENANT_PASSWORD:-}"

# Cognito config
POOL_ID="${COGNITO_USER_POOL_ID:-}"
CLIENT_ID="${COGNITO_CLIENT_ID:-}"

echo "╔══════════════════════════════════════╗"
echo "║  T3nets Seed                         ║"
echo "║  Env: $ENVIRONMENT"
echo "╚══════════════════════════════════════╝"
echo ""
echo "→ Loaded .env"

###############################################################################
# Helper: Create a Cognito user and return its sub
#
# Usage: create_cognito_user <email> <password>
# Returns: Cognito sub (UUID) or empty string on failure
###############################################################################
create_cognito_user() {
    local email="$1"
    local password="$2"

    if [ -z "${POOL_ID}" ]; then
        echo ""
        return
    fi

    # Check if user already exists
    local existing_sub
    existing_sub=$(aws cognito-idp admin-get-user \
        --user-pool-id "${POOL_ID}" \
        --username "${email}" \
        --region "${REGION}" \
        --query "UserAttributes[?Name=='sub'].Value" \
        --output text 2>/dev/null || echo "")

    if [ -n "${existing_sub}" ] && [ "${existing_sub}" != "None" ]; then
        echo "${existing_sub}"
        return
    fi

    # Create user
    aws cognito-idp admin-create-user \
        --user-pool-id "${POOL_ID}" \
        --username "${email}" \
        --user-attributes Name=email,Value="${email}" Name=email_verified,Value=true \
        --message-action SUPPRESS \
        --region "${REGION}" \
        --no-cli-pager > /dev/null 2>&1

    # Set permanent password (skip force-change flow)
    if [ -n "${password}" ]; then
        aws cognito-idp admin-set-user-password \
            --user-pool-id "${POOL_ID}" \
            --username "${email}" \
            --password "${password}" \
            --permanent \
            --region "${REGION}" \
            --no-cli-pager > /dev/null 2>&1
    fi

    # Get the sub
    local sub
    sub=$(aws cognito-idp admin-get-user \
        --user-pool-id "${POOL_ID}" \
        --username "${email}" \
        --region "${REGION}" \
        --query "UserAttributes[?Name=='sub'].Value" \
        --output text 2>/dev/null || echo "")

    echo "${sub}"
}

# ===========================================================================
# TENANT 1: Default
# ===========================================================================

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

# --- Create Cognito user for primary admin ---
ADMIN_SUB=""
if [ -n "${POOL_ID}" ]; then
    echo "→ Creating Cognito user for ${ADMIN_EMAIL}..."
    ADMIN_SUB=$(create_cognito_user "${ADMIN_EMAIL}" "${ADMIN_PASSWORD}")
    if [ -n "${ADMIN_SUB}" ] && [ "${ADMIN_SUB}" != "None" ]; then
        echo "  ✓ Cognito user created (sub: ${ADMIN_SUB:0:8}...)"
    else
        echo "  ⚠ Could not create/find Cognito user — DynamoDB user will lack cognito_sub"
        ADMIN_SUB=""
    fi
else
    echo "  ⚠ COGNITO_USER_POOL_ID not set — skipping Cognito user creation"
fi

# --- Seed admin user (with cognito_sub + GSI key) ---
echo "→ Seeding admin user..."
if [ -n "${ADMIN_SUB}" ]; then
    aws dynamodb put-item \
        --table-name "${TENANTS_TABLE}" \
        --region "${REGION}" \
        --item '{
            "pk": {"S": "TENANT#'"${TENANT_ID}"'"},
            "sk": {"S": "USER#admin"},
            "user_id": {"S": "admin"},
            "tenant_id": {"S": "'"${TENANT_ID}"'"},
            "email": {"S": "'"${ADMIN_EMAIL}"'"},
            "display_name": {"S": "Admin"},
            "role": {"S": "admin"},
            "channel_identities": {"S": "{}"},
            "cognito_sub": {"S": "'"${ADMIN_SUB}"'"},
            "gsi2pk": {"S": "COGNITO#'"${ADMIN_SUB}"'"}
        }' \
        --no-cli-pager
else
    aws dynamodb put-item \
        --table-name "${TENANTS_TABLE}" \
        --region "${REGION}" \
        --item '{
            "pk": {"S": "TENANT#'"${TENANT_ID}"'"},
            "sk": {"S": "USER#admin"},
            "user_id": {"S": "admin"},
            "tenant_id": {"S": "'"${TENANT_ID}"'"},
            "email": {"S": "'"${ADMIN_EMAIL}"'"},
            "display_name": {"S": "Admin"},
            "role": {"S": "admin"},
            "channel_identities": {"S": "{}"}
        }' \
        --no-cli-pager
fi
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

# ===========================================================================
# TENANT 2: Acme (multi-tenancy testing)
# ===========================================================================

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

# --- Create Cognito user for second tenant admin ---
TENANT2_SUB=""
if [ -n "${POOL_ID}" ] && [ -n "${TENANT2_EMAIL}" ]; then
    echo "→ Creating Cognito user for ${TENANT2_EMAIL}..."
    TENANT2_SUB=$(create_cognito_user "${TENANT2_EMAIL}" "${TENANT2_PASSWORD}")
    if [ -n "${TENANT2_SUB}" ] && [ "${TENANT2_SUB}" != "None" ]; then
        echo "  ✓ Cognito user created (sub: ${TENANT2_SUB:0:8}...)"
    else
        echo "  ⚠ Could not create/find Cognito user for ${TENANT2_EMAIL}"
        TENANT2_SUB=""
    fi
fi

# --- Seed admin user for second tenant ---
echo "→ Seeding admin user for '${TENANT2_ID}'..."
if [ -n "${TENANT2_SUB}" ]; then
    aws dynamodb put-item \
        --table-name "${TENANTS_TABLE}" \
        --region "${REGION}" \
        --item '{
            "pk": {"S": "TENANT#'"${TENANT2_ID}"'"},
            "sk": {"S": "USER#admin"},
            "user_id": {"S": "admin"},
            "tenant_id": {"S": "'"${TENANT2_ID}"'"},
            "email": {"S": "'"${TENANT2_EMAIL}"'"},
            "display_name": {"S": "Acme Admin"},
            "role": {"S": "admin"},
            "channel_identities": {"S": "{}"},
            "cognito_sub": {"S": "'"${TENANT2_SUB}"'"},
            "gsi2pk": {"S": "COGNITO#'"${TENANT2_SUB}"'"}
        }' \
        --no-cli-pager
else
    aws dynamodb put-item \
        --table-name "${TENANTS_TABLE}" \
        --region "${REGION}" \
        --item '{
            "pk": {"S": "TENANT#'"${TENANT2_ID}"'"},
            "sk": {"S": "USER#admin"},
            "user_id": {"S": "admin"},
            "tenant_id": {"S": "'"${TENANT2_ID}"'"},
            "email": {"S": "'"${TENANT2_EMAIL}"'"},
            "display_name": {"S": "Acme Admin"},
            "role": {"S": "admin"},
            "channel_identities": {"S": "{}"}
        }' \
        --no-cli-pager
fi
echo "  ✓ Admin user for '${TENANT2_ID}' seeded"

# ===========================================================================
# Summary
# ===========================================================================

echo ""
echo "✅ Seed complete. You can now deploy and test."
echo "   Tenants: ${TENANT_ID}, ${TENANT2_ID}"
if [ -n "${ADMIN_SUB}" ]; then
    echo "   Admin:   ${ADMIN_EMAIL} (Cognito linked ✓)"
else
    echo "   Admin:   ${ADMIN_EMAIL} (⚠ no Cognito user — add COGNITO_USER_POOL_ID + ADMIN_PASSWORD to .env)"
fi
echo ""
