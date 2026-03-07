"""
DynamoDB-backed training store — AWS implementation.

Schema (stored in the tenants table, single-table design):
  PK: TENANT#{tenant_id}
  SK: TRAINING#{timestamp}#{example_id}

Sorted by SK so list_examples() can use a range query without a scan.
"""

import asyncio
from typing import Optional

import boto3  # type: ignore[import-untyped]
from boto3.dynamodb.conditions import Key  # type: ignore[import-untyped]

from agent.interfaces.training_store import TrainingStore
from agent.router.models import TrainingExample


class DynamoDBTrainingStore(TrainingStore):
    """DynamoDB-backed training example storage (stored in the tenants table)."""

    def __init__(self, table_name: str, region: str = "us-east-1") -> None:
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    async def log_example(self, example: TrainingExample) -> None:
        sk = f"TRAINING#{example.timestamp}#{example.example_id}"
        item = {
            "pk": f"TENANT#{example.tenant_id}",
            "sk": sk,
            "example_id": example.example_id,
            "tenant_id": example.tenant_id,
            "message_text": example.message_text,
            "timestamp": example.timestamp,
            "was_disabled_skill": example.was_disabled_skill,
        }
        if example.matched_skill is not None:
            item["matched_skill"] = example.matched_skill
        if example.matched_action is not None:
            item["matched_action"] = example.matched_action
        if example.confidence is not None:
            item["confidence"] = str(example.confidence)  # DynamoDB decimal-safe

        await asyncio.to_thread(
            self.table.put_item,
            Item=item,
        )

    def _find_sk(self, tenant_id: str, example_id: str) -> Optional[str]:
        """Query for the SK of a specific example_id (needed for update/delete)."""
        response = self.table.query(
            KeyConditionExpression=(
                Key("pk").eq(f"TENANT#{tenant_id}") & Key("sk").begins_with("TRAINING#")
            ),
            FilterExpression="example_id = :eid",
            ExpressionAttributeValues={":eid": example_id},
            ProjectionExpression="sk",
        )
        items = response.get("Items", [])
        if not items:
            return None
        return str(items[0]["sk"])

    async def annotate_example(
        self,
        tenant_id: str,
        example_id: str,
        skill: str,
        action: str,
    ) -> bool:
        sk = self._find_sk(tenant_id, example_id)
        if not sk:
            return False
        update_expr = "SET admin_override_skill = :s, admin_override_action = :a"
        self.table.update_item(
            Key={"pk": f"TENANT#{tenant_id}", "sk": sk},
            UpdateExpression=update_expr,
            ExpressionAttributeValues={":s": skill or "", ":a": action or ""},
        )
        return True

    async def delete_example(self, tenant_id: str, example_id: str) -> bool:
        sk = self._find_sk(tenant_id, example_id)
        if not sk:
            return False
        self.table.delete_item(Key={"pk": f"TENANT#{tenant_id}", "sk": sk})
        return True

    def _list_examples_sync(self, tenant_id: str, limit: int) -> list[TrainingExample]:
        """Synchronous DynamoDB query — call via asyncio.to_thread from async contexts."""
        response = self.table.query(
            KeyConditionExpression=(
                Key("pk").eq(f"TENANT#{tenant_id}") & Key("sk").begins_with("TRAINING#")
            ),
            ScanIndexForward=False,  # newest first
            Limit=limit,
        )
        examples: list[TrainingExample] = []
        for item in response.get("Items", []):
            confidence_raw = item.get("confidence")
            confidence: Optional[float] = float(confidence_raw) if confidence_raw else None
            examples.append(
                TrainingExample(
                    tenant_id=tenant_id,
                    example_id=str(item.get("example_id", "")),
                    message_text=str(item.get("message_text", "")),
                    timestamp=str(item.get("timestamp", "")),
                    matched_skill=item.get("matched_skill"),
                    matched_action=item.get("matched_action"),
                    was_disabled_skill=bool(item.get("was_disabled_skill", False)),
                    confidence=confidence,
                    admin_override_skill=item.get("admin_override_skill") or None,
                    admin_override_action=item.get("admin_override_action") or None,
                )
            )
        return examples

    async def list_examples(
        self,
        tenant_id: str,
        limit: int = 100,
    ) -> list[TrainingExample]:
        return await asyncio.to_thread(self._list_examples_sync, tenant_id, limit)
