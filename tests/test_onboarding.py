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
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock boto3 before importing adapters.aws (its __init__.py eagerly imports
# BedrockProvider which requires boto3 at import time).
if "boto3" not in sys.modules:
    sys.modules["boto3"] = MagicMock()
    sys.modules["botocore"] = MagicMock()
    sys.modules["botocore.exceptions"] = MagicMock()

from agent.models.tenant import Tenant, TenantSettings, TenantUser
from adapters.aws.admin_api import AdminAPI
from adapters.aws.auth_middleware import AuthContext, AuthError


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
        raise Exception(f"User not found")

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


def make_auth_headers(tenant_id="test-tenant", sub="user-123", email="test@example.com"):
    """Create mock headers that extract_auth can parse."""
    import base64
    payload = {
        "sub": sub,
        "custom:tenant_id": tenant_id,
        "email": email,
    }
    # Create a fake JWT (header.payload.signature)
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    token = f"{header}.{body}.fake-sig"
    return {"Authorization": f"Bearer {token}"}


# --- Tests: Tenant Creation ---


class TestCreateTenant(unittest.TestCase):
    """Tests for POST /api/admin/tenants with admin user creation."""

    def test_create_tenant_basic(self):
        api, store, _ = make_admin_api()
        headers = make_auth_headers(tenant_id="")  # No tenant yet (onboarding)

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No tenant", 403)
            data, status = api._create_tenant({
                "tenant_id": "acme-corp",
                "name": "Acme Corporation",
            }, headers)

        assert status == 201
        assert data["created"] is True
        assert data["tenant_id"] == "acme-corp"
        assert "acme-corp" in store.tenants
        assert store.tenants["acme-corp"].name == "Acme Corporation"

    def test_create_tenant_with_admin_user(self):
        api, store, _ = make_admin_api()
        headers = make_auth_headers()

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No tenant", 403)
            data, status = api._create_tenant({
                "tenant_id": "acme-eng",
                "name": "Acme Engineering",
                "status": "onboarding",
                "admin_user": {
                    "display_name": "Ellie P",
                    "email": "ellie@acme.com",
                    "cognito_sub": "sub-abc-123",
                },
            }, headers)

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

    def test_create_tenant_missing_fields(self):
        api, store, _ = make_admin_api()
        headers = make_auth_headers()

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No tenant", 403)
            data, status = api._create_tenant({"tenant_id": ""}, headers)

        assert status == 400
        assert "required" in data["error"]

    def test_create_tenant_invalid_id(self):
        api, store, _ = make_admin_api()
        headers = make_auth_headers()

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No tenant", 403)

            # Too short
            data, status = api._create_tenant({"tenant_id": "ab", "name": "AB"}, headers)
            assert status == 400

            # Uppercase
            data, status = api._create_tenant({"tenant_id": "MyTeam", "name": "Team"}, headers)
            assert status == 400

            # Special characters
            data, status = api._create_tenant({"tenant_id": "my_team!", "name": "Team"}, headers)
            assert status == 400

    def test_create_tenant_duplicate(self):
        api, store, _ = make_admin_api()
        headers = make_auth_headers()

        # Pre-populate a tenant
        import asyncio
        asyncio.run(store.create_tenant(Tenant(
            tenant_id="existing",
            name="Existing",
            status="active",
            created_at=datetime.now(timezone.utc).isoformat(),
        )))

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No tenant", 403)
            data, status = api._create_tenant({"tenant_id": "existing", "name": "New"}, headers)

        assert status == 409
        assert "already exists" in data["error"]

    def test_create_tenant_default_skills(self):
        api, store, _ = make_admin_api()
        headers = make_auth_headers()

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No tenant", 403)
            api._create_tenant({"tenant_id": "new-team", "name": "New Team"}, headers)

        tenant = store.tenants["new-team"]
        assert "sprint_status" in tenant.settings.enabled_skills
        assert "release_notes" in tenant.settings.enabled_skills


# --- Tests: Tenant Activation ---


class TestActivateTenant(unittest.TestCase):
    """Tests for PATCH /api/admin/tenants/{id}/activate."""

    def test_activate_onboarding_tenant(self):
        api, store, _ = make_admin_api()
        import asyncio
        asyncio.run(store.create_tenant(Tenant(
            tenant_id="test-team",
            name="Test Team",
            status="onboarding",
            created_at=datetime.now(timezone.utc).isoformat(),
        )))

        data, status = api._activate_tenant("test-team")
        assert status == 200
        assert data["activated"] is True
        assert store.tenants["test-team"].status == "active"

    def test_activate_already_active(self):
        api, store, _ = make_admin_api()
        import asyncio
        asyncio.run(store.create_tenant(Tenant(
            tenant_id="active-team",
            name="Active Team",
            status="active",
            created_at=datetime.now(timezone.utc).isoformat(),
        )))

        data, status = api._activate_tenant("active-team")
        assert status == 200
        assert "Already active" in data.get("message", "")

    def test_activate_nonexistent(self):
        api, store, _ = make_admin_api()

        data, status = api._activate_tenant("does-not-exist")
        assert status == 404


# --- Tests: Integration Storage ---


class TestIntegrationStorage(unittest.TestCase):
    """Tests for integration credential storage and testing."""

    def test_store_jira_credentials(self):
        import asyncio
        _, _, secrets = make_admin_api()

        asyncio.run(secrets.put("test-tenant", "jira", {
            "url": "https://acme.atlassian.net",
            "email": "bot@acme.com",
            "api_token": "secret",
            "project_key": "NV",
        }))

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


class TestAdminAPIRouting(unittest.TestCase):
    """Tests for admin API request routing, especially onboarding relaxation."""

    def test_create_tenant_bypasses_auth(self):
        """POST /api/admin/tenants should work without custom:tenant_id."""
        api, store, _ = make_admin_api()
        headers = make_auth_headers(tenant_id="")

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            # Simulate user without tenant_id
            mock_auth.side_effect = AuthError("JWT missing 'custom:tenant_id' claim", 403)
            data, status = api.handle_request("POST", "/api/admin/tenants", headers, {
                "tenant_id": "new-team",
                "name": "New Team",
            })

        assert status == 201

    def test_other_routes_require_auth(self):
        """GET /api/admin/tenants should require full auth."""
        api, _, _ = make_admin_api()

        with patch("adapters.aws.admin_api.extract_auth") as mock_auth:
            mock_auth.side_effect = AuthError("No auth", 401)
            data, status = api.handle_request("GET", "/api/admin/tenants", {})

        assert status == 401

    def test_patch_activate_route(self):
        """PATCH /api/admin/tenants/{id}/activate should route correctly."""
        api, store, _ = make_admin_api()
        import asyncio
        asyncio.run(store.create_tenant(Tenant(
            tenant_id="my-team",
            name="My Team",
            status="onboarding",
            created_at=datetime.now(timezone.utc).isoformat(),
        )))

        headers = make_auth_headers(tenant_id="my-team")
        data, status = api.handle_request("PATCH", "/api/admin/tenants/my-team/activate", headers)
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
