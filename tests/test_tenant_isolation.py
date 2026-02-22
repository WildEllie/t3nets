"""
Tenant isolation tests.

Verifies that multi-tenant data stays separated:
- Conversations from tenant A are not visible to tenant B
- Settings changes for tenant A don't affect tenant B
- Auth middleware correctly extracts tenant from JWT
"""

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.models.tenant import Tenant, TenantSettings
from adapters.local.sqlite_store import SQLiteConversationStore
from adapters.local.sqlite_tenant_store import SQLiteTenantStore
from adapters.aws.auth_middleware import extract_auth, AuthError, AuthContext


# --- Fixtures ---


@pytest.fixture
def memory(tmp_path):
    """Fresh SQLite conversation store."""
    db = str(tmp_path / "test.db")
    return SQLiteConversationStore(db)


@pytest.fixture
def tenant_store(tmp_path):
    """Fresh SQLite tenant store."""
    db = str(tmp_path / "test.db")
    return SQLiteTenantStore(db)


def _make_tenant(tenant_id: str, name: str) -> Tenant:
    return Tenant(
        tenant_id=tenant_id,
        name=name,
        status="active",
        created_at="2025-01-01T00:00:00Z",
        settings=TenantSettings(enabled_skills=["ping"]),
    )


def _make_jwt(sub: str, tenant_id: str, email: str = "test@example.com") -> str:
    """Create a mock JWT (no signature verification needed — API Gateway handles that)."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({
        "sub": sub,
        "custom:tenant_id": tenant_id,
        "email": email,
    }).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.{sig.decode()}"


# --- Conversation isolation ---


async def test_conversations_isolated(memory):
    """Messages from tenant A should not appear in tenant B's history."""
    await memory.save_turn("tenant-a", "conv-1", "hello from A", "reply to A")
    await memory.save_turn("tenant-b", "conv-1", "hello from B", "reply to B")

    history_a = await memory.get_conversation("tenant-a", "conv-1")
    history_b = await memory.get_conversation("tenant-b", "conv-1")

    # Each tenant should only see their own messages
    assert len(history_a) == 2  # user + assistant
    assert len(history_b) == 2
    assert history_a[0]["content"] == "hello from A"
    assert history_b[0]["content"] == "hello from B"


async def test_clear_doesnt_affect_other_tenant(memory):
    """Clearing tenant A's conversation shouldn't affect tenant B."""
    await memory.save_turn("tenant-a", "conv-1", "msg A", "reply A")
    await memory.save_turn("tenant-b", "conv-1", "msg B", "reply B")

    await memory.clear_conversation("tenant-a", "conv-1")

    history_a = await memory.get_conversation("tenant-a", "conv-1")
    history_b = await memory.get_conversation("tenant-b", "conv-1")

    assert len(history_a) == 0
    assert len(history_b) == 2


# --- Tenant store isolation ---


async def test_tenants_independent(tenant_store):
    """Two tenants should have independent settings."""
    tenant_a = _make_tenant("alpha", "Alpha Corp")
    tenant_b = _make_tenant("beta", "Beta Inc")

    await tenant_store.create_tenant(tenant_a)
    await tenant_store.create_tenant(tenant_b)

    # Change A's model
    tenant_a.settings.ai_model = "custom-model"
    await tenant_store.update_tenant(tenant_a)

    # B should be unaffected
    loaded_b = await tenant_store.get_tenant("beta")
    assert loaded_b.settings.ai_model == ""
    assert loaded_b.name == "Beta Inc"


async def test_list_tenants(tenant_store):
    """list_tenants should return all tenants."""
    await tenant_store.create_tenant(_make_tenant("one", "Tenant One"))
    await tenant_store.create_tenant(_make_tenant("two", "Tenant Two"))

    all_tenants = await tenant_store.list_tenants()
    ids = {t.tenant_id for t in all_tenants}
    assert "one" in ids
    assert "two" in ids


# --- Auth middleware ---


def test_extract_auth_valid_jwt():
    """extract_auth should correctly parse tenant_id from JWT."""
    token = _make_jwt("user-123", "my-tenant", "user@example.com")

    class FakeHeaders:
        def get(self, key, default=""):
            if key == "Authorization":
                return f"Bearer {token}"
            return default

    auth = extract_auth(FakeHeaders())
    assert auth.tenant_id == "my-tenant"
    assert auth.user_id == "user-123"
    assert auth.email == "user@example.com"


def test_extract_auth_missing_header():
    """extract_auth should raise AuthError with no Authorization header."""
    class FakeHeaders:
        def get(self, key, default=""):
            return default

    with pytest.raises(AuthError):
        extract_auth(FakeHeaders())


def test_extract_auth_missing_tenant():
    """extract_auth should raise AuthError when tenant_id claim is missing."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "user-1"}).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=")
    token = f"{header.decode()}.{payload.decode()}.{sig.decode()}"

    class FakeHeaders:
        def get(self, key, default=""):
            if key == "Authorization":
                return f"Bearer {token}"
            return default

    with pytest.raises(AuthError):
        extract_auth(FakeHeaders())
