"""
Secrets Provider Interface

Cloud-agnostic abstraction for secrets management.
Implementations: SecretsManagerProvider (AWS), EnvSecretsProvider (local), etc.
"""

from abc import ABC, abstractmethod
from typing import Optional


class SecretsProvider(ABC):
    """
    Abstract base class for secrets retrieval.

    Secrets are stored per-tenant at paths like:
        /t3nets/tenants/{tenant_id}/{integration_name}

    Implementations must enforce tenant isolation.
    """

    @abstractmethod
    async def get(self, tenant_id: str, integration_name: str) -> dict:
        """
        Retrieve secrets for a tenant's integration.

        Args:
            tenant_id: Tenant scope
            integration_name: e.g., "jira", "github", "teams"

        Returns:
            Dict of secret key-value pairs
            e.g., {"url": "...", "email": "...", "api_token": "..."}

        Raises:
            SecretNotFound: If no secrets exist for this tenant/integration
        """
        ...

    @abstractmethod
    async def put(
        self,
        tenant_id: str,
        integration_name: str,
        secrets: dict,
    ) -> None:
        """
        Store or update secrets for a tenant's integration.

        Args:
            tenant_id: Tenant scope
            integration_name: e.g., "jira", "github"
            secrets: Key-value pairs to store
        """
        ...

    @abstractmethod
    async def delete(self, tenant_id: str, integration_name: str) -> None:
        """Remove secrets for a tenant's integration."""
        ...

    @abstractmethod
    async def list_integrations(self, tenant_id: str) -> list[str]:
        """List all integration names that have stored secrets for a tenant."""
        ...


class SecretNotFound(Exception):
    """Raised when requested secrets don't exist."""
    pass
