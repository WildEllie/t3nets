"""
Local File Store — Filesystem-backed BlobStore.

For local development. Stores files under data/blobs/{tenant_id}/{key}.
"""

import json
from pathlib import Path
from typing import Any

from agent.interfaces.blob_store import BlobNotFoundError, BlobStore


class FileStore(BlobStore):
    """Filesystem-backed blob store for local development."""

    def __init__(self, base_dir: str = "data/blobs"):
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, tenant_id: str, key: str) -> Path:
        """Build a safe filesystem path from tenant and key."""
        safe_key = key.replace("..", "").lstrip("/")
        return self.base / tenant_id / safe_key

    async def get(self, tenant_id: str, key: str) -> bytes:
        p = self._path(tenant_id, key)
        if not p.exists():
            raise BlobNotFoundError(f"{tenant_id}/{key}")
        return p.read_bytes()

    async def get_json(self, tenant_id: str, key: str) -> dict[str, Any]:
        data = await self.get(tenant_id, key)
        return dict(json.loads(data.decode()))

    async def put(self, tenant_id: str, key: str, data: bytes) -> None:
        p = self._path(tenant_id, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    async def put_json(self, tenant_id: str, key: str, data: dict[str, Any]) -> None:
        await self.put(tenant_id, key, json.dumps(data, indent=2).encode())

    async def delete(self, tenant_id: str, key: str) -> None:
        p = self._path(tenant_id, key)
        if p.exists():
            p.unlink()

    async def list_keys(self, tenant_id: str, prefix: str = "") -> list[str]:
        root = self.base / tenant_id
        if not root.exists():
            return []
        results = []
        for p in root.rglob("*"):
            if p.is_file():
                rel = str(p.relative_to(root))
                if rel.startswith(prefix):
                    results.append(rel)
        return sorted(results)
