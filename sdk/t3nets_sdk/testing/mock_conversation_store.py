"""
MockConversationStore — in-memory ConversationStore for tests.

Stores turns as `{"role": "user"|"assistant", "content": str}` dicts, the
same shape returned by the real implementations.
"""

import copy
from typing import Any, Optional

from t3nets_sdk.interfaces.conversation_store import ConversationStore


class MockConversationStore(ConversationStore):
    """In-memory ConversationStore. Use in tests instead of DynamoDB or SQLite."""

    def __init__(self) -> None:
        # (tenant_id, conversation_id) -> list of message dicts
        self._store: dict[tuple[str, str], list[dict[str, Any]]] = {}

    async def get_conversation(
        self,
        tenant_id: str,
        conversation_id: str,
        max_turns: int = 20,
    ) -> list[dict[str, Any]]:
        history = self._store.get((tenant_id, conversation_id), [])
        if max_turns <= 0:
            return []
        # A "turn" is a user+assistant pair → 2 messages.
        return copy.deepcopy(history[-(max_turns * 2) :])

    async def save_turn(
        self,
        tenant_id: str,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        history = self._store.setdefault((tenant_id, conversation_id), [])
        user_turn: dict[str, Any] = {"role": "user", "content": user_message}
        assistant_turn: dict[str, Any] = {"role": "assistant", "content": assistant_message}
        if metadata:
            assistant_turn["metadata"] = copy.deepcopy(metadata)
        history.append(user_turn)
        history.append(assistant_turn)

    async def clear_conversation(
        self,
        tenant_id: str,
        conversation_id: str,
    ) -> None:
        self._store.pop((tenant_id, conversation_id), None)

    # --- Test helpers ---

    def clear(self) -> None:
        """Drop every conversation across every tenant."""
        self._store.clear()
