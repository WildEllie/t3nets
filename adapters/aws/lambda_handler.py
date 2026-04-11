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
from typing import Any

import boto3  # type: ignore[import-untyped]

# Add project root to path so we can import agent.skills etc.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import asyncio

from t3nets_sdk.contracts import SkillContext

from agent.skills.registry import SkillRegistry, SkillNotFound
from agent.practices.registry import PracticeRegistry
from adapters.aws.secrets_manager import SecretsManagerProvider
from adapters.aws.s3_blob_store import S3BlobStore
from adapters.aws.pending_requests import PendingRequestsStore
from adapters.aws.dynamodb_tenant_store import DynamoDBTenantStore

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --- Lazy-initialized globals (cold start optimization) ---
_skills: SkillRegistry | None = None
_secrets: SecretsManagerProvider | None = None
_pending: PendingRequestsStore | None = None
_blobs: S3BlobStore | None = None
_tenants: DynamoDBTenantStore | None = None
_sqs_client = None

REGION = os.environ.get("AWS_REGION_NAME", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
SQS_QUEUE_URL = os.environ.get("SQS_RESULTS_QUEUE_URL", "")
PENDING_TABLE = os.environ.get("PENDING_REQUESTS_TABLE", "")
SECRETS_PREFIX = os.environ.get("SECRETS_PREFIX", "/t3nets/dev")
S3_BUCKET = os.environ.get("S3_BUCKET_NAME", "t3nets-dev-static")
TENANTS_TABLE = os.environ.get("DYNAMODB_TENANTS_TABLE", "t3nets-dev-tenants")
DEFAULT_TENANT = "default"


def _init_blobs() -> S3BlobStore:
    """Lazy-load the S3 BlobStore."""
    global _blobs
    if _blobs is None:
        _blobs = S3BlobStore(bucket_name=S3_BUCKET, region=REGION)
    return _blobs


def _init_tenants() -> DynamoDBTenantStore:
    """Lazy-load the tenant store."""
    global _tenants
    if _tenants is None:
        _tenants = DynamoDBTenantStore(TENANTS_TABLE, region=REGION)
    return _tenants


def _init_skills() -> SkillRegistry:
    """Lazy-load the skill registry + uploaded practices on first invocation."""
    global _skills
    if _skills is None:
        _skills = SkillRegistry()
        skills_dir = Path(__file__).parent.parent.parent / "agent" / "skills"
        _skills.load_from_directory(skills_dir)

        # Load uploaded practices from S3 (same as router does on startup)
        try:
            practices = PracticeRegistry()
            practices_dir = Path(__file__).parent.parent.parent / "agent" / "practices"
            practices.load_builtin(practices_dir)

            blobs = _init_blobs()
            tenants_store = _init_tenants()
            tenant = asyncio.get_event_loop().run_until_complete(
                tenants_store.get_tenant(DEFAULT_TENANT)
            )
            installed = tenant.settings.installed_practices
            data_dir = Path("/tmp/practices_data")
            data_dir.mkdir(exist_ok=True)

            if installed:
                restored = asyncio.get_event_loop().run_until_complete(
                    practices.restore_from_blob_store(blobs, DEFAULT_TENANT, data_dir, installed)
                )
                if restored:
                    logger.info(f"Lambda: restored {restored} practice(s) from S3")
                practices.load_uploaded(data_dir)

            practices.register_skills(_skills)
            logger.info(f"Lambda: loaded skills: {_skills.list_skill_names()}")
        except Exception as e:
            logger.warning(f"Lambda: practice loading failed: {e}")
            logger.info(f"Lambda: loaded skills (built-in only): {_skills.list_skill_names()}")

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


def _init_sqs() -> Any:
    """Lazy-load the SQS client."""
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs", region_name=REGION)
    return _sqs_client


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
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
        f"Lambda: invoked for skill={skill_name}, request={request_id[:8]}, tenant={tenant_id}"
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
    skill_secrets: dict[str, Any] = {}
    if skill_def and skill_def.requires_integration:
        try:
            skill_secrets = asyncio.get_event_loop().run_until_complete(
                secrets_provider.get(tenant_id, skill_def.requires_integration)
            )
        except Exception as e:
            logger.error(f"Lambda: failed to get secrets for {skill_def.requires_integration}: {e}")
            _send_result(
                request_id,
                detail,
                {"error": f"Integration not configured: {skill_def.requires_integration}"},
            )
            return {"statusCode": 400, "body": "Integration not configured"}

    # --- Step 3: Execute the skill ---
    try:
        skill_ctx = SkillContext(
            tenant_id=tenant_id,
            secrets=skill_secrets,
            logger=logging.getLogger(f"t3nets.skill.{skill_name}"),
            blob_store=_init_blobs(),
        )
        skill_result = asyncio.get_event_loop().run_until_complete(worker_fn(skill_ctx, params))
        result = skill_result.to_dict()

        logger.info(f"Lambda: skill {skill_name} completed in {time.time() - start_time:.2f}s")
    except Exception as e:
        logger.exception(f"Lambda: skill {skill_name} failed")
        result = {"error": f"Skill execution failed: {e}"}

    # --- Step 4: Mark completed (idempotent conditional update) ---
    was_pending = pending.mark_completed(request_id)
    if not was_pending:
        logger.info(f"Lambda: request {request_id[:8]} was already completed by another invocation")
        # Another Lambda already processed this — don't send duplicate result
        return {"statusCode": 200, "body": "Already completed by another invocation"}

    # --- Step 5: Send result to SQS ---
    _send_result(request_id, detail, result)

    return {"statusCode": 200, "body": "OK"}


SQS_MAX_BYTES = 250_000  # SQS limit is 262144, leave margin for envelope


def _offload_audio_to_s3(result: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    """If result has large audio_b64, upload to S3 and replace with presigned URL."""
    audio_b64 = result.get("audio_b64", "")
    if not audio_b64 or not S3_BUCKET:
        return result

    import base64
    import uuid

    audio_bytes = base64.b64decode(audio_b64)
    if len(audio_b64) < SQS_MAX_BYTES:
        return result  # Small enough for inline

    s3 = boto3.client("s3", region_name=REGION)
    key = f"{tenant_id}/audio/{uuid.uuid4().hex}.wav"
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=audio_bytes, ContentType="audio/wav")
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=3600,
    )
    logger.info(f"Lambda: audio offloaded to S3 ({len(audio_bytes)} bytes)")

    # Replace inline audio with URL
    result = dict(result)
    del result["audio_b64"]
    result["audio_url"] = url
    return result


def _send_result(request_id: str, detail: dict[str, Any], result: dict[str, Any]) -> None:
    """Publish skill result to SQS for the router to pick up."""
    # Offload large audio to S3 before sending via SQS
    if result.get("type") == "audio" and result.get("audio_b64"):
        result = _offload_audio_to_s3(result, detail.get("tenant_id", "default"))

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
        send_kwargs: dict[str, Any] = {
            "QueueUrl": SQS_QUEUE_URL,
            "MessageBody": json.dumps(message),
        }
        if ".fifo" in SQS_QUEUE_URL:
            send_kwargs["MessageGroupId"] = request_id
        sqs.send_message(**send_kwargs)
        logger.info(f"Lambda: result sent to SQS for request {request_id[:8]}")
    except Exception as e:
        logger.exception(f"Lambda: failed to send result to SQS: {e}")
        raise
