"""
AWS Conversation Store — DynamoDB.

Schema:
  pk: {tenant_id}
  sk: {conversation_id}
  messages: JSON list of message dicts
  updated_at: ISO timestamp
  ttl: Unix timestamp (30 day expiry)
"""

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional, cast

import boto3  # type: ignore[import-untyped]

from agent.interfaces.conversation_store import ConversationStore


class DynamoDBConversationStore(ConversationStore):
    """DynamoDB-backed conversation store."""

    def __init__(self, table_name: str, region: str = "us-east-1"):
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self.ttl_days = 30

    async def get_conversation(
        self,
        tenant_id: str,
        conversation_id: str,
        max_turns: int = 20,
    ) -> list[dict[str, Any]]:
        response = self.table.get_item(
            Key={"pk": tenant_id, "sk": conversation_id},
        )

        item = response.get("Item")
        if not item:
            return []

        messages = cast(list[dict[str, Any]], json.loads(item.get("messages", "[]")))
        return messages[-(max_turns * 2) :]

    async def save_turn(
        self,
        tenant_id: str,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        ttl = int(time.time()) + (self.ttl_days * 86400)

        # Get existing messages
        existing = await self.get_conversation(tenant_id, conversation_id, max_turns=100)
        user_msg: dict[str, Any] = {"role": "user", "content": user_message}
        user_meta: dict[str, Any] = {}
        if metadata and metadata.get("user_email"):
            user_meta["user_email"] = metadata["user_email"]
        if metadata and metadata.get("timestamp"):
            user_meta["timestamp"] = metadata["timestamp"]
        if user_meta:
            user_msg["metadata"] = user_meta
        existing.append(user_msg)
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": assistant_message}
        if metadata:
            assistant_msg["metadata"] = metadata
        existing.append(assistant_msg)

        self.table.put_item(
            Item={
                "pk": tenant_id,
                "sk": conversation_id,
                "messages": json.dumps(existing),
                "updated_at": now,
                "ttl": ttl,
            }
        )

    async def clear_conversation(
        self,
        tenant_id: str,
        conversation_id: str,
    ) -> None:
        self.table.delete_item(
            Key={"pk": tenant_id, "sk": conversation_id},
        )
