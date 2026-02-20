"""
Tenant Store Interface

Cloud-agnostic abstraction for tenant and user management.
Implementations: DynamoDBTenantStore (AWS), SQLiteTenantStore (local), etc.
"""

from abc import ABC, abstractmethod
from typing import Optional
from agent.models.tenant import Tenant, TenantUser


class TenantStore(ABC):
    """
    Abstract base class for tenant data management.
    """

    # --- Tenant operations ---

    @abstractmethod
    async def get_tenant(self, tenant_id: str) -> Tenant:
        """Get a tenant by ID. Raises TenantNotFound if missing."""
        ...

    @abstractmethod
    async def create_tenant(self, tenant: Tenant) -> None:
        """Create a new tenant."""
        ...

    @abstractmethod
    async def update_tenant(self, tenant: Tenant) -> None:
        """Update an existing tenant."""
        ...

    @abstractmethod
    async def list_tenants(self) -> list[Tenant]:
        """List all tenants. Platform admin only."""
        ...

    # --- Tenant resolution (from channel-specific IDs) ---

    @abstractmethod
    async def get_by_channel_id(
        self,
        channel_type: str,
        channel_specific_id: str,
    ) -> Tenant:
        """
        Resolve a tenant from a channel-specific identifier.

        Examples:
            get_by_channel_id("teams", "azure-bot-app-id")
            get_by_channel_id("slack", "workspace-id")
            get_by_channel_id("whatsapp", "+15551234567")

        Raises TenantNotFound if no mapping exists.
        """
        ...

    @abstractmethod
    async def set_channel_mapping(
        self,
        tenant_id: str,
        channel_type: str,
        channel_specific_id: str,
    ) -> None:
        """Register a channel-to-tenant mapping."""
        ...

    # --- User operations ---

    @abstractmethod
    async def get_user(
        self,
        tenant_id: str,
        user_id: str,
    ) -> TenantUser:
        """Get a user within a tenant. Raises UserNotFound if missing."""
        ...

    @abstractmethod
    async def get_user_by_email(
        self,
        tenant_id: str,
        email: str,
    ) -> Optional[TenantUser]:
        """Find a user by email within a tenant. Returns None if not found."""
        ...

    @abstractmethod
    async def get_user_by_channel_identity(
        self,
        tenant_id: str,
        channel_type: str,
        channel_user_id: str,
    ) -> Optional[TenantUser]:
        """
        Find a user by their channel-specific identity.

        Example: get_user_by_channel_identity("outlocks", "teams", "aad-object-id")
        """
        ...

    @abstractmethod
    async def create_user(self, user: TenantUser) -> None:
        """Create a new user within a tenant."""
        ...

    @abstractmethod
    async def update_user(self, user: TenantUser) -> None:
        """Update an existing user."""
        ...

    @abstractmethod
    async def delete_user(self, tenant_id: str, user_id: str) -> None:
        """Remove a user from a tenant."""
        ...

    @abstractmethod
    async def list_users(self, tenant_id: str) -> list[TenantUser]:
        """List all users in a tenant."""
        ...


class TenantNotFound(Exception):
    pass


class UserNotFound(Exception):
    pass
