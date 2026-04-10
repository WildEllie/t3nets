"""
t3nets-sdk — public SDK for building t3nets practices.

Re-exports the stable, cloud-agnostic surface that practice authors code
against. Practice repos should depend on this package and nothing else
from t3nets.
"""

from t3nets_sdk.interfaces import (
    BlobNotFound,
    BlobStore,
    ConversationStore,
    EventBus,
    SecretNotFound,
    SecretsProvider,
)
from t3nets_sdk.models import (
    ChannelCapability,
    ChannelType,
    InboundMessage,
    OutboundMessage,
    RequestContext,
    Tenant,
    TenantSettings,
    TenantUser,
)

__all__ = [
    # Models
    "RequestContext",
    "Tenant",
    "TenantSettings",
    "TenantUser",
    "ChannelType",
    "ChannelCapability",
    "InboundMessage",
    "OutboundMessage",
    # Interfaces
    "BlobStore",
    "BlobNotFound",
    "ConversationStore",
    "EventBus",
    "SecretsProvider",
    "SecretNotFound",
]

__version__ = "0.1.0"
