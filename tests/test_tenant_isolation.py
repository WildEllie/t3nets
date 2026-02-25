"""
Tenant isolation tests.

Verifies that multi-tenant data stays separated:
- Conversations from tenant A are not visible to tenant B
- Settings changes for tenant A don't affect tenant B
- Users are scoped to their own tenant
- Auth middleware correctly extracts identity from JWT
- Second tenant seed works and data is isolated
"""

import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.models.tenant import Tenant, TenantSettings, TenantUser
from adapters.local.sqlite_store import SQLiteConversationStore
from adapters.local.sqlite_tenant_store import SQLiteTenantStore

# Import auth_middleware directly by file path to avoid adapters.aws.__init__
# (which eagerly imports boto3 — not available in all test environments)
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "auth_middleware",
    str(Path(__file__).parent.parent / "adapters" / "aws" / "auth_middleware.py"),
)
_auth_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_auth_mod)
extract_auth = _auth_mod.extract_auth
AuthError = _auth_mod.AuthError
AuthContext = _auth_mod.AuthContext


# --- Helpers ---


def _make_tenant(tenant_id: str, name: str, skills: list[str] | None = None) -> Tenant:
    return Tenant(
        tenant_id=tenant_id,
        name=name,
        status="active",
        created_at="2025-01-01T00:00:00Z",
        settings=TenantSettings(enabled_skills=skills or ["ping"]),
    )


def _make_user(
    user_id: str,
    tenant_id: str,
    email: str = "user@example.com",
    role: str = "member",
    cognito_sub: str = "",
) -> TenantUser:
    return TenantUser(
        user_id=user_id,
        tenant_id=tenant_id,
        email=email,
        display_name=f"User {user_id}",
        role=role,
        cognito_sub=cognito_sub,
    )


def _make_jwt(sub: str, email: str = "test@example.com") -> str:
    """Create a mock JWT with sub + email (no tenant_id — IdP-agnostic)."""
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
    ).rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": sub, "email": email}).encode()
    ).rstrip(b"=")
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.{sig.decode()}"


class FakeHeaders:
    """Mock HTTP headers for auth tests."""

    def __init__(self, token: str | None = None):
        self._token = token

    def get(self, key, default=""):
        if key == "Authorization" and self._token:
            return f"Bearer {self._token}"
        return default


# --- Conversation Isolation ---


class TestConversationIsolation(unittest.TestCase):
    """Verify conversation data is isolated between tenants."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        db = str(Path(self.tmp.name) / "test.db")
        self.memory = SQLiteConversationStore(db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_conversations_isolated(self):
        """Messages from tenant A should not appear in tenant B's history."""
        asyncio.run(self.memory.save_turn("tenant-a", "conv-1", "hello from A", "reply to A"))
        asyncio.run(self.memory.save_turn("tenant-b", "conv-1", "hello from B", "reply to B"))

        history_a = asyncio.run(self.memory.get_conversation("tenant-a", "conv-1"))
        history_b = asyncio.run(self.memory.get_conversation("tenant-b", "conv-1"))

        self.assertEqual(len(history_a), 2)  # user + assistant
        self.assertEqual(len(history_b), 2)
        self.assertEqual(history_a[0]["content"], "hello from A")
        self.assertEqual(history_b[0]["content"], "hello from B")

    def test_clear_doesnt_affect_other_tenant(self):
        """Clearing tenant A's conversation shouldn't affect tenant B."""
        asyncio.run(self.memory.save_turn("tenant-a", "conv-1", "msg A", "reply A"))
        asyncio.run(self.memory.save_turn("tenant-b", "conv-1", "msg B", "reply B"))

        asyncio.run(self.memory.clear_conversation("tenant-a", "conv-1"))

        history_a = asyncio.run(self.memory.get_conversation("tenant-a", "conv-1"))
        history_b = asyncio.run(self.memory.get_conversation("tenant-b", "conv-1"))

        self.assertEqual(len(history_a), 0)
        self.assertEqual(len(history_b), 2)

    def test_same_conversation_id_different_tenants(self):
        """Same conversation_id in different tenants should hold independent data."""
        asyncio.run(
            self.memory.save_turn("alpha", "shared-conv", "alpha says hi", "alpha reply")
        )
        asyncio.run(
            self.memory.save_turn("beta", "shared-conv", "beta says hi", "beta reply")
        )
        asyncio.run(
            self.memory.save_turn("alpha", "shared-conv", "alpha second", "alpha second reply")
        )

        alpha_msgs = asyncio.run(self.memory.get_conversation("alpha", "shared-conv"))
        beta_msgs = asyncio.run(self.memory.get_conversation("beta", "shared-conv"))

        self.assertEqual(len(alpha_msgs), 4)  # 2 turns = 4 messages
        self.assertEqual(len(beta_msgs), 2)   # 1 turn = 2 messages

    def test_nonexistent_tenant_returns_empty(self):
        """Querying conversations for a non-existent tenant should return empty."""
        history = asyncio.run(self.memory.get_conversation("ghost-tenant", "conv-1"))
        self.assertEqual(history, [])


