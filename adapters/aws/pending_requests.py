"""
AWS Pending Requests Store — DynamoDB.

Tracks in-flight async skill invocations. Enables:
1. Any router instance to pick up any result (horizontal scaling)
2. Lambda idempotency (check status before executing)
3. Channel context recovery (service_url for Teams, reply_target, etc.)

Schema:
    pk: {request_id}
    Attributes: tenant_id, skill_name, channel, conversation_id,
                reply_target, service_url, is_raw, status, user_key,
                user_message, created_at
    TTL: ttl (Unix epoch, 5 min after creation)

Status flow: pending → completed
"""

import logging
import time
from dataclasses import dataclass

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

# TTL: 5 minutes from creation
PENDING_TTL_SECONDS = 300


@dataclass
class PendingRequest:
    """In-flight async skill invocation."""

    request_id: str
    tenant_id: str
    skill_name: str
    channel: str  # "dashboard" | "teams" | "telegram"
    conversation_id: str
    reply_target: str  # channel-specific: user_id, chat_id, etc.
    user_key: str  # SSE user key (email or sub)
    status: str = "pending"  # "pending" | "completed"
    service_url: str = ""  # Teams Bot Framework service URL
    is_raw: bool = False  # --raw flag
    user_message: str = ""  # original user message (for conversation saving)
    created_at: float = 0.0  # Unix timestamp
    model_id: str = ""  # Bedrock model ID (with geo prefix) for AI formatting
    model_short_name: str = ""  # Short display name (e.g. "Nova Micro")
    route_type: str = ""  # "rule" or "ai" — original routing decision


class PendingRequestsStore:
    """DynamoDB-backed store for pending async skill requests."""

    def __init__(self, table_name: str, region: str = "us-east-1"):
        self.table_name = table_name
        dynamodb = boto3.resource("dynamodb", region_name=region)
        self.table = dynamodb.Table(table_name)

    def create(self, request: PendingRequest) -> None:
        """Store a new pending request."""
        now = time.time()
        request.created_at = now

        self.table.put_item(
            Item={
                "pk": request.request_id,
                "tenant_id": request.tenant_id,
                "skill_name": request.skill_name,
                "channel": request.channel,
                "conversation_id": request.conversation_id,
                "reply_target": request.reply_target,
                "user_key": request.user_key,
                "status": "pending",
                "service_url": request.service_url,
                "is_raw": request.is_raw,
                "user_message": request.user_message,
                "model_id": request.model_id,
                "model_short_name": request.model_short_name,
                "route_type": request.route_type,
                "created_at": str(now),
                "ttl": int(now + PENDING_TTL_SECONDS),
            }
        )

        logger.info(
            f"PendingRequests: created {request.request_id[:8]} "
            f"(skill={request.skill_name}, channel={request.channel})"
        )

    def get(self, request_id: str) -> PendingRequest | None:
        """Retrieve a pending request by ID. Returns None if not found or expired."""
        try:
            response = self.table.get_item(Key={"pk": request_id})
        except ClientError as e:
            logger.error(f"PendingRequests: get failed for {request_id[:8]}: {e}")
            return None

        item = response.get("Item")
        if not item:
            return None

        return PendingRequest(
            request_id=item["pk"],
            tenant_id=item.get("tenant_id", ""),
            skill_name=item.get("skill_name", ""),
            channel=item.get("channel", ""),
            conversation_id=item.get("conversation_id", ""),
            reply_target=item.get("reply_target", ""),
            user_key=item.get("user_key", ""),
            status=item.get("status", "pending"),
            service_url=item.get("service_url", ""),
            is_raw=item.get("is_raw", False),
            user_message=item.get("user_message", ""),
            created_at=float(item.get("created_at", 0)),
            model_id=item.get("model_id", ""),
            model_short_name=item.get("model_short_name", ""),
            route_type=item.get("route_type", ""),
        )

    def mark_completed(self, request_id: str) -> bool:
        """Atomically mark a request as completed. Returns False if already completed.

        Uses a conditional update to guarantee idempotency — only one Lambda
        invocation can transition from pending → completed.
        """
        try:
            self.table.update_item(
                Key={"pk": request_id},
                UpdateExpression="SET #s = :completed",
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":completed": "completed",
                    ":pending": "pending",
                },
            )
            logger.info(f"PendingRequests: marked {request_id[:8]} as completed")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.info(
                    f"PendingRequests: {request_id[:8]} already completed (idempotency skip)"
                )
                return False
            raise

    def get_status(self, request_id: str) -> str | None:
        """Get just the status field. Used by Lambda for fast idempotency check."""
        try:
            response = self.table.get_item(
                Key={"pk": request_id},
                ProjectionExpression="#s",
                ExpressionAttributeNames={"#s": "status"},
            )
        except ClientError:
            return None

        item = response.get("Item")
        if not item:
            return None
        status = item.get("status")
        return str(status) if status is not None else None
