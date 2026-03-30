"""
Tenant and User models.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


@dataclass
class TenantSettings:
    """Configurable settings per tenant."""

    # AI
    ai_provider: str = "bedrock"
    ai_model: str = ""  # set from BEDROCK_MODEL_ID at deploy time
    tier1_formatting_model: str = ""  # optional: free model for Tier 1 result formatting
    system_prompt_override: str = ""
    max_tokens_per_message: int = 4096

    # Channels
    enabled_channels: list[str] = field(default_factory=lambda: ["dashboard"])

    # Skills
    enabled_skills: list[str] = field(default_factory=list)
    custom_skills: list[str] = field(default_factory=list)

    # Practices
    primary_practice: str = ""  # Active practice name, e.g. "engineering"
    addon_skills: list[str] = field(default_factory=list)  # Extra skills from other practices
    addon_pages: list[str] = field(default_factory=list)  # "practice/page" format
    installed_practices: dict[str, str] = field(default_factory=dict)  # {"voiceher": "0.9.0"}

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
    cognito_sub: str = ""  # IdP subject ID (Cognito sub, Authentik uid, etc.)
    last_login: str = ""  # ISO 8601 timestamp of last login
    avatar_url: str = ""  # URL or data URI for user avatar
    channel_identities: dict[str, Any] = field(default_factory=dict)
    # e.g., {"teams": "aad-object-id", "whatsapp": "+1555...", "slack": "U12345"}

    def is_admin(self) -> bool:
        return self.role == "admin"

    def get_channel_identity(self, channel_type: str) -> Optional[str]:
        return self.channel_identities.get(channel_type)


@dataclass
class Invitation:
    """An invitation for a user to join an existing tenant."""

    invite_code: str  # "inv_" + 32 random URL-safe chars
    tenant_id: str
    email: str  # Must match signup email
    role: str = "member"  # member or admin
    status: str = "pending"  # pending | accepted | revoked
    invited_by: str = ""  # user_id of admin who created it
    created_at: str = ""
    expires_at: str = ""  # 14 days from creation
    accepted_at: str = ""

    def is_valid(self) -> bool:
        """Check if the invitation is still valid (pending and not expired)."""
        if self.status != "pending":
            return False
        if not self.expires_at:
            return False
        try:
            expiry = datetime.fromisoformat(self.expires_at)
            return datetime.now(timezone.utc) < expiry
        except (ValueError, TypeError):
            return False

    @staticmethod
    def generate_code() -> str:
        """Generate a new invite code."""
        import secrets

        return f"inv_{secrets.token_urlsafe(32)}"

    @staticmethod
    def default_expiry() -> str:
        """Return ISO 8601 timestamp 14 days from now."""
        return (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
