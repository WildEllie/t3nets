"""
Local Conversation Store — SQLite.

For local development. No DynamoDB dependency.
Stores conversations in a local SQLite file.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.interfaces.conversation_store import ConversationStore


class SQLiteConversationStore(ConversationStore):
    """SQLite-backed conversation store for local development."""

    def __init__(self, db_path: str = "data/t3nets.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    tenant_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    messages TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, conversation_id)
                )
            """)

    async def get_conversation(
        self,
        tenant_id: str,
        conversation_id: str,
        max_turns: int = 20,
    ) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT messages FROM conversations WHERE tenant_id = ? AND conversation_id = ?",
                (tenant_id, conversation_id),
            ).fetchone()

        if not row:
            return []

        messages = json.loads(row[0])
        # Return last N turns (each turn = 2 messages: user + assistant)
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

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT messages FROM conversations WHERE tenant_id = ? AND conversation_id = ?",
                (tenant_id, conversation_id),
            ).fetchone()

            if row:
                messages = json.loads(row[0])
            else:
                messages = []

            messages.append({"role": "user", "content": user_message})
            messages.append({"role": "assistant", "content": assistant_message})

            conn.execute(
                """INSERT INTO conversations (tenant_id, conversation_id, messages, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(tenant_id, conversation_id)
                   DO UPDATE SET messages = ?, updated_at = ?""",
                (tenant_id, conversation_id, json.dumps(messages), now,
                 json.dumps(messages), now),
            )

    async def clear_conversation(
        self,
        tenant_id: str,
        conversation_id: str,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM conversations WHERE tenant_id = ? AND conversation_id = ?",
                (tenant_id, conversation_id),
            )
