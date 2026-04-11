"""
BlobStore tests.

Verifies:
- FileStore CRUD operations (get, put, delete, list_keys)
- Path traversal prevention
- BlobNotFoundError on missing keys
- JSON round-trip (put_json / get_json)
"""

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.local.file_blob_store import FileStore
from agent.interfaces.blob_store import BlobNotFoundError


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestFileStore(unittest.TestCase):
    """Tests for filesystem-backed BlobStore."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = FileStore(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_put_and_get(self):
        run(self.store.put("tenant1", "test.txt", b"hello world"))
        data = run(self.store.get("tenant1", "test.txt"))
        self.assertEqual(data, b"hello world")

    def test_get_not_found(self):
        with self.assertRaises(BlobNotFoundError):
            run(self.store.get("tenant1", "nonexistent.txt"))

    def test_put_json_get_json(self):
        run(self.store.put_json("tenant1", "config.json", {"key": "value", "num": 42}))
        data = run(self.store.get_json("tenant1", "config.json"))
        self.assertEqual(data["key"], "value")
        self.assertEqual(data["num"], 42)

    def test_delete(self):
        run(self.store.put("tenant1", "temp.bin", b"\x00\x01\x02"))
        run(self.store.delete("tenant1", "temp.bin"))
        with self.assertRaises(BlobNotFoundError):
            run(self.store.get("tenant1", "temp.bin"))

    def test_delete_nonexistent(self):
        # Should not raise
        run(self.store.delete("tenant1", "nope.txt"))

    def test_list_keys(self):
        run(self.store.put("tenant1", "a/1.txt", b"a"))
        run(self.store.put("tenant1", "a/2.txt", b"b"))
        run(self.store.put("tenant1", "b/3.txt", b"c"))
        keys = run(self.store.list_keys("tenant1"))
        self.assertEqual(sorted(keys), ["a/1.txt", "a/2.txt", "b/3.txt"])

    def test_list_keys_with_prefix(self):
        run(self.store.put("tenant1", "a/1.txt", b"a"))
        run(self.store.put("tenant1", "a/2.txt", b"b"))
        run(self.store.put("tenant1", "b/3.txt", b"c"))
        keys = run(self.store.list_keys("tenant1", prefix="a/"))
        self.assertEqual(sorted(keys), ["a/1.txt", "a/2.txt"])

    def test_list_keys_empty_tenant(self):
        keys = run(self.store.list_keys("nobody"))
        self.assertEqual(keys, [])

    def test_tenant_isolation(self):
        run(self.store.put("tenant1", "data.txt", b"tenant1-data"))
        run(self.store.put("tenant2", "data.txt", b"tenant2-data"))
        self.assertEqual(run(self.store.get("tenant1", "data.txt")), b"tenant1-data")
        self.assertEqual(run(self.store.get("tenant2", "data.txt")), b"tenant2-data")

    def test_path_traversal_prevention(self):
        run(self.store.put("tenant1", "../escape.txt", b"bad"))
        # Should not be able to escape the tenant directory
        path = self.store._path("tenant1", "../escape.txt")
        self.assertTrue(str(path).startswith(self.tmp))

    def test_nested_keys(self):
        run(self.store.put("tenant1", "practices/voiceher/assets/ref.wav", b"audio"))
        data = run(self.store.get("tenant1", "practices/voiceher/assets/ref.wav"))
        self.assertEqual(data, b"audio")


if __name__ == "__main__":
    unittest.main()
