"""
MockSecretsProvider — in-memory SecretsProvider for tests.

Seedable from a constructor for ergonomic single-tenant tests:

    secrets = MockSecretsProvider({"jira": {"url": "...", "token": "..."}})
    await secrets.get("default", "jira")  # works

For multi-tenant tests, pass `tenant_id=` or call `put()` directly.
"""

import copy
from typing import Any, Optional

from t3nets_sdk.interfaces.secrets_provider import SecretNotFound, SecretsProvider


class MockSecretsProvider(SecretsProvider):
    """In-memory SecretsProvider. Use in tests to avoid touching real secret stores."""

    def __init__(
        self,
        secrets: Optional[dict[str, dict[str, Any]]] = None,
        tenant_id: str = "default",
    ) -> None:
        """
        Args:
            secrets: Optional seed mapping of `{integration_name: {key: value}}`.
                Stored under `tenant_id`. Defensively deep-copied so test
                fixtures aren't mutated by put/delete calls.
            tenant_id: Tenant scope for the seed dict. Defaults to "default".
        """
        # (tenant_id, integration_name) -> dict[str, Any]
        self._store: dict[tuple[str, str], dict[str, Any]] = {}
        if secrets:
            for name, payload in secrets.items():
                self._store[(tenant_id, name)] = copy.deepcopy(payload)

    async def get(self, tenant_id: str, integration_name: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self._store[(tenant_id, integration_name)])
        except KeyError as e:
            raise SecretNotFound(f"{tenant_id}/{integration_name}") from e

    async def put(
        self,
        tenant_id: str,
        integration_name: str,
        secrets: dict[str, Any],
    ) -> None:
        self._store[(tenant_id, integration_name)] = copy.deepcopy(secrets)

    async def delete(self, tenant_id: str, integration_name: str) -> None:
        self._store.pop((tenant_id, integration_name), None)

    async def list_integrations(self, tenant_id: str) -> list[str]:
        return sorted(name for (tid, name) in self._store if tid == tenant_id)

    # --- Test helpers ---

    def clear(self) -> None:
        """Reset all stored secrets across all tenants."""
        self._store.clear()
