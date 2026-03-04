"""
SQLite-backed rule store — local development implementation.

Stores AI-generated tenant rule sets in the existing t3nets SQLite database.
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

from agent.interfaces.rule_store import RuleStore
from agent.router.models import SkillRules, TenantRuleSet


class SQLiteRuleStore(RuleStore):
    """SQLite-backed rule set storage for local development."""

    def __init__(self, db_path: str = "data/t3nets.db") -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rule_sets (
                    tenant_id       TEXT PRIMARY KEY,
                    version         INTEGER NOT NULL,
                    generated_at    TEXT NOT NULL,
                    generation_model TEXT NOT NULL DEFAULT '',
                    rules           TEXT NOT NULL
                )
            """)

    async def save_rule_set(self, rule_set: TenantRuleSet) -> None:
        rules_json = json.dumps(
            {
                "skill_rules": {
                    name: {
                        "detection_patterns": sr.detection_patterns,
                        "action_rules": list(sr.action_rules),  # tuples → lists for JSON
                        "disambiguation_notes": sr.disambiguation_notes,
                    }
                    for name, sr in rule_set.skill_rules.items()
                },
                "disabled_skill_catchers": rule_set.disabled_skill_catchers,
            }
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO rule_sets
                    (tenant_id, version, generated_at, generation_model, rules)
                VALUES (?, ?, ?, ?, ?)
            """,
                (
                    rule_set.tenant_id,
                    rule_set.version,
                    rule_set.generated_at,
                    rule_set.generation_model,
                    rules_json,
                ),
            )

    async def load_rule_set(self, tenant_id: str) -> Optional[TenantRuleSet]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT version, generated_at, generation_model, rules "
                "FROM rule_sets WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()

        if not row:
            return None

        version, generated_at, generation_model, rules_json = row
        data = json.loads(rules_json)

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
            version=version,
            generated_at=generated_at,
            generation_model=generation_model,
            skill_rules=skill_rules,
            disabled_skill_catchers=data.get("disabled_skill_catchers", {}),
        )
