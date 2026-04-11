"""
Re-export shim — canonical definitions live in t3nets_sdk.interfaces.blob_store.

Kept for backwards-compatible imports of the form:
    from agent.interfaces.blob_store import BlobStore, BlobNotFoundError
"""

from t3nets_sdk.interfaces.blob_store import BlobNotFoundError, BlobStore

__all__ = ["BlobStore", "BlobNotFoundError"]