# --- Tenant Store Isolation ---


class TestTenantStoreIsolation(unittest.TestCase):
    """Verify tenant metadata and settings are isolated."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        db = str(Path(self.tmp.name) / "test.db")
        self.store = SQLiteTenantStore(db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tenants_independent_settings(self):
        """Two tenants should have independent settings."""
        tenant_a = _make_tenant("alpha", "Alpha Corp")
        tenant_b = _make_tenant("beta", "Beta Inc")

        asyncio.run(self.store.create_tenant(tenant_a))
        asyncio.run(self.store.create_tenant(tenant_b))

        # Change A's model
        tenant_a.settings.ai_model = "custom-model"
        asyncio.run(self.store.update_tenant(tenant_a))

        # B should be unaffected
        loaded_b = asyncio.run(self.store.get_tenant("beta"))
        self.assertEqual(loaded_b.settings.ai_model, "")
        self.assertEqual(loaded_b.name, "Beta Inc")

    def test_list_tenants(self):
        """list_tenants should return all tenants."""
        asyncio.run(self.store.create_tenant(_make_tenant("one", "Tenant One")))
        asyncio.run(self.store.create_tenant(_make_tenant("two", "Tenant Two")))

        all_tenants = asyncio.run(self.store.list_tenants())
        ids = {t.tenant_id for t in all_tenants}
        self.assertIn("one", ids)
        self.assertIn("two", ids)

    def test_tenant_not_found(self):
        """Getting a non-existent tenant should raise TenantNotFound."""
        from agent.interfaces.tenant_store import TenantNotFound

        with self.assertRaises(TenantNotFound):
            asyncio.run(self.store.get_tenant("does-not-exist"))

    def test_enabled_skills_isolated(self):
        """Each tenant can have different enabled skills."""
        asyncio.run(
            self.store.create_tenant(
                _make_tenant("eng", "Engineering", skills=["sprint_status", "release_notes"])
            )
        )
        asyncio.run(
            self.store.create_tenant(
                _make_tenant("sales", "Sales Team", skills=["ping"])
            )
        )

        eng = asyncio.run(self.store.get_tenant("eng"))
        sales = asyncio.run(self.store.get_tenant("sales"))

        self.assertEqual(eng.settings.enabled_skills, ["sprint_status", "release_notes"])
        self.assertEqual(sales.settings.enabled_skills, ["ping"])


# --- User Isolation ---


class TestUserIsolation(unittest.TestCase):
    """Verify user data is scoped to the correct tenant."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        db = str(Path(self.tmp.name) / "test.db")
        self.store = SQLiteTenantStore(db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_users_scoped_to_tenant(self):
        """Users in tenant A should not appear in tenant B's user list."""
        asyncio.run(self.store.create_tenant(_make_tenant("alpha", "Alpha")))
        asyncio.run(self.store.create_tenant(_make_tenant("beta", "Beta")))

        asyncio.run(self.store.create_user(_make_user("alice", "alpha", "alice@alpha.com")))
        asyncio.run(self.store.create_user(_make_user("bob", "beta", "bob@beta.com")))

        alpha_users = asyncio.run(self.store.list_users("alpha"))
        beta_users = asyncio.run(self.store.list_users("beta"))

        self.assertEqual(len(alpha_users), 1)
        self.assertEqual(alpha_users[0].email, "alice@alpha.com")
        self.assertEqual(len(beta_users), 1)
        self.assertEqual(beta_users[0].email, "bob@beta.com")

    def test_same_user_id_different_tenants(self):
        """Same user_id in different tenants should be independent."""
        asyncio.run(self.store.create_tenant(_make_tenant("alpha", "Alpha")))
        asyncio.run(self.store.create_tenant(_make_tenant("beta", "Beta")))

        asyncio.run(
            self.store.create_user(_make_user("admin", "alpha", "admin@alpha.com", role="admin"))
        )
        asyncio.run(
            self.store.create_user(_make_user("admin", "beta", "admin@beta.com", role="admin"))
        )

        admin_a = asyncio.run(self.store.get_user("alpha", "admin"))
        admin_b = asyncio.run(self.store.get_user("beta", "admin"))

        self.assertEqual(admin_a.email, "admin@alpha.com")
        self.assertEqual(admin_b.email, "admin@beta.com")

    def test_get_user_wrong_tenant_raises(self):
        """Looking up a user in the wrong tenant should raise UserNotFound."""
        from agent.interfaces.tenant_store import UserNotFound

        asyncio.run(self.store.create_tenant(_make_tenant("alpha", "Alpha")))
        asyncio.run(self.store.create_user(_make_user("alice", "alpha", "alice@alpha.com")))

        with self.assertRaises(UserNotFound):
            asyncio.run(self.store.get_user("beta", "alice"))

    def test_delete_user_doesnt_affect_other_tenant(self):
        """Deleting a user in tenant A should not affect the same user_id in tenant B."""
        asyncio.run(self.store.create_tenant(_make_tenant("alpha", "Alpha")))
        asyncio.run(self.store.create_tenant(_make_tenant("beta", "Beta")))

        asyncio.run(self.store.create_user(_make_user("admin", "alpha")))
        asyncio.run(self.store.create_user(_make_user("admin", "beta")))

        asyncio.run(self.store.delete_user("alpha", "admin"))

        # Alpha's admin is gone
        from agent.interfaces.tenant_store import UserNotFound

        with self.assertRaises(UserNotFound):
            asyncio.run(self.store.get_user("alpha", "admin"))

        # Beta's admin is untouched
        admin_b = asyncio.run(self.store.get_user("beta", "admin"))
        self.assertEqual(admin_b.tenant_id, "beta")

    def test_cognito_sub_cross_tenant_lookup(self):
        """get_user_by_cognito_sub should find user regardless of tenant."""
        asyncio.run(self.store.create_tenant(_make_tenant("alpha", "Alpha")))

        user = _make_user("alice", "alpha", "alice@alpha.com", cognito_sub="sub-abc-123")
        asyncio.run(self.store.create_user(user))

        found = asyncio.run(self.store.get_user_by_cognito_sub("sub-abc-123"))
        self.assertIsNotNone(found)
        self.assertEqual(found.tenant_id, "alpha")
        self.assertEqual(found.email, "alice@alpha.com")

    def test_cognito_sub_not_found(self):
        """get_user_by_cognito_sub should return None for unknown sub."""
        found = asyncio.run(self.store.get_user_by_cognito_sub("nonexistent-sub"))
        self.assertIsNone(found)

    def test_email_lookup_scoped_to_tenant(self):
        """get_user_by_email should only find users within the specified tenant."""
        asyncio.run(self.store.create_tenant(_make_tenant("alpha", "Alpha")))
        asyncio.run(self.store.create_tenant(_make_tenant("beta", "Beta")))

        asyncio.run(
            self.store.create_user(_make_user("alice", "alpha", "shared@example.com"))
        )
        asyncio.run(
            self.store.create_user(_make_user("bob", "beta", "shared@example.com"))
        )

        found_alpha = asyncio.run(self.store.get_user_by_email("alpha", "shared@example.com"))
        found_beta = asyncio.run(self.store.get_user_by_email("beta", "shared@example.com"))

        self.assertEqual(found_alpha.user_id, "alice")
        self.assertEqual(found_beta.user_id, "bob")


# --- Auth Middleware (IdP-Agnostic) ---


class TestAuthMiddleware(unittest.TestCase):
    """Verify JWT extraction — no tenant_id in AuthContext after Phase I."""

    def test_extract_auth_returns_sub_and_email(self):
        """extract_auth should correctly parse sub + email from JWT."""
        token = _make_jwt("user-123", "user@example.com")
        auth = extract_auth(FakeHeaders(token))

        self.assertEqual(auth.user_id, "user-123")
        self.assertEqual(auth.email, "user@example.com")

    def test_auth_context_has_no_tenant_id(self):
        """AuthContext should NOT have tenant_id — DynamoDB resolves it."""
        token = _make_jwt("user-456", "user@test.com")
        auth = extract_auth(FakeHeaders(token))

        self.assertFalse(hasattr(auth, "tenant_id"))

    def test_missing_bearer_raises(self):
        """extract_auth should raise AuthError with no Authorization header."""
        with self.assertRaises(AuthError):
            extract_auth(FakeHeaders(None))

    def test_missing_sub_raises(self):
        """extract_auth should raise AuthError when sub claim is missing."""
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "RS256"}).encode()
        ).rstrip(b"=")
        payload = base64.urlsafe_b64encode(
            json.dumps({"email": "nosub@test.com"}).encode()
        ).rstrip(b"=")
        sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=")
        token = f"{header.decode()}.{payload.decode()}.{sig.decode()}"

        with self.assertRaises(AuthError):
            extract_auth(FakeHeaders(token))

    def test_malformed_jwt_raises(self):
        """extract_auth should raise AuthError for a malformed JWT."""
        with self.assertRaises(AuthError):
            extract_auth(FakeHeaders("not.a.valid.jwt.at.all"))


