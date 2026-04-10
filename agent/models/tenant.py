"""
Re-export shim — canonical definitions live in t3nets_sdk.models.tenant.

Kept for backwards-compatible imports of the form:
    from agent.models.tenant import Tenant, TenantUser, TenantSettings, Invitation
"""

from t3nets_sdk.models.tenant import Invitation, Tenant, TenantSettings, TenantUser

__all__ = [
    "Invitation",
    "Tenant",
    "TenantSettings",
    "TenantUser",
]
