"""
Callback endpoint tests.

Verifies:
- LocalPendingStore create/get/mark_completed lifecycle
- TTL expiry
- Idempotent completion (409 on double-complete)
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.local.local_pending_store import LocalPendingStore


class TestLocalPendingStore(unittest.TestCase):
    """Tests for the in-memory pending store."""

    def test_create_and_get(self):
        store = LocalPendingStore()
        store.create("req-001", user_key="user@test.com", skill_name="voice_say")
        entry = store.get("req-001")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["user_key"], "user@test.com")
        self.assertEqual(entry["skill_name"], "voice_say")

    def test_get_nonexistent(self):
        store = LocalPendingStore()
        self.assertIsNone(store.get("does-not-exist"))

    def test_mark_completed(self):
        store = LocalPendingStore()
        store.create("req-002", user_key="u@t.com")
        result = store.mark_completed("req-002")
        self.assertTrue(result)
        entry = store.get("req-002")
        self.assertEqual(entry["status"], "completed")

    def test_mark_completed_idempotent(self):
        """Second completion attempt returns False."""
        store = LocalPendingStore()
        store.create("req-003", user_key="u@t.com")
        self.assertTrue(store.mark_completed("req-003"))
        self.assertFalse(store.mark_completed("req-003"))

    def test_mark_completed_nonexistent(self):
        store = LocalPendingStore()
        self.assertFalse(store.mark_completed("nope"))

    def test_ttl_expiry(self):
        """Expired entries return None."""
        store = LocalPendingStore(ttl_seconds=1)
        store.create("req-004", user_key="u@t.com")
        # Manually backdate the creation time
        store._store["req-004"]["created_at"] = time.time() - 2
        self.assertIsNone(store.get("req-004"))

    def test_ttl_not_expired(self):
        """Fresh entries are returned normally."""
        store = LocalPendingStore(ttl_seconds=300)
        store.create("req-005", user_key="u@t.com")
        self.assertIsNotNone(store.get("req-005"))

    def test_multiple_requests(self):
        store = LocalPendingStore()
        store.create("a", user_key="u1")
        store.create("b", user_key="u2")
        store.create("c", user_key="u3")
        self.assertEqual(store.get("a")["user_key"], "u1")
        self.assertEqual(store.get("b")["user_key"], "u2")
        self.assertEqual(store.get("c")["user_key"], "u3")


if __name__ == "__main__":
    unittest.main()
