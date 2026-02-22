"""
Tenant and User models.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TenantSettings:
    """Configurable settings per tenant."""

    # AI
    ai_provider: str = "bedrock"
    ai_model: str = ""  # set from BEDROCK_MODEL_ID at deploy time
    system_prompt_override: str = ""
    max_tokens_per_message: int = 4096

    # Channels
    enabled_channels: list[str] = field(default_factory=lambda: ["dashboard"])

    # Skills
    enabled_skills: list[str] = field(default_factory=list)
    custom_skills: list[str] = field(default_factory=list)

    # Limits
    messages_per_day: int = 1000
    max_conversation_history: int = 20


@dataclass
class Tenant:
    """A team/organization using the platform."""

    tenant_id: str
    name: str
    status: str = "active"  # active, suspended, onboarding
    created_at: str = ""
    settings: TenantSettings = field(default_factory=TenantSettings)

    def is_active(self) -> bool:
        return self.status == "active"


@dataclass
class TenantUser:
    """An individual person within a tenant."""

    user_id: str
    tenant_id: str
    email: str
    display_name: str
    role: str = "member"  # admin, member
    channel_identities: dict = field(default_factory=dict)
    # e.g., {"teams": "aad-object-id", "whatsapp": "+1555...", "slack": "U12345"}

    def is_admin(self) -> bool:
        return self.role == "admin"

    def get_channel_identity(self, channel_type: str) -> Optional[str]:
        return self.channel_identities.get(channel_type)
