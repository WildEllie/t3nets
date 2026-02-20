"""
T3nets Interfaces — Cloud-agnostic contracts.

All application code depends on these interfaces only.
Cloud-specific implementations live in adapters/.
"""

from agent.interfaces.ai_provider import AIProvider, AIResponse, ToolDefinition, ToolCall
from agent.interfaces.conversation_store import ConversationStore
from agent.interfaces.event_bus import EventBus
from agent.interfaces.secrets_provider import SecretsProvider, SecretNotFound
from agent.interfaces.blob_store import BlobStore, BlobNotFound
from agent.interfaces.tenant_store import TenantStore, TenantNotFound, UserNotFound

__all__ = [
    "AIProvider", "AIResponse", "ToolDefinition", "ToolCall",
    "ConversationStore",
    "EventBus",
    "SecretsProvider", "SecretNotFound",
    "BlobStore", "BlobNotFound",
    "TenantStore", "TenantNotFound", "UserNotFound",
]
