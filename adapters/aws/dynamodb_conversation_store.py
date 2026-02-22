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
import boto3
from datetime import datetime, timezone
from typing import Optional

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
    ) -> list[dict]:
        response = self.table.get_item(
            Key={"pk": tenant_id, "sk": conversation_id},
        )

        item = response.get("Item")
        if not item:
            return []

        messages = json.loads(item.get("messages", "[]"))
        return messages[-(max_turns * 2):]

    async def save_turn(
        self,
        tenant_id: str,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        metadata: Optional[dict] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        ttl = int(time.time()) + (self.ttl_days * 86400)

        # Get existing messages
        existing = await self.get_conversation(tenant_id, conversation_id, max_turns=100)
        user_msg: dict = {"role": "user", "content": user_message}
        if metadata and metadata.get("user_email"):
            user_msg["metadata"] = {"user_email": metadata["user_email"]}
        existing.append(user_msg)
        assistant_msg: dict = {"role": "assistant", "content": assistant_message}
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
