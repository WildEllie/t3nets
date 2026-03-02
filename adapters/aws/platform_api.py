"""
T3nets Platform API — Cross-tenant management endpoints.

These endpoints are restricted to admins of the 'default' tenant only.
Regular tenant admins cannot access these routes.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from agent.models.tenant import Invitation, Tenant, TenantSettings
from adapters.aws.auth_middleware import extract_auth, AuthError

logger = logging.getLogger("t3nets.platform")

DEFAULT_TENANT = "default"


class PlatformAPI:
    """Handles platform-level API requests (default-tenant admin only)."""

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
        """Route a platform API request. Returns (response_dict, status_code)."""
        try:
            import asyncio

            auth = extract_auth(headers)
            user = asyncio.run(self.tenants.get_user_by_cognito_sub(auth.user_id))
            if not user or user.tenant_id != DEFAULT_TENANT or user.role != "admin":
                return {"error": "Forbidden"}, 403

            if method == "GET" and path == "/api/platform/tenants":
                return self._list_tenants()
            elif method == "POST" and path == "/api/platform/tenants":
                return self._create_tenant(body or {}, headers)
            elif method == "PATCH" and path.startswith("/api/platform/tenants/"):
                parts = path.rstrip("/").split("/")
                if len(parts) >= 6:
                    tenant_id = parts[4]
                    action = parts[5]
                    if action == "suspend":
                        return self._suspend_tenant(tenant_id)
                    elif action == "activate":
                        return self._activate_tenant(tenant_id)
                return {"error": "Not found"}, 404
            elif method == "DELETE" and path.startswith("/api/platform/tenants/"):
                parts = path.rstrip("/").split("/")
                tenant_id = parts[4]
                return self._delete_tenant(tenant_id)
            else:
                return {"error": "Not found"}, 404

        except AuthError as e:
            return {"error": e.message}, e.status
        except Exception as e:
            logger.exception("Platform API error")
            return {"error": str(e)}, 500

    def _list_tenants(self) -> tuple[dict, int]:
        """List all tenants with user counts."""
        import asyncio

        tenant_list = asyncio.run(self.tenants.list_tenants())
        result = []
        for t in tenant_list:
            try:
                users = asyncio.run(self.tenants.list_users(t.tenant_id))
                user_count = len(users)
            except Exception:
                user_count = 0
            result.append({
                "tenant_id": t.tenant_id,
                "name": t.name,
                "status": t.status,
                "created_at": t.created_at,
                "user_count": user_count,
            })
        return {"tenants": result, "count": len(result)}, 200

    def _create_tenant(self, body: dict, headers: dict) -> tuple[dict, int]:
        """Create a new tenant and send an admin invitation."""
        import asyncio

        tenant_name = body.get("tenant_name", "").strip()
        admin_email = body.get("admin_email", "").strip().lower()
        admin_name = body.get("admin_name", "").strip()

        if not tenant_name or not admin_email or not admin_name:
            return {"error": "tenant_name, admin_email, and admin_name are required"}, 400

        # Slugify tenant name server-side
        slug = re.sub(r"[^a-z0-9-]+", "-", tenant_name.lower()).strip("-")
        if not slug or len(slug) < 2:
            return {"error": "Tenant name cannot be slugified to a valid ID"}, 400

        # Ensure uniqueness — try slug, then slug-2, slug-3, ...
        candidate = slug
        suffix = 2
        while True:
            try:
                asyncio.run(self.tenants.get_tenant(candidate))
                # Found — try next suffix
                candidate = f"{slug}-{suffix}"
                suffix += 1
            except Exception:
                break  # Not found — candidate is available

        tenant_id = candidate
        now = datetime.now(timezone.utc).isoformat()

        tenant = Tenant(
            tenant_id=tenant_id,
            name=tenant_name,
            status="active",
            created_at=now,
            settings=TenantSettings(
                enabled_skills=self.skills.list_skill_names(),
            ),
        )
        asyncio.run(self.tenants.create_tenant(tenant))
        logger.info(f"Platform: created tenant {tenant_id} ({tenant_name})")

        # Create invitation for the first admin
        invitation = Invitation(
            invite_code=Invitation.generate_code(),
            tenant_id=tenant_id,
            email=admin_email,
            role="admin",
            status="pending",
            invited_by=f"platform-admin",
            created_at=now,
            expires_at=Invitation.default_expiry(),
        )
        asyncio.run(self.tenants.create_invitation(invitation))
        logger.info(f"Platform: created admin invitation for {admin_email} → {tenant_id}")

        # Build invite URL from Host header
        host = headers.get("Host", "localhost") if isinstance(headers, dict) else (
            headers.get("Host") or "localhost"
        )
        scheme = "http" if "localhost" in host else "https"
        invite_url = f"{scheme}://{host}/login?invite={invitation.invite_code}"

        return {
            "tenant_id": tenant_id,
            "tenant_name": tenant_name,
            "invite_code": invitation.invite_code,
            "invite_url": invite_url,
            "admin_name": admin_name,
            "admin_email": admin_email,
        }, 201

    def _suspend_tenant(self, tenant_id: str) -> tuple[dict, int]:
        """Suspend a tenant (non-default only)."""
        import asyncio

        if tenant_id == DEFAULT_TENANT:
            return {"error": "Cannot suspend the default tenant"}, 400

        try:
            tenant = asyncio.run(self.tenants.get_tenant(tenant_id))
        except Exception:
            return {"error": f"Tenant '{tenant_id}' not found"}, 404

        tenant.status = "suspended"
        asyncio.run(self.tenants.update_tenant(tenant))
        logger.info(f"Platform: suspended tenant {tenant_id}")
        return {"tenant_id": tenant_id, "status": "suspended"}, 200

    def _activate_tenant(self, tenant_id: str) -> tuple[dict, int]:
        """Activate a suspended tenant."""
        import asyncio

        try:
            tenant = asyncio.run(self.tenants.get_tenant(tenant_id))
        except Exception:
            return {"error": f"Tenant '{tenant_id}' not found"}, 404

        tenant.status = "active"
        asyncio.run(self.tenants.update_tenant(tenant))
        logger.info(f"Platform: activated tenant {tenant_id}")
        return {"tenant_id": tenant_id, "status": "active"}, 200

    def _delete_tenant(self, tenant_id: str) -> tuple[dict, int]:
        """Tombstone-delete a tenant (non-default only)."""
        import asyncio

        if tenant_id == DEFAULT_TENANT:
            return {"error": "Cannot delete the default tenant"}, 400

        try:
            tenant = asyncio.run(self.tenants.get_tenant(tenant_id))
        except Exception:
            return {"error": f"Tenant '{tenant_id}' not found"}, 404

        tenant.status = "deleted"
        asyncio.run(self.tenants.update_tenant(tenant))
        logger.info(f"Platform: deleted (tombstoned) tenant {tenant_id}")
        return {"tenant_id": tenant_id, "status": "deleted"}, 200
