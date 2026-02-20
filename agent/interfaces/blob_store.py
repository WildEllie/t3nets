"""
Blob Store Interface

Cloud-agnostic abstraction for object/file storage.
Implementations: S3Store (AWS), FileStore (local), etc.

Used for long-term memory, custom skills, exports, and audit logs.
"""

from abc import ABC, abstractmethod


class BlobStore(ABC):
    """
    Abstract base class for blob/object storage.

    All paths are tenant-scoped. Implementations must enforce isolation.
    """

    @abstractmethod
    async def get(self, tenant_id: str, key: str) -> bytes:
        """
        Retrieve an object.

        Args:
            tenant_id: Tenant scope
            key: Object key within tenant namespace (e.g., "memory/2026-02.json")

        Returns:
            Raw bytes of the object

        Raises:
            BlobNotFound: If the object doesn't exist
        """
        ...

    @abstractmethod
    async def get_json(self, tenant_id: str, key: str) -> dict:
        """Convenience: retrieve and parse JSON."""
        ...

    @abstractmethod
    async def put(self, tenant_id: str, key: str, data: bytes) -> None:
        """
        Store an object.

        Args:
            tenant_id: Tenant scope
            key: Object key within tenant namespace
            data: Raw bytes to store
        """
        ...

    @abstractmethod
    async def put_json(self, tenant_id: str, key: str, data: dict) -> None:
        """Convenience: serialize dict to JSON and store."""
        ...

    @abstractmethod
    async def delete(self, tenant_id: str, key: str) -> None:
        """Delete an object."""
        ...

    @abstractmethod
    async def list_keys(self, tenant_id: str, prefix: str = "") -> list[str]:
        """
        List object keys under a prefix.

        Args:
            tenant_id: Tenant scope
            prefix: Optional prefix filter (e.g., "memory/")

        Returns:
            List of keys (relative to tenant namespace)
        """
        ...


class BlobNotFound(Exception):
    """Raised when a requested blob doesn't exist."""
    pass
