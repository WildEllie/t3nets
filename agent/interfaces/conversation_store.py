"""
Re-export shim — canonical definitions live in t3nets_sdk.interfaces.conversation_store.

Kept for backwards-compatible imports of the form:
    from agent.interfaces.conversation_store import ConversationStore
"""

from t3nets_sdk.interfaces.conversation_store import ConversationStore

__all__ = ["ConversationStore"]
