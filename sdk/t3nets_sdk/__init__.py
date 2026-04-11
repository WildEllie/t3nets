"""
t3nets-sdk — public SDK for building t3nets practices.

Re-exports the stable, cloud-agnostic surface that practice authors code
against. Practice repos should depend on this package and nothing else
from t3nets.
"""

from t3nets_sdk.contracts import (
    SkillContext,
    SkillResult,
    Worker,
)
from t3nets_sdk.interfaces import (
    BlobNotFoundError,
    BlobStore,
    ConversationStore,
    EventBus,
    SecretNotFoundError,
    SecretsProvider,
)
from t3nets_sdk.manifest import (
    ManifestError,
    PracticeManifest,
    PracticePageManifest,
    SkillManifest,
    parse_practice_yaml,
    parse_skill_yaml,
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
    # Contracts
    "SkillContext",
    "SkillResult",
    "Worker",
    # Interfaces
    "BlobStore",
    "BlobNotFoundError",
    "ConversationStore",
    "EventBus",
    "SecretsProvider",
    "SecretNotFoundError",
    # Manifest
    "PracticeManifest",
    "PracticePageManifest",
    "SkillManifest",
    "ManifestError",
    "parse_practice_yaml",
    "parse_skill_yaml",
]

__version__ = "0.1.0"
