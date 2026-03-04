"""
DynamoDB-backed training store — AWS implementation.

Schema (stored in the tenants table, single-table design):
  PK: TENANT#{tenant_id}
  SK: TRAINING#{timestamp}#{example_id}

Sorted by SK so list_examples() can use a range query without a scan.
"""

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

        self.table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(pk) OR attribute_not_exists(sk)",
        )

    async def list_examples(
        self,
        tenant_id: str,
        limit: int = 100,
    ) -> list[TrainingExample]:
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
                )
            )
        return examples
