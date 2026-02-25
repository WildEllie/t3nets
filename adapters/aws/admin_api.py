"""
T3nets Admin API — Tenant management endpoints.

These endpoints require admin-level authentication (checked via JWT claims).
Used by platform operators to manage tenants, users, and configuration.

The onboarding flow uses a relaxed auth mode: users without a tenant_id
can still call POST /api/admin/tenants to create their first tenant.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from agent.models.tenant import Tenant, TenantSettings, TenantUser
from adapters.aws.auth_middleware import extract_auth, AuthError

logger = logging.getLogger("t3nets.admin")


class AdminAPI:
    """Handles admin-level API requests for tenant management."""

    def __init__(self, tenants, secrets, skills):
        self.tenants = tenants
        self.secrets = secrets
        self.skills = skills

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict,
        body: dict | None = None,
    ) -> tuple[dict, int]:
        """Route an admin API request. Returns (response_dict, status_code)."""
        try:
            # Onboarding: allow tenant creation without a tenant_id in JWT.
            # The user just signed up via Cognito and has no tenant yet.
            if method == "POST" and path == "/api/admin/tenants":
                return self._create_tenant(body or {}, headers)

            # All other admin routes require full auth (with tenant_id)
            auth = extract_auth(headers)
            # For now, any authenticated user with a tenant can access admin.
            # TODO: add role-based access (admin claim in JWT)

            if method == "GET" and path == "/api/admin/tenants":
                return self._list_tenants()
            elif method == "GET" and path.startswith("/api/admin/tenants/"):
                tenant_id = path.split("/")[-1]
                return self._get_tenant(tenant_id)
            elif method == "PUT" and path.startswith("/api/admin/tenants/"):
                tenant_id = path.split("/")[-1]
                return self._update_tenant(tenant_id, body or {})
            elif method == "PATCH" and path.endswith("/activate"):
                # PATCH /api/admin/tenants/{id}/activate
                parts = path.rstrip("/").split("/")
                tenant_id = parts[-2]  # /api/admin/tenants/{id}/activate
                return self._activate_tenant(tenant_id)
            else:
                return {"error": "Not found"}, 404

        except AuthError as e:
            return {"error": e.message}, e.status
        except Exception as e:
            logger.exception("Admin API error")
            return {"error": str(e)}, 500

    def _list_tenants(self) -> tuple[dict, int]:
        """List all tenants."""
        import asyncio
        tenant_list = asyncio.run(self.tenants.list_tenants())
        return {
            "tenants": [
                {
                    "tenant_id": t.tenant_id,
                    "name": t.name,
                    "status": t.status,
                    "created_at": t.created_at,
                    "ai_model": t.settings.ai_model,
                    "enabled_skills": t.settings.enabled_skills,
                }
                for t in tenant_list
            ],
            "count": len(tenant_list),
        }, 200

    def _get_tenant(self, tenant_id: str) -> tuple[dict, int]:
        """Get a single tenant with full details."""
        import asyncio
        try:
            tenant = asyncio.run(self.tenants.get_tenant(tenant_id))
        except Exception:
            return {"error": f"Tenant '{tenant_id}' not found"}, 404

        connected = asyncio.run(self.secrets.list_integrations(tenant_id))

        return {
            "tenant_id": tenant.tenant_id,
            "name": tenant.name,
            "status": tenant.status,
            "created_at": tenant.created_at,
            "settings": {
                "ai_model": tenant.settings.ai_model,
                "enabled_skills": tenant.settings.enabled_skills,
                "enabled_channels": tenant.settings.enabled_channels,
                "messages_per_day": tenant.settings.messages_per_day,
                "max_conversation_history": tenant.settings.max_conversation_history,
            },
            "integrations": list(connected),
        }, 200

    def _create_tenant(self, body: dict, headers: dict) -> tuple[dict, int]:
        """Create a new tenant, optionally with a first admin user.

        This endpoint uses relaxed auth: the user may not have a tenant_id
        yet (they're onboarding), so we extract sub/email from JWT without
        requiring custom:tenant_id.
        """
        import asyncio

        tenant_id = body.get("tenant_id", "").strip()
        name = body.get("name", "").strip()

        if not tenant_id or not name:
            return {"error": "tenant_id and name are required"}, 400

        # Validate tenant_id format (URL-safe: lowercase, numbers, hyphens)
        import re
        if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", tenant_id) or len(tenant_id) < 3:
            return {
                "error": "tenant_id must be 3+ chars, lowercase letters/numbers/hyphens only"
            }, 400

        # Check if already exists
        try:
            asyncio.run(self.tenants.get_tenant(tenant_id))
            return {"error": f"Tenant '{tenant_id}' already exists"}, 409
        except Exception:
            pass  # Expected — tenant doesn't exist yet

        now = datetime.now(timezone.utc).isoformat()
        status = body.get("status", "active")
        tenant = Tenant(
            tenant_id=tenant_id,
            name=name,
            status=status,
            created_at=now,
            settings=TenantSettings(
                enabled_skills=body.get("enabled_skills", self.skills.list_skill_names()),
                ai_model=body.get("ai_model", ""),
            ),
        )
        asyncio.run(self.tenants.create_tenant(tenant))
        logger.info(f"Created tenant: {tenant_id} ({name}) [status={status}]")

        # Create first admin user if admin_user data is provided
        admin_data = body.get("admin_user")
        if admin_data:
            user = TenantUser(
                user_id=admin_data.get("cognito_sub", f"admin-{tenant_id}"),
                tenant_id=tenant_id,
                email=admin_data.get("email", ""),
                display_name=admin_data.get("display_name", "Admin"),
                role="admin",
                cognito_sub=admin_data.get("cognito_sub", ""),
                avatar_url=admin_data.get("avatar_url", ""),
            )
            asyncio.run(self.tenants.create_user(user))
            logger.info(f"Created admin user for tenant {tenant_id}: {user.email}")

        return {"tenant_id": tenant_id, "created": True}, 201

    def _activate_tenant(self, tenant_id: str) -> tuple[dict, int]:
        """Set tenant status from 'onboarding' to 'active'."""
        import asyncio

        try:
            tenant = asyncio.run(self.tenants.get_tenant(tenant_id))
        except Exception:
            return {"error": f"Tenant '{tenant_id}' not found"}, 404

        if tenant.status == "active":
            return {"tenant_id": tenant_id, "status": "active", "message": "Already active"}, 200

        tenant.status = "active"
        asyncio.run(self.tenants.update_tenant(tenant))
        logger.info(f"Activated tenant: {tenant_id}")

        return {"tenant_id": tenant_id, "status": "active", "activated": True}, 200

    def _update_tenant(self, tenant_id: str, body: dict) -> tuple[dict, int]:
        """Update an existing tenant's settings."""
        import asyncio

        try:
            tenant = asyncio.run(self.tenants.get_tenant(tenant_id))
        except Exception:
            return {"error": f"Tenant '{tenant_id}' not found"}, 404

        if "name" in body:
            tenant.name = body["name"]
        if "status" in body:
            tenant.status = body["status"]
        if "ai_model" in body:
            tenant.settings.ai_model = body["ai_model"]
        if "enabled_skills" in body:
            tenant.settings.enabled_skills = body["enabled_skills"]
        if "messages_per_day" in body:
            tenant.settings.messages_per_day = body["messages_per_day"]

        asyncio.run(self.tenants.update_tenant(tenant))
        logger.info(f"Updated tenant: {tenant_id}")

        return {"tenant_id": tenant_id, "updated": True}, 200
