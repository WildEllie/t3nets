"""
SQLite-backed training store — local development implementation.

Logs Tier 2 (AI) routing decisions as training examples for future rule improvement.
"""

import sqlite3
from pathlib import Path

from agent.interfaces.training_store import TrainingStore
from agent.router.models import TrainingExample


class SQLiteTrainingStore(TrainingStore):
    """SQLite-backed training example storage for local development."""

    def __init__(self, db_path: str = "data/t3nets.db") -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS training_examples (
                    example_id              TEXT PRIMARY KEY,
                    tenant_id               TEXT NOT NULL,
                    message_text            TEXT NOT NULL,
                    timestamp               TEXT NOT NULL,
                    matched_skill           TEXT,
                    matched_action          TEXT,
                    was_disabled_skill      INTEGER NOT NULL DEFAULT 0,
                    confidence              REAL,
                    admin_override_skill    TEXT,
                    admin_override_action   TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_training_tenant "
                "ON training_examples (tenant_id, timestamp DESC)"
            )

    async def log_example(self, example: TrainingExample) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO training_examples (
                    example_id, tenant_id, message_text, timestamp,
                    matched_skill, matched_action, was_disabled_skill, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    example.example_id,
                    example.tenant_id,
                    example.message_text,
                    example.timestamp,
                    example.matched_skill,
                    example.matched_action,
                    1 if example.was_disabled_skill else 0,
                    example.confidence,
                ),
            )

    async def annotate_example(
        self,
        tenant_id: str,
        example_id: str,
        skill: str,
        action: str,
    ) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE training_examples
                SET admin_override_skill = ?, admin_override_action = ?
                WHERE example_id = ? AND tenant_id = ?
            """,
                (skill or None, action or None, example_id, tenant_id),
            )
        return cursor.rowcount > 0

    async def delete_example(self, tenant_id: str, example_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM training_examples WHERE example_id = ? AND tenant_id = ?",
                (example_id, tenant_id),
            )
        return cursor.rowcount > 0

    async def list_examples(
        self,
        tenant_id: str,
        limit: int = 100,
    ) -> list[TrainingExample]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT example_id, message_text, timestamp, matched_skill, matched_action,
                       was_disabled_skill, confidence, admin_override_skill, admin_override_action
                FROM training_examples
                WHERE tenant_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (tenant_id, limit),
            ).fetchall()

        return [
            TrainingExample(
                tenant_id=tenant_id,
                example_id=row[0],
                message_text=row[1],
                timestamp=row[2],
                matched_skill=row[3],
                matched_action=row[4],
                was_disabled_skill=bool(row[5]),
                confidence=row[6],
                admin_override_skill=row[7],
                admin_override_action=row[8],
            )
            for row in rows
        ]
