"""
Error pattern catalog.

Maps regex patterns from known errors to friendly, actionable messages.
When a new error is encountered in production:
  1. Capture the raw error from logs
  2. Add a regex pattern here
  3. Write a friendly response following the voice guidelines
  4. Add a unit test
"""

import re
from agent.errors.models import FriendlyError, ErrorSeverity

# Each entry: (compiled_regex, FriendlyError template)
# Order matters — first match wins.

ERROR_PATTERNS: list[tuple[re.Pattern, FriendlyError]] = [
    # ── Bedrock / AI Provider ──────────────────────────────────────────────

    (
        re.compile(
            r"Invocation of model ID .* with on-demand throughput isn't supported",
            re.IGNORECASE,
        ),
        FriendlyError(
            message=(
                "I'm having trouble connecting to my AI model. It looks like the model ID "
                "needs to use an inference profile. Your admin can check the BEDROCK_MODEL_ID "
                "setting — it should start with a geographic prefix like us."
            ),
            severity=ErrorSeverity.CONFIG,
            error_code="BEDROCK_INFERENCE_PROFILE",
            action="Update BEDROCK_MODEL_ID to use an inference profile",
            admin_required=True,
        ),
    ),
    (
        re.compile(
            r"Model access is denied due to IAM user or service role is not authorized",
            re.IGNORECASE,
        ),
        FriendlyError(
            message=(
                "I don't have access to this AI model yet. Your admin needs to enable "
                "model access in the Amazon Bedrock console under Model access for the "
                "region this workspace uses."
            ),
            severity=ErrorSeverity.CONFIG,
            error_code="BEDROCK_MODEL_ACCESS",
            action="Enable model access in Bedrock console",
            admin_required=True,
        ),
    ),
    (
        re.compile(r"AccessDeniedException.*bedrock:InvokeModel", re.IGNORECASE),
        FriendlyError(
            message=(
                "I don't have permission to call my AI model. This is usually an IAM "
                "configuration issue — the ECS task role needs bedrock:InvokeModel "
                "permission for the inference profile ARN."
            ),
            severity=ErrorSeverity.CRITICAL,
            error_code="BEDROCK_IAM_DENIED",
            action="Add bedrock:InvokeModel to ECS task role",
            admin_required=True,
        ),
    ),
    (
        re.compile(r"ThrottlingException", re.IGNORECASE),
        FriendlyError(
            message=(
                "I'm getting a lot of requests right now and need to slow down. "
                "Try again in a moment — if this keeps happening, your admin may want "
                "to request a quota increase in AWS."
            ),
            severity=ErrorSeverity.INFO,
            error_code="THROTTLED",
            action="Retry in a moment",
        ),
    ),
    (
        re.compile(r"ModelTimeoutException", re.IGNORECASE),
        FriendlyError(
            message=(
                "My AI model is taking longer than expected to respond. This sometimes "
                "happens with complex questions. Try again, or try rephrasing with a "
                "simpler question."
            ),
            severity=ErrorSeverity.INFO,
            error_code="MODEL_TIMEOUT",
            action="Retry or simplify the question",
        ),
    ),
    (
        re.compile(r"ValidationException.*max_tokens", re.IGNORECASE),
        FriendlyError(
            message=(
                "The request was too large for the AI model to handle. "
                "Try breaking your question into smaller parts."
            ),
            severity=ErrorSeverity.INFO,
            error_code="MAX_TOKENS_EXCEEDED",
            action="Break question into smaller parts",
        ),
    ),
    (
        re.compile(r"ValidationException", re.IGNORECASE),
        FriendlyError(
            message=(
                "The AI model couldn't process that request due to a validation issue. "
                "This might be a configuration problem — check the model settings on the "
                "Settings page."
            ),
            severity=ErrorSeverity.CONFIG,
            error_code="BEDROCK_VALIDATION",
            action="Check model configuration in Settings",
            admin_required=True,
        ),
    ),

    # ── Jira Integration ──────────────────────────────────────────────────

    (
        re.compile(r"Jira integration is not configured", re.IGNORECASE),
        FriendlyError(
            message=(
                "I can't access Jira yet — your workspace hasn't connected a Jira "
                "instance. Your admin can set up the connection by running the seed script "
                "with Jira credentials in the .env file."
            ),
            severity=ErrorSeverity.CONFIG,
            error_code="JIRA_NOT_CONFIGURED",
            action="Configure Jira integration",
            admin_required=True,
        ),
    ),
    (
        re.compile(r"401.*Unauthorized.*jira|jira.*401.*Unauthorized", re.IGNORECASE),
        FriendlyError(
            message=(
                "I can't log into Jira — the API token may have expired or been revoked. "
                "Your admin can update the token in the Jira integration settings."
            ),
            severity=ErrorSeverity.CONFIG,
            error_code="JIRA_AUTH_EXPIRED",
            action="Update Jira API token",
            admin_required=True,
        ),
    ),
    (
        re.compile(r"403.*Forbidden.*jira|jira.*403.*Forbidden", re.IGNORECASE),
        FriendlyError(
            message=(
                "I don't have permission to access that Jira project. Make sure the "
                "connected Jira account has access to the board you're asking about."
            ),
            severity=ErrorSeverity.CONFIG,
            error_code="JIRA_FORBIDDEN",
            action="Check Jira account permissions",
            admin_required=True,
        ),
    ),
    (
        re.compile(r"404.*Board not found|Board not found.*404", re.IGNORECASE),
        FriendlyError(
            message=(
                "I can't find that Jira board. It may have been deleted or renamed. "
                "Check the board ID in your integration settings."
            ),
            severity=ErrorSeverity.CONFIG,
            error_code="JIRA_BOARD_NOT_FOUND",
            action="Verify Jira board ID",
            admin_required=True,
        ),
    ),
    (
        re.compile(r"ConnectionError.*jira|jira.*ConnectionError", re.IGNORECASE),
        FriendlyError(
            message=(
                "I can't reach your Jira instance right now. This could be a network "
                "issue or the Jira server might be down. Try again in a few minutes."
            ),
            severity=ErrorSeverity.INFO,
            error_code="JIRA_UNREACHABLE",
            action="Retry in a few minutes",
        ),
    ),

    # ── Secrets Manager ───────────────────────────────────────────────────

    (
        re.compile(r"ResourceNotFoundException.*secret", re.IGNORECASE),
        FriendlyError(
            message=(
                "I'm missing some configuration data. Your admin needs to run the seed "
                "script to set up workspace credentials."
            ),
            severity=ErrorSeverity.CRITICAL,
            error_code="SECRETS_NOT_FOUND",
            action="Run scripts/seed.sh",
            admin_required=True,
        ),
    ),
    (
        re.compile(r"AccessDeniedException.*secretsmanager", re.IGNORECASE),
        FriendlyError(
            message=(
                "I can't access the credential store. This is an AWS permissions issue "
                "— the ECS task role needs Secrets Manager read access."
            ),
            severity=ErrorSeverity.CRITICAL,
            error_code="SECRETS_IAM_DENIED",
            action="Add secretsmanager:GetSecretValue to ECS task role",
            admin_required=True,
        ),
    ),

    # ── DynamoDB ──────────────────────────────────────────────────────────

    (
        re.compile(r"ResourceNotFoundException.*table", re.IGNORECASE),
        FriendlyError(
            message=(
                "I can't find my database tables. They may not have been created yet "
                "— your admin should verify the Terraform deployment completed successfully."
            ),
            severity=ErrorSeverity.CRITICAL,
            error_code="DYNAMODB_TABLE_MISSING",
            action="Verify Terraform deployment",
            admin_required=True,
        ),
    ),
    (
        re.compile(r"ProvisionedThroughputExceededException", re.IGNORECASE),
        FriendlyError(
            message=(
                "The database is under heavy load right now. Try again in a moment."
            ),
            severity=ErrorSeverity.INFO,
            error_code="DYNAMODB_THROTTLED",
            action="Retry in a moment",
        ),
    ),

    # ── Anthropic Direct API (local dev) ──────────────────────────────────

    (
        re.compile(r"AuthenticationError|invalid.*api.key|401.*anthropic", re.IGNORECASE),
        FriendlyError(
            message=(
                "I can't authenticate with the AI provider. The API key might be "
                "invalid or expired. Check your ANTHROPIC_API_KEY in the .env file."
            ),
            severity=ErrorSeverity.CONFIG,
            error_code="ANTHROPIC_AUTH",
            action="Check ANTHROPIC_API_KEY in .env",
            admin_required=True,
        ),
    ),
    (
        re.compile(r"RateLimitError|rate.limit|429", re.IGNORECASE),
        FriendlyError(
            message=(
                "I've hit the API rate limit. Give me a moment and try again. "
                "If this keeps happening, the usage tier may need upgrading."
            ),
            severity=ErrorSeverity.INFO,
            error_code="RATE_LIMITED",
            action="Retry in a moment",
        ),
    ),
]


# Pre-built generic fallback
GENERIC_ERROR = FriendlyError(
    message=(
        "Something unexpected went wrong. I've logged the details so the team can "
        "investigate. In the meantime, try again or rephrase your question. "
        "If the problem persists, check the system health dashboard."
    ),
    severity=ErrorSeverity.INFO,
    error_code="UNKNOWN",
    action="Retry or check health dashboard",
)
