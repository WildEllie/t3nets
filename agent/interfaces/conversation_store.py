"""
Conversation Store Interface

Cloud-agnostic abstraction for conversation persistence.
Implementations: DynamoDBStore (AWS), SQLiteStore (local), etc.
"""

from abc import ABC, abstractmethod
from typing import Optional

# Avoid circular imports — use string type hints
# RequestContext is defined in agent.models.context


class ConversationStore(ABC):
    """
    Abstract base class for conversation storage.

    All operations are scoped to a tenant via the RequestContext.
    Implementations must ensure tenant isolation.
    """

    @abstractmethod
    async def get_conversation(
        self,
        tenant_id: str,
        conversation_id: str,
        max_turns: int = 20,
    ) -> list[dict]:
        """
        Retrieve conversation history.

        Args:
            tenant_id: Tenant scope
            conversation_id: Channel-specific conversation ID
            max_turns: Maximum number of turns to retrieve

        Returns:
            List of message dicts: [{"role": "user"/"assistant", "content": "..."}]
        """
        ...

    @abstractmethod
    async def save_turn(
        self,
        tenant_id: str,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        metadata: Optional[dict] = None,
    ) -> None:
        """
        Save a conversation turn (user message + assistant response).

        Args:
            tenant_id: Tenant scope
            conversation_id: Channel-specific conversation ID
            user_message: What the user said
            assistant_message: What the assistant replied
            metadata: Optional extra data (skill used, tokens, etc.)
        """
        ...

    @abstractmethod
    async def clear_conversation(
        self,
        tenant_id: str,
        conversation_id: str,
    ) -> None:
        """Delete conversation history."""
        ...
