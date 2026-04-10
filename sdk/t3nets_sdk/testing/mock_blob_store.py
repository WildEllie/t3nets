"""
MockBlobStore — in-memory BlobStore for tests.

Tenant-isolated. Round-trips bytes and JSON. Mirrors the semantics of the
real S3 / FileStore implementations: missing keys raise BlobNotFound.
"""

import json
from typing import Any

from t3nets_sdk.interfaces.blob_store import BlobNotFound, BlobStore


class MockBlobStore(BlobStore):
    """In-memory BlobStore. Use in tests to avoid touching S3 or the filesystem."""

    def __init__(self) -> None:
        # (tenant_id, key) -> bytes
        self._store: dict[tuple[str, str], bytes] = {}

    async def get(self, tenant_id: str, key: str) -> bytes:
        try:
            return self._store[(tenant_id, key)]
        except KeyError as e:
            raise BlobNotFound(f"{tenant_id}/{key}") from e

    async def get_json(self, tenant_id: str, key: str) -> dict[str, Any]:
        raw = await self.get(tenant_id, key)
        result: dict[str, Any] = json.loads(raw)
        return result

    async def put(self, tenant_id: str, key: str, data: bytes) -> None:
        self._store[(tenant_id, key)] = data

    async def put_json(self, tenant_id: str, key: str, data: dict[str, Any]) -> None:
        await self.put(tenant_id, key, json.dumps(data).encode("utf-8"))

    async def delete(self, tenant_id: str, key: str) -> None:
        self._store.pop((tenant_id, key), None)

    async def list_keys(self, tenant_id: str, prefix: str = "") -> list[str]:
        return sorted(
            key for (tid, key) in self._store if tid == tenant_id and key.startswith(prefix)
        )

    # --- Test helpers (not part of the BlobStore interface) ---

    def clear(self) -> None:
        """Reset all stored blobs across all tenants."""
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, item: tuple[str, str]) -> bool:
        return item in self._store
