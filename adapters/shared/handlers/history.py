"""Conversation history handler.

Provides GET /api/history — returns conversation history for a given
tenant and conversation, along with platform/stage metadata.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from agent.interfaces.conversation_store import ConversationStore


class HistoryHandlers:
    """Shared handler for conversation history retrieval."""

    def __init__(self, conversation_store: ConversationStore) -> None:
        self._store = conversation_store

    async def get_history(
        self,
        request: Request,
        tenant_id: str,
        conversation_id: str,
    ) -> Response:
        """Return conversation history for the given tenant/conversation.

        The caller is responsible for authentication and for resolving
        ``tenant_id`` and ``conversation_id`` before invoking this method.
        Platform and stage metadata are read from query parameters so each
        server can pass its own values.

        Query params:
            platform: deployment platform identifier (e.g. "aws", "local")
            stage: deployment stage identifier (e.g. "dev", "prod")
        """
        try:
            history = await self._store.get_conversation(tenant_id, conversation_id)
            platform = request.query_params.get("platform", "")
            stage = request.query_params.get("stage", "")
            return JSONResponse(
                {
                    "messages": history,
                    "platform": platform,
                    "stage": stage,
                }
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
