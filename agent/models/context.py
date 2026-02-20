"""
Request Context — flows through the entire request pipeline.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent.models.tenant import Tenant, TenantUser
from agent.models.message import ChannelType


@dataclass
class RequestContext:
    """
    Immutable context for a single request.
    Created at the start of message handling, passed to every component.
    """

    tenant: Tenant
    user: TenantUser
    channel: ChannelType
    conversation_id: str
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def tenant_id(self) -> str:
        return self.tenant.tenant_id

    @property
    def user_id(self) -> str:
        return self.user.user_id

    def log_prefix(self) -> str:
        """For structured logging."""
        return f"[{self.tenant_id}:{self.user.display_name}:{self.channel.value}:{self.request_id[:8]}]"
