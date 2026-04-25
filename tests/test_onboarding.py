"""
Tests for onboarding wizard backend endpoints.

Covers:
  - Tenant creation with admin user
  - Tenant ID validation
  - Tenant activation (onboarding → active)
  - Integration credential storage and testing
  - Admin API auth relaxation for onboarding
"""

import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock boto3 before importing adapters.aws (its __init__.py eagerly imports
# BedrockProvider which requires boto3 at import time).
if "boto3" not in sys.modules:
    sys.modules["boto3"] = MagicMock()
    sys.modules["botocore"] = MagicMock()
    sys.modules["botocore.exceptions"] = MagicMock()

from adapters.aws.admin_api import AdminAPI
from adapters.aws.auth_middleware import AuthError
from agent.models.tenant import Tenant, TenantUser

# --- Fixtures ---


class MockTenantStore:
    """In-memory tenant store for testing."""

    def __init__(self):
        self.tenants: dict[str, Tenant] = {}
        self.users: dict[str, list[TenantUser]] = {}

    async def get_tenant(self, tenant_id: str) -> Tenant:
        if tenant_id not in self.tenants:
            raise Exception(f"Tenant '{tenant_id}' not found")
        return self.tenants[tenant_id]

    async def create_tenant(self, tenant: Tenant) -> None:
        self.tenants[tenant.tenant_id] = tenant

    async def update_tenant(self, tenant: Tenant) -> None:
        self.tenants[tenant.tenant_id] = tenant

    async def list_tenants(self) -> list[Tenant]:
        return list(self.tenants.values())

    async def create_user(self, user: TenantUser) -> None:
        if user.tenant_id not in self.users:
            self.users[user.tenant_id] = []
        self.users[user.tenant_id].append(user)

    async def get_user(self, tenant_id: str, user_id: str) -> TenantUser:
        for u in self.users.get(tenant_id, []):
            if u.user_id == user_id:
                return u
        raise Exception("User not found")

    async def get_user_by_cognito_sub(self, cognito_sub: str):
        """Cross-tenant lookup by cognito_sub."""
        if not cognito_sub:
            return None
        for user_list in self.users.values():
            for u in user_list:
                if u.cognito_sub == cognito_sub:
                    return u
        return None

    async def list_users(self, tenant_id: str) -> list[TenantUser]:
        return self.users.get(tenant_id, [])


class MockSecretsProvider:
    """In-memory secrets store for testing."""

    def __init__(self):
        self.secrets: dict[str, dict] = {}

    async def get(self, tenant_id: str, integration_name: str) -> dict:
        key = f"{tenant_id}/{integration_name}"
        if key not in self.secrets:
            raise Exception(f"No secrets for {key}")
        return self.secrets[key]

    async def put(self, tenant_id: str, integration_name: str, secrets: dict) -> None:
        self.secrets[f"{tenant_id}/{integration_name}"] = secrets

    async def delete(self, tenant_id: str, integration_name: str) -> None:
        self.secrets.pop(f"{tenant_id}/{integration_name}", None)

    async def list_integrations(self, tenant_id: str) -> list[str]:
        prefix = f"{tenant_id}/"
        return [k.split("/")[1] for k in self.secrets if k.startswith(prefix)]


class MockSkills:
    def list_skill_names(self):
        return ["sprint_status", "release_notes"]


def make_admin_api():
    store = MockTenantStore()
    secrets = MockSecretsProvider()
    skills = MockSkills()
    api = AdminAPI(store, secrets, skills)
    return api, store, secrets


def make_auth_headers(sub="user-123", email="test@example.com"):
    """Create mock headers that extract_auth can parse."""
    import base64

    payload = {
        "sub": sub,
        "email": email,
    }
    # Create a fake JWT (header.payload.signature)
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    token = f"{header}.{body}.fake-sig"
    return {"Authorization": f"Bearer {token}"}


# --- Tests: Tenant Creation ---


class TestCreateTenant(unittest.IsolatedAsyncioTestCase):
    """Tests for POST /api/admin/tenants with admin user creation."""

    async def test_create_tenant_basic(self):
        api, store, _ = make_admin_api()
        headers = make_auth_headers()  # No tenant yet (onboarding)

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No tenant", 403)
            data, status = await api._create_tenant(
                {
                    "tenant_id": "acme-corp",
                    "name": "Acme Corporation",
                },
                headers,
            )

        assert status == 201
        assert data["created"] is True
        assert data["tenant_id"] == "acme-corp"
        assert "acme-corp" in store.tenants
        assert store.tenants["acme-corp"].name == "Acme Corporation"

    async def test_create_tenant_with_admin_user(self):
        api, store, _ = make_admin_api()
        headers = make_auth_headers()

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No tenant", 403)
            data, status = await api._create_tenant(
                {
                    "tenant_id": "acme-eng",
                    "name": "Acme Engineering",
                    "status": "onboarding",
                    "admin_user": {
                        "display_name": "Ellie P",
                        "email": "ellie@acme.com",
                        "cognito_sub": "sub-abc-123",
                    },
                },
                headers,
            )

        assert status == 201
        assert "acme-eng" in store.tenants
        assert store.tenants["acme-eng"].status == "onboarding"

        # Check admin user was created
        users = store.users.get("acme-eng", [])
        assert len(users) == 1
        assert users[0].email == "ellie@acme.com"
        assert users[0].display_name == "Ellie P"
        assert users[0].role == "admin"
        assert users[0].cognito_sub == "sub-abc-123"

    async def test_create_tenant_missing_fields(self):
        api, store, _ = make_admin_api()
        headers = make_auth_headers()

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No tenant", 403)
            data, status = await api._create_tenant({"tenant_id": ""}, headers)

        assert status == 400
        assert "required" in data["error"]

    async def test_create_tenant_invalid_id(self):
        api, store, _ = make_admin_api()
        headers = make_auth_headers()

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No tenant", 403)

            # Too short
            data, status = await api._create_tenant({"tenant_id": "ab", "name": "AB"}, headers)
            assert status == 400

            # Uppercase
            data, status = await api._create_tenant(
                {"tenant_id": "MyTeam", "name": "Team"}, headers
            )
            assert status == 400

            # Special characters
            data, status = await api._create_tenant(
                {"tenant_id": "my_team!", "name": "Team"}, headers
            )
            assert status == 400

    async def test_create_tenant_duplicate(self):
        api, store, _ = make_admin_api()
        headers = make_auth_headers()

        await store.create_tenant(
            Tenant(
                tenant_id="existing",
                name="Existing",
                status="active",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No tenant", 403)
            data, status = await api._create_tenant(
                {"tenant_id": "existing", "name": "New"}, headers
            )

        assert status == 409
        assert "already exists" in data["error"]

    async def test_create_tenant_default_skills(self):
        api, store, _ = make_admin_api()
        headers = make_auth_headers()

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No tenant", 403)
            await api._create_tenant({"tenant_id": "new-team", "name": "New Team"}, headers)

        tenant = store.tenants["new-team"]
        assert "sprint_status" in tenant.settings.enabled_skills
        assert "release_notes" in tenant.settings.enabled_skills


# --- Tests: Tenant Activation ---


class TestActivateTenant(unittest.IsolatedAsyncioTestCase):
    """Tests for PATCH /api/admin/tenants/{id}/activate."""

    async def test_activate_onboarding_tenant(self):
        api, store, _ = make_admin_api()

        await store.create_tenant(
            Tenant(
                tenant_id="test-team",
                name="Test Team",
                status="onboarding",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )

        data, status = await api._activate_tenant("test-team")
        assert status == 200
        assert data["activated"] is True
        assert store.tenants["test-team"].status == "active"

    async def test_activate_already_active(self):
        api, store, _ = make_admin_api()

        await store.create_tenant(
            Tenant(
                tenant_id="active-team",
                name="Active Team",
                status="active",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )

        data, status = await api._activate_tenant("active-team")
        assert status == 200
        assert "Already active" in data.get("message", "")

    async def test_activate_nonexistent(self):
        api, store, _ = make_admin_api()

        data, status = await api._activate_tenant("does-not-exist")
        assert status == 404


# --- Tests: Integration Storage ---


class TestIntegrationStorage(unittest.TestCase):
    """Tests for integration credential storage and testing."""

    def test_store_jira_credentials(self):
        import asyncio

        _, _, secrets = make_admin_api()

        asyncio.run(
            secrets.put(
                "test-tenant",
                "jira",
                {
                    "url": "https://acme.atlassian.net",
                    "email": "bot@acme.com",
                    "api_token": "secret",
                    "project_key": "NV",
                },
            )
        )

        stored = asyncio.run(secrets.get("test-tenant", "jira"))
        assert stored["url"] == "https://acme.atlassian.net"
        assert stored["project_key"] == "NV"

    def test_list_integrations(self):
        import asyncio

        _, _, secrets = make_admin_api()

        asyncio.run(secrets.put("t1", "jira", {"url": "https://a.atlassian.net"}))
        asyncio.run(secrets.put("t1", "github", {"token": "ghp_123"}))

        connected = asyncio.run(secrets.list_integrations("t1"))
        assert "jira" in connected
        assert "github" in connected
        assert len(connected) == 2


# --- Tests: Admin API Routing ---


class TestAdminAPIRouting(unittest.IsolatedAsyncioTestCase):
    """Tests for admin API request routing, especially onboarding relaxation."""

    async def test_create_tenant_bypasses_auth(self):
        """POST /api/admin/tenants should work without custom:tenant_id."""
        api, store, _ = make_admin_api()
        headers = make_auth_headers()

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            # Simulate user without tenant_id
            mock_auth.side_effect = AuthError("JWT missing 'custom:tenant_id' claim", 403)
            data, status = await api.handle_request(
                "POST",
                "/api/admin/tenants",
                headers,
                {
                    "tenant_id": "new-team",
                    "name": "New Team",
                },
            )

        assert status == 201

    async def test_other_routes_require_auth(self):
        """GET /api/admin/tenants should require full auth."""
        api, _, _ = make_admin_api()

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No auth", 401)
            data, status = await api.handle_request("GET", "/api/admin/tenants", {})

        assert status == 401

    async def test_patch_activate_route(self):
        """PATCH /api/admin/tenants/{id}/activate should route correctly."""
        api, store, _ = make_admin_api()

        await store.create_tenant(
            Tenant(
                tenant_id="my-team",
                name="My Team",
                status="onboarding",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )

        headers = make_auth_headers()
        data, status = await api.handle_request(
            "PATCH", "/api/admin/tenants/my-team/activate", headers
        )
        assert status == 200
        assert data["activated"] is True


# --- Tests: Tenant Model ---


class TestTenantModel(unittest.TestCase):
    """Tests for tenant model lifecycle."""

    def test_onboarding_status(self):
        tenant = Tenant(tenant_id="t1", name="Test", status="onboarding")
        assert not tenant.is_active()
        tenant.status = "active"
        assert tenant.is_active()

    def test_tenant_user_admin_role(self):
        user = TenantUser(
            user_id="u1",
            tenant_id="t1",
            email="admin@test.com",
            display_name="Admin",
            role="admin",
            cognito_sub="sub-123",
        )
        assert user.is_admin()
        assert user.cognito_sub == "sub-123"

    def test_tenant_user_member_role(self):
        user = TenantUser(
            user_id="u2",
            tenant_id="t1",
            email="member@test.com",
            display_name="Member",
        )
        assert not user.is_admin()
        assert user.role == "member"


# --- Tests: Cognito Sub Lookup (DynamoDB fallback) ---


class TestCognitoSubLookup(unittest.IsolatedAsyncioTestCase):
    """Tests for get_user_by_cognito_sub and cognito_sub persistence."""

    def test_cognito_sub_roundtrip(self):
        """cognito_sub should survive create → get."""
        _, store, _ = make_admin_api()
        user = TenantUser(
            user_id="u1",
            tenant_id="acme",
            email="alice@acme.com",
            display_name="Alice",
            role="admin",
            cognito_sub="sub-aaaa-1111",
        )
        asyncio.run(store.create_user(user))

        found = asyncio.run(store.get_user("acme", "u1"))
        assert found.cognito_sub == "sub-aaaa-1111"

    def test_get_user_by_cognito_sub_found(self):
        """Should find user across tenants by cognito_sub."""
        _, store, _ = make_admin_api()

        # Users in different tenants
        asyncio.run(
            store.create_user(
                TenantUser(
                    user_id="u1",
                    tenant_id="team-a",
                    email="alice@a.com",
                    display_name="Alice",
                    cognito_sub="sub-alice",
                )
            )
        )
        asyncio.run(
            store.create_user(
                TenantUser(
                    user_id="u2",
                    tenant_id="team-b",
                    email="bob@b.com",
                    display_name="Bob",
                    cognito_sub="sub-bob",
                )
            )
        )

        # Look up Bob by cognito_sub — should find him in team-b
        found = asyncio.run(store.get_user_by_cognito_sub("sub-bob"))
        assert found is not None
        assert found.tenant_id == "team-b"
        assert found.email == "bob@b.com"

    def test_get_user_by_cognito_sub_not_found(self):
        """Should return None for unknown cognito_sub."""
        _, store, _ = make_admin_api()

        found = asyncio.run(store.get_user_by_cognito_sub("sub-unknown"))
        assert found is None

    def test_get_user_by_cognito_sub_empty(self):
        """Should return None for empty cognito_sub."""
        _, store, _ = make_admin_api()

        found = asyncio.run(store.get_user_by_cognito_sub(""))
        assert found is None

    async def test_create_tenant_with_cognito_sub_then_lookup(self):
        """Full flow: create tenant + admin user, then look up by cognito_sub."""
        api, store, _ = make_admin_api()
        headers = make_auth_headers()

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No tenant", 403)
            data, status = await api._create_tenant(
                {
                    "tenant_id": "startup-xyz",
                    "name": "Startup XYZ",
                    "status": "onboarding",
                    "admin_user": {
                        "display_name": "Founder",
                        "email": "founder@startup.xyz",
                        "cognito_sub": "sub-founder-001",
                    },
                },
                headers,
            )

        assert status == 201

        # Now look up by cognito_sub — should resolve to startup-xyz
        found = await store.get_user_by_cognito_sub("sub-founder-001")
        assert found is not None
        assert found.tenant_id == "startup-xyz"
        assert found.email == "founder@startup.xyz"
        assert found.role == "admin"


class TestSQLiteCognitoSub(unittest.TestCase):
    """Tests for SQLite tenant store cognito_sub support."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")

        from adapters.local.sqlite_tenant_store import SQLiteTenantStore

        self.store = SQLiteTenantStore(db_path=self.db_path)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_user_with_cognito_sub(self):
        """SQLite store should persist cognito_sub."""
        user = TenantUser(
            user_id="u1",
            tenant_id="local",
            email="test@local.dev",
            display_name="Test",
            cognito_sub="sub-sqlite-test",
        )

        # Seed tenant first
        self.store.seed_default_tenant()
        asyncio.run(self.store.create_user(user))

        fetched = asyncio.run(self.store.get_user("local", "u1"))
        assert fetched.cognito_sub == "sub-sqlite-test"

    def test_get_user_by_cognito_sub_sqlite(self):
        """SQLite store should support cross-tenant cognito_sub lookup."""
        self.store.seed_default_tenant()

        user = TenantUser(
            user_id="u2",
            tenant_id="local",
            email="alice@local.dev",
            display_name="Alice",
            cognito_sub="sub-alice-sqlite",
        )
        asyncio.run(self.store.create_user(user))

        found = asyncio.run(self.store.get_user_by_cognito_sub("sub-alice-sqlite"))
        assert found is not None
        assert found.email == "alice@local.dev"
        assert found.tenant_id == "local"

    def test_get_user_by_cognito_sub_not_found_sqlite(self):
        """SQLite store returns None for unknown cognito_sub."""
        self.store.seed_default_tenant()

        found = asyncio.run(self.store.get_user_by_cognito_sub("sub-nonexistent"))
        assert found is None

    def test_update_user_cognito_sub_sqlite(self):
        """SQLite store should update cognito_sub."""
        self.store.seed_default_tenant()

        user = TenantUser(
            user_id="u3",
            tenant_id="local",
            email="bob@local.dev",
            display_name="Bob",
            cognito_sub="",
        )
        asyncio.run(self.store.create_user(user))

        # Update with cognito_sub
        user.cognito_sub = "sub-bob-updated"
        asyncio.run(self.store.update_user(user))

        fetched = asyncio.run(self.store.get_user("local", "u3"))
        assert fetched.cognito_sub == "sub-bob-updated"

    def test_avatar_url_roundtrip_sqlite(self):
        """SQLite store should persist avatar_url."""
        self.store.seed_default_tenant()

        user = TenantUser(
            user_id="u4",
            tenant_id="local",
            email="carol@local.dev",
            display_name="Carol",
            avatar_url="https://example.com/carol.png",
        )
        asyncio.run(self.store.create_user(user))

        fetched = asyncio.run(self.store.get_user("local", "u4"))
        assert fetched.avatar_url == "https://example.com/carol.png"

    def test_avatar_url_update_sqlite(self):
        """SQLite store should update avatar_url."""
        self.store.seed_default_tenant()

        user = TenantUser(
            user_id="u5",
            tenant_id="local",
            email="dave@local.dev",
            display_name="Dave",
            avatar_url="",
        )
        asyncio.run(self.store.create_user(user))

        user.avatar_url = "https://example.com/dave.jpg"
        asyncio.run(self.store.update_user(user))

        fetched = asyncio.run(self.store.get_user("local", "u5"))
        assert fetched.avatar_url == "https://example.com/dave.jpg"


# --- Tests: Auth Middleware (refactored) ---


class TestAuthMiddleware(unittest.TestCase):
    """Tests for the simplified auth middleware (no custom:tenant_id)."""

    def test_extract_auth_returns_sub_and_email(self):
        """extract_auth should return user_id and email from JWT."""
        from adapters.aws.auth_middleware import extract_auth

        headers = make_auth_headers(sub="sub-abc-123", email="user@test.com")
        ctx = extract_auth(headers)

        assert ctx.user_id == "sub-abc-123"
        assert ctx.email == "user@test.com"

    def test_extract_auth_no_tenant_id_field(self):
        """AuthContext should not have tenant_id attribute."""
        from adapters.aws.auth_middleware import extract_auth

        headers = make_auth_headers()
        ctx = extract_auth(headers)

        assert not hasattr(ctx, "tenant_id")

    def test_extract_auth_missing_bearer(self):
        """extract_auth should raise AuthError if no Bearer token."""
        from adapters.aws.auth_middleware import AuthError, extract_auth

        with self.assertRaises(AuthError):
            extract_auth({"Authorization": "Basic abc"})

    def test_extract_auth_missing_sub(self):
        """extract_auth should raise AuthError if JWT has no sub."""
        import base64

        from adapters.aws.auth_middleware import AuthError, extract_auth

        payload = {"email": "no-sub@test.com"}
        header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"{header}.{body}.fake-sig"

        with self.assertRaises(AuthError):
            extract_auth({"Authorization": f"Bearer {token}"})


# --- Tests: Avatar URL in Model ---


class TestAvatarUrl(unittest.IsolatedAsyncioTestCase):
    """Tests for avatar_url field on TenantUser."""

    def test_avatar_url_default_empty(self):
        user = TenantUser(
            user_id="u1",
            tenant_id="t1",
            email="a@b.com",
            display_name="Test",
        )
        assert user.avatar_url == ""

    def test_avatar_url_set(self):
        user = TenantUser(
            user_id="u1",
            tenant_id="t1",
            email="a@b.com",
            display_name="Test",
            avatar_url="https://img.example.com/avatar.png",
        )
        assert user.avatar_url == "https://img.example.com/avatar.png"

    async def test_create_tenant_with_avatar_url(self):
        """Admin API should accept avatar_url in admin_user."""
        api, store, _ = make_admin_api()
        headers = make_auth_headers()

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No tenant", 403)
            data, status = await api._create_tenant(
                {
                    "tenant_id": "avatar-team",
                    "name": "Avatar Team",
                    "admin_user": {
                        "display_name": "Admin",
                        "email": "admin@avatar.com",
                        "cognito_sub": "sub-avatar-001",
                        "avatar_url": "https://img.example.com/admin.png",
                    },
                },
                headers,
            )

        assert status == 201
        users = store.users.get("avatar-team", [])
        assert len(users) == 1
        assert users[0].avatar_url == "https://img.example.com/admin.png"
