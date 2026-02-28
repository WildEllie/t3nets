"""
AWS Lambda Skill Executor — Event handler for async skill invocations.

Triggered by EventBridge rule matching:
    source: "agent.router"
    detail-type: "skill.invoke"

Flow:
    1. Parse event → extract skill_name, params, request_id, tenant_id
    2. Idempotency check → skip if pending request already completed
    3. Load skill worker, fetch secrets, execute skill
    4. Mark pending request completed (conditional update — exactly-once)
    5. Publish result to SQS for the router to pick up

Environment Variables:
    T3NETS_PLATFORM     — "aws"
    T3NETS_STAGE        — "dev" / "staging" / "prod"
    AWS_REGION_NAME     — e.g., "us-east-1"
    SECRETS_PREFIX      — Secrets Manager prefix, e.g., "/t3nets/dev"
    SQS_RESULTS_QUEUE_URL — SQS queue URL for skill results
    PENDING_REQUESTS_TABLE — DynamoDB table for pending requests
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

import boto3

# Add project root to path so we can import agent.skills etc.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.skills.registry import SkillRegistry, SkillNotFound
from adapters.aws.secrets_manager import SecretsManagerProvider
from adapters.aws.pending_requests import PendingRequestsStore

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --- Lazy-initialized globals (cold start optimization) ---
_skills: SkillRegistry | None = None
_secrets: SecretsManagerProvider | None = None
_pending: PendingRequestsStore | None = None
_sqs_client = None

REGION = os.environ.get("AWS_REGION_NAME", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
SQS_QUEUE_URL = os.environ.get("SQS_RESULTS_QUEUE_URL", "")
PENDING_TABLE = os.environ.get("PENDING_REQUESTS_TABLE", "")
SECRETS_PREFIX = os.environ.get("SECRETS_PREFIX", "/t3nets/dev")


def _init_skills() -> SkillRegistry:
    """Lazy-load the skill registry on first invocation."""
    global _skills
    if _skills is None:
        _skills = SkillRegistry()
        skills_dir = Path(__file__).parent.parent.parent / "agent" / "skills"
        _skills.load_from_directory(skills_dir)
        logger.info(f"Lambda: loaded skills: {_skills.list_skill_names()}")
    return _skills


def _init_secrets() -> SecretsManagerProvider:
    """Lazy-load the secrets provider."""
    global _secrets
    if _secrets is None:
        _secrets = SecretsManagerProvider(SECRETS_PREFIX, region=REGION)
    return _secrets


def _init_pending() -> PendingRequestsStore:
    """Lazy-load the pending requests store."""
    global _pending
    if _pending is None:
        _pending = PendingRequestsStore(PENDING_TABLE, region=REGION)
    return _pending


def _init_sqs():
    """Lazy-load the SQS client."""
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs", region_name=REGION)
    return _sqs_client


def handler(event: dict, context) -> dict:
    """
    Lambda entry point. Receives EventBridge event with skill invocation details.

    Event structure (EventBridge detail):
        {
            "tenant_id": "...",
            "skill_name": "...",
            "params": {...},
            "request_id": "...",
            "session_id": "...",
            "reply_channel": "dashboard|teams|telegram",
            "reply_target": "..."
        }
    """
    start_time = time.time()

    # EventBridge wraps the payload in "detail"
    detail = event.get("detail", event)

    request_id = detail.get("request_id", "")
    skill_name = detail.get("skill_name", "")
    tenant_id = detail.get("tenant_id", "")
    params = detail.get("params", {})

    logger.info(
        f"Lambda: invoked for skill={skill_name}, "
        f"request={request_id[:8]}, tenant={tenant_id}"
    )

    # --- Step 1: Idempotency check ---
    pending = _init_pending()
    status = pending.get_status(request_id)
    if status == "completed":
        logger.info(f"Lambda: request {request_id[:8]} already completed (idempotency skip)")
        return {"statusCode": 200, "body": "Already completed"}

    if status is None:
        logger.warning(f"Lambda: request {request_id[:8]} not found in pending table")
        # Proceed anyway — the request might have expired from TTL but we still
        # want to try executing the skill and returning a result.

    # --- Step 2: Load skill worker + secrets ---
    skills = _init_skills()
    secrets_provider = _init_secrets()

    try:
        worker_fn = skills.get_worker(skill_name)
    except SkillNotFound as e:
        logger.error(f"Lambda: skill not found: {skill_name}")
        _send_result(request_id, detail, {"error": str(e)})
        return {"statusCode": 400, "body": f"Skill not found: {skill_name}"}

    # Fetch integration secrets if the skill requires them
    skill_def = skills.get_skill(skill_name)
    skill_secrets: dict = {}
    if skill_def and skill_def.requires_integration:
        try:
            import asyncio
            skill_secrets = asyncio.get_event_loop().run_until_complete(
                secrets_provider.get(tenant_id, skill_def.requires_integration)
            )
        except Exception as e:
            logger.error(
                f"Lambda: failed to get secrets for "
                f"{skill_def.requires_integration}: {e}"
            )
            _send_result(request_id, detail, {
                "error": f"Integration not configured: {skill_def.requires_integration}"
            })
            return {"statusCode": 400, "body": "Integration not configured"}

    # --- Step 3: Execute the skill ---
    try:
        result = worker_fn(params, skill_secrets)
        logger.info(
            f"Lambda: skill {skill_name} completed in "
            f"{time.time() - start_time:.2f}s"
        )
    except Exception as e:
        logger.exception(f"Lambda: skill {skill_name} failed")
        result = {"error": f"Skill execution failed: {e}"}

    # --- Step 4: Mark completed (idempotent conditional update) ---
    was_pending = pending.mark_completed(request_id)
    if not was_pending:
        logger.info(
            f"Lambda: request {request_id[:8]} was already completed by another invocation"
        )
        # Another Lambda already processed this — don't send duplicate result
        return {"statusCode": 200, "body": "Already completed by another invocation"}

    # --- Step 5: Send result to SQS ---
    _send_result(request_id, detail, result)

    return {"statusCode": 200, "body": "OK"}


def _send_result(request_id: str, detail: dict, result: dict) -> None:
    """Publish skill result to SQS for the router to pick up."""
    sqs = _init_sqs()
    message = {
        "request_id": request_id,
        "tenant_id": detail.get("tenant_id", ""),
        "skill_name": detail.get("skill_name", ""),
        "reply_channel": detail.get("reply_channel", ""),
        "reply_target": detail.get("reply_target", ""),
        "session_id": detail.get("session_id", ""),
        "result": result,
    }

    try:
        sqs.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps(message),
            MessageGroupId=request_id if ".fifo" in SQS_QUEUE_URL else None,
        )
        logger.info(f"Lambda: result sent to SQS for request {request_id[:8]}")
    except Exception as e:
        logger.exception(f"Lambda: failed to send result to SQS: {e}")
        raise