# --- Second Tenant Seed ---


class TestSecondTenantSeed(unittest.TestCase):
    """Verify seed_default_tenant works for multiple tenants with full isolation."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        db = str(Path(self.tmp.name) / "test.db")
        self.store = SQLiteTenantStore(db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_two_tenants(self):
        """Seeding two tenants should create both with correct settings."""
        local = self.store.seed_default_tenant(
            tenant_id="local",
            name="Local Development",
            enabled_skills=["sprint_status", "release_notes", "ping"],
        )
        acme = self.store.seed_default_tenant(
            tenant_id="acme",
            name="Acme Corp",
            admin_email="admin@acme.dev",
            admin_name="Acme Admin",
            enabled_skills=["sprint_status", "ping"],
        )

        self.assertEqual(local.tenant_id, "local")
        self.assertEqual(acme.tenant_id, "acme")

        # Verify in DB
        all_tenants = asyncio.run(self.store.list_tenants())
        ids = {t.tenant_id for t in all_tenants}
        self.assertEqual(ids, {"local", "acme"})

    def test_seed_creates_admin_users(self):
        """Each seeded tenant should have its own admin user."""
        self.store.seed_default_tenant(
            tenant_id="local", name="Local Dev", admin_email="admin@local.dev"
        )
        self.store.seed_default_tenant(
            tenant_id="acme", name="Acme Corp", admin_email="admin@acme.dev",
            admin_name="Acme Admin",
        )

        local_admin = asyncio.run(self.store.get_user("local", "admin"))
        acme_admin = asyncio.run(self.store.get_user("acme", "admin"))

        self.assertEqual(local_admin.email, "admin@local.dev")
        self.assertEqual(local_admin.role, "admin")
        self.assertEqual(acme_admin.email, "admin@acme.dev")
        self.assertEqual(acme_admin.display_name, "Acme Admin")
        self.assertEqual(acme_admin.role, "admin")

    def test_seed_skills_independent(self):
        """Seeded tenants should have independent enabled_skills lists."""
        self.store.seed_default_tenant(
            tenant_id="local", name="Local",
            enabled_skills=["sprint_status", "release_notes", "ping"],
        )
        self.store.seed_default_tenant(
            tenant_id="acme", name="Acme",
            enabled_skills=["sprint_status", "ping"],
        )

        local = asyncio.run(self.store.get_tenant("local"))
        acme = asyncio.run(self.store.get_tenant("acme"))

        self.assertEqual(
            local.settings.enabled_skills,
            ["sprint_status", "release_notes", "ping"],
        )
        self.assertEqual(acme.settings.enabled_skills, ["sprint_status", "ping"])

    def test_seed_idempotent(self):
        """Calling seed_default_tenant twice should not duplicate data."""
        self.store.seed_default_tenant(tenant_id="local", name="Local Dev")
        self.store.seed_default_tenant(tenant_id="local", name="Local Dev")

        all_tenants = asyncio.run(self.store.list_tenants())
        self.assertEqual(len(all_tenants), 1)

        users = asyncio.run(self.store.list_users("local"))
        self.assertEqual(len(users), 1)

    def test_seed_updates_skills_on_restart(self):
        """Re-seeding with new skills list should update existing tenant."""
        self.store.seed_default_tenant(
            tenant_id="local", name="Local", enabled_skills=["ping"]
        )
        self.store.seed_default_tenant(
            tenant_id="local", name="Local", enabled_skills=["ping", "sprint_status"]
        )

        tenant = asyncio.run(self.store.get_tenant("local"))
        self.assertEqual(tenant.settings.enabled_skills, ["ping", "sprint_status"])


# --- Channel Mapping Isolation ---


class TestChannelMappingIsolation(unittest.TestCase):
    """Verify channel→tenant mappings work correctly across tenants."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        db = str(Path(self.tmp.name) / "test.db")
        self.store = SQLiteTenantStore(db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_different_channels_map_to_different_tenants(self):
        """Two channels should map to their respective tenants."""
        asyncio.run(self.store.create_tenant(_make_tenant("alpha", "Alpha")))
        asyncio.run(self.store.create_tenant(_make_tenant("beta", "Beta")))

        asyncio.run(self.store.set_channel_mapping("alpha", "teams", "team-alpha-id"))
        asyncio.run(self.store.set_channel_mapping("beta", "teams", "team-beta-id"))

        resolved_a = asyncio.run(self.store.get_by_channel_id("teams", "team-alpha-id"))
        resolved_b = asyncio.run(self.store.get_by_channel_id("teams", "team-beta-id"))

        self.assertEqual(resolved_a.tenant_id, "alpha")
        self.assertEqual(resolved_b.tenant_id, "beta")

    def test_unmapped_channel_raises(self):
        """Looking up an unmapped channel should raise TenantNotFound."""
        from agent.interfaces.tenant_store import TenantNotFound

        with self.assertRaises(TenantNotFound):
            asyncio.run(self.store.get_by_channel_id("teams", "nonexistent-channel"))


if __name__ == "__main__":
    unittest.main()
