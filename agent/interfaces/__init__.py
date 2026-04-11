"""
T3nets Interfaces — Cloud-agnostic contracts.

All application code depends on these interfaces only.
Cloud-specific implementations live in adapters/.
"""

from agent.interfaces.ai_provider import AIProvider, AIResponse, ToolCall, ToolDefinition
from agent.interfaces.blob_store import BlobNotFoundError, BlobStore
from agent.interfaces.conversation_store import ConversationStore
from agent.interfaces.event_bus import EventBus
from agent.interfaces.secrets_provider import SecretNotFoundError, SecretsProvider
from agent.interfaces.tenant_store import TenantNotFoundError, TenantStore, UserNotFoundError

__all__ = [
    "AIProvider",
    "AIResponse",
    "ToolDefinition",
    "ToolCall",
    "BlobStore",
    "BlobNotFoundError",
    "ConversationStore",
    "EventBus",
    "SecretsProvider",
    "SecretNotFoundError",
    "TenantStore",
    "TenantNotFoundError",
    "UserNotFoundError",
]
