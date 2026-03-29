"""
Local Pending Store — in-memory tracker for async skill requests.

For local development. Mimics the AWS PendingRequestsStore (DynamoDB)
but uses a simple dict with TTL expiry.

Used by the callback endpoint to match external service results
back to the original user/channel.
"""

import time
from typing import Any


class LocalPendingStore:
    """In-memory pending request tracker for local dev."""

    def __init__(self, ttl_seconds: int = 300):
        self._store: dict[str, dict[str, Any]] = {}
        self._ttl = ttl_seconds

    def create(self, request_id: str, **kwargs: Any) -> None:
        """Register a new pending request."""
        self._store[request_id] = {
            "status": "pending",
            "created_at": time.time(),
            **kwargs,
        }

    def get(self, request_id: str) -> dict[str, Any] | None:
        """Get a pending request, returning None if expired or not found."""
        entry = self._store.get(request_id)
        if not entry:
            return None
        if time.time() - entry["created_at"] > self._ttl:
            del self._store[request_id]
            return None
        return entry

    def mark_completed(self, request_id: str) -> bool:
        """
        Atomically mark a request as completed.
        Returns True if transitioned from pending → completed.
        Returns False if already completed or not found.
        """
        entry = self._store.get(request_id)
        if not entry or entry["status"] != "pending":
            return False
        entry["status"] = "completed"
        return True
