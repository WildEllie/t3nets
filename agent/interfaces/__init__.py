"""
T3nets Interfaces — Cloud-agnostic contracts.

All application code depends on these interfaces only.
Cloud-specific implementations live in adapters/.
"""

from agent.interfaces.ai_provider import AIProvider, AIResponse, ToolCall, ToolDefinition
from agent.interfaces.blob_store import BlobNotFound, BlobStore
from agent.interfaces.conversation_store import ConversationStore
from agent.interfaces.event_bus import EventBus
from agent.interfaces.secrets_provider import SecretNotFound, SecretsProvider
from agent.interfaces.tenant_store import TenantNotFound, TenantStore, UserNotFound

__all__ = [
    "AIProvider",
    "AIResponse",
    "ToolDefinition",
    "ToolCall",
    "BlobStore",
    "BlobNotFound",
    "ConversationStore",
    "EventBus",
    "SecretsProvider",
    "SecretNotFound",
    "TenantStore",
    "TenantNotFound",
    "UserNotFound",
]
