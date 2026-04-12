#!/bin/bash
###############################################################################
# Build Lambda Base — Ping Skill
#
# Builds a minimal Lambda ZIP containing only the ping skill plus the
# framework code needed to run it. Used by Terraform to deploy the ping
# Lambda with real code (not a placeholder).
#
# Usage:
#   ./scripts/build_lambda_base.sh                              # default output
#   ./scripts/build_lambda_base.sh /tmp/test.zip                # custom output
#
# Contents:
#   - adapters/aws/lambda_handler.py, pending_requests.py, secrets_manager.py
#   - agent/skills/registry.py + agent/skills/ping/ only
#   - agent/interfaces/, agent/models/ (framework imports)
#   - t3nets-sdk (contracts, manifest validators, interfaces)
#   - PyYAML, pydantic (pip installed via sdk)
#   - __init__.py files for all packages
###############################################################################

set -euo pipefail

# Output path: first argument or default
OUTPUT_PATH="${1:-$(cd "$(dirname "$0")/.." && pwd)/infra/aws/modules/compute/lambda_base.zip}"

# Project root is one level up from scripts/
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "→ Building ping Lambda ZIP..."
echo "  Project root: ${PROJECT_ROOT}"
echo "  Output: ${OUTPUT_PATH}"

# --- Create temp build directory ---
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "${BUILD_DIR}"' EXIT

# --- Copy AWS adapter files needed by Lambda ---
mkdir -p "${BUILD_DIR}/adapters/aws"
touch "${BUILD_DIR}/adapters/__init__.py"
touch "${BUILD_DIR}/adapters/aws/__init__.py"
cp "${PROJECT_ROOT}/adapters/aws/lambda_handler.py" "${BUILD_DIR}/adapters/aws/"
cp "${PROJECT_ROOT}/adapters/aws/pending_requests.py" "${BUILD_DIR}/adapters/aws/"
cp "${PROJECT_ROOT}/adapters/aws/secrets_manager.py" "${BUILD_DIR}/adapters/aws/"
cp "${PROJECT_ROOT}/adapters/aws/s3_blob_store.py" "${BUILD_DIR}/adapters/aws/"
cp "${PROJECT_ROOT}/adapters/aws/dynamodb_tenant_store.py" "${BUILD_DIR}/adapters/aws/"

# --- Copy agent framework (interfaces, models) ---
mkdir -p "${BUILD_DIR}/agent"
touch "${BUILD_DIR}/agent/__init__.py"

cp -r "${PROJECT_ROOT}/agent/interfaces" "${BUILD_DIR}/agent/"
touch "${BUILD_DIR}/agent/interfaces/__init__.py"

cp -r "${PROJECT_ROOT}/agent/models" "${BUILD_DIR}/agent/"
touch "${BUILD_DIR}/agent/models/__init__.py"

# --- Copy skill registry + ping skill only ---
mkdir -p "${BUILD_DIR}/agent/skills/ping"
touch "${BUILD_DIR}/agent/skills/__init__.py"
cp "${PROJECT_ROOT}/agent/skills/registry.py" "${BUILD_DIR}/agent/skills/"
cp -r "${PROJECT_ROOT}/agent/skills/ping/" "${BUILD_DIR}/agent/skills/ping/"

# --- Copy practice registry (for loading uploaded practice skills) ---
mkdir -p "${BUILD_DIR}/agent/practices"
touch "${BUILD_DIR}/agent/practices/__init__.py"
cp "${PROJECT_ROOT}/agent/practices/registry.py" "${BUILD_DIR}/agent/practices/"
# Copy built-in practices (dev-jira etc.)
cp -r "${PROJECT_ROOT}/agent/practices/dev-jira" "${BUILD_DIR}/agent/practices/" 2>/dev/null || true

# --- Install PyYAML + t3nets-sdk (boto3 is in Lambda runtime) ---
pip3 install pyyaml "${PROJECT_ROOT}/sdk" -t "${BUILD_DIR}" --quiet 2>/dev/null

# --- Remove __pycache__ dirs ---
find "${BUILD_DIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# --- Create ZIP ---
mkdir -p "$(dirname "${OUTPUT_PATH}")"
(cd "${BUILD_DIR}" && zip -r "${OUTPUT_PATH}" . -x '*.pyc' '__pycache__/*' > /dev/null)

ZIP_SIZE=$(du -h "${OUTPUT_PATH}" | cut -f1)
echo "  ZIP size: ${ZIP_SIZE}"
echo "  ✅ Lambda base ZIP built"
