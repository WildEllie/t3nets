"""
make_test_context — RequestContext builder for skill/router tests.

Defaults are deliberately boring so most tests can call `make_test_context()`
with no arguments. Override only the fields your test cares about.

Note: this builds a `RequestContext`, which is the *request-shaped* context
flowing through the router. The richer `SkillContext` (which bundles a
secrets/blob/event handle) lands in step 5 of the SDK rollout.
"""

from typing import Optional

from t3nets_sdk.models.context import RequestContext
from t3nets_sdk.models.message import ChannelType
from t3nets_sdk.models.tenant import Tenant, TenantSettings, TenantUser


def make_test_context(
    tenant_id: str = "test-tenant",
    user_id: str = "test-user",
    user_email: str = "test@example.com",
    user_display_name: str = "Test User",
    channel: ChannelType = ChannelType.DASHBOARD,
    conversation_id: str = "test-conversation",
    tenant: Optional[Tenant] = None,
    user: Optional[TenantUser] = None,
    settings: Optional[TenantSettings] = None,
) -> RequestContext:
    """
    Build a RequestContext for tests.

    Args:
        tenant_id: Used when constructing the default Tenant/TenantUser.
        user_id: Used for the default TenantUser.
        user_email: Email for the default TenantUser.
        user_display_name: Display name for the default TenantUser.
        channel: Channel type. Defaults to DASHBOARD.
        conversation_id: Conversation/thread ID for this request.
        tenant: Override the auto-built Tenant entirely.
        user: Override the auto-built TenantUser entirely.
        settings: Override the auto-built TenantSettings (ignored if `tenant`
            is also supplied).

    Returns:
        A fully-constructed RequestContext usable anywhere the platform
        accepts one.
    """
    if tenant is None:
        tenant = Tenant(
            tenant_id=tenant_id,
            name=f"Test tenant {tenant_id}",
            settings=settings or TenantSettings(),
        )
    if user is None:
        user = TenantUser(
            user_id=user_id,
            tenant_id=tenant.tenant_id,
            email=user_email,
            display_name=user_display_name,
        )
    return RequestContext(
        tenant=tenant,
        user=user,
        channel=channel,
        conversation_id=conversation_id,
    )
