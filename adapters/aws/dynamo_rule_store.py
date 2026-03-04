"""
DynamoDB-backed rule store — AWS implementation.

Schema (stored in the tenants table, single-table design):
  PK: TENANT#{tenant_id}
  SK: RULE_ENGINE
  {
    "version": 3,
    "generated_at": "2026-03-03T10:00:00Z",
    "generation_model": "claude-sonnet-4-5",
    "rules": { ...serialized TenantRuleSet... }
  }
"""

import json
from typing import Any, Optional

import boto3  # type: ignore[import-untyped]

from agent.interfaces.rule_store import RuleStore
from agent.router.models import SkillRules, TenantRuleSet


class DynamoDBRuleStore(RuleStore):
    """DynamoDB-backed rule set storage (stored in the tenants table)."""

    def __init__(self, table_name: str, region: str = "us-east-1") -> None:
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    async def save_rule_set(self, rule_set: TenantRuleSet) -> None:
        rules_data: dict[str, Any] = {
            "skill_rules": {
                name: {
                    "detection_patterns": sr.detection_patterns,
                    "action_rules": list(sr.action_rules),  # tuples → lists for DynamoDB
                    "disambiguation_notes": sr.disambiguation_notes,
                }
                for name, sr in rule_set.skill_rules.items()
            },
            "disabled_skill_catchers": rule_set.disabled_skill_catchers,
        }
        self.table.put_item(
            Item={
                "pk": f"TENANT#{rule_set.tenant_id}",
                "sk": "RULE_ENGINE",
                "tenant_id": rule_set.tenant_id,
                "version": rule_set.version,
                "generated_at": rule_set.generated_at,
                "generation_model": rule_set.generation_model,
                "rules": json.dumps(rules_data),
            }
        )

    async def load_rule_set(self, tenant_id: str) -> Optional[TenantRuleSet]:
        response = self.table.get_item(Key={"pk": f"TENANT#{tenant_id}", "sk": "RULE_ENGINE"})
        item = response.get("Item")
        if not item:
            return None

        data = json.loads(item["rules"])
        skill_rules: dict[str, SkillRules] = {}
        for name, r in data.get("skill_rules", {}).items():
            action_rules: list[tuple[str, str]] = [
                (pair[0], pair[1]) for pair in r.get("action_rules", [])
            ]
            skill_rules[name] = SkillRules(
                skill_name=name,
                detection_patterns=r.get("detection_patterns", []),
                action_rules=action_rules,
                disambiguation_notes=r.get("disambiguation_notes", ""),
            )

        return TenantRuleSet(
            tenant_id=tenant_id,
            version=int(item.get("version", 1)),
            generated_at=str(item.get("generated_at", "")),
            generation_model=str(item.get("generation_model", "")),
            skill_rules=skill_rules,
            disabled_skill_catchers=data.get("disabled_skill_catchers", {}),
        )
