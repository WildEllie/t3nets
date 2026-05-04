"""
T3nets Local Platform API — cross-tenant management for the local dev server.

Mirrors ``adapters/aws/platform_api.py`` for the SQLite-backed local stores
without authentication.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from agent.models.tenant import Invitation, Tenant, TenantSettings

logger = logging.getLogger("t3nets.local.platform")


class LocalPlatformAPI:
    """Cross-tenant management endpoints for the local dev server."""

    def __init__(self, tenants: Any, skills: Any) -> None:
        self.tenants = tenants
        self.skills = skills

    async def list_tenants(self, request: Request) -> Response:
        try:
            tenant_list = await self.tenants.list_tenants()
            result = []
            for t in tenant_list:
                try:
                    users = await self.tenants.list_users(t.tenant_id)
                    user_count = len(users)
                except Exception:
                    user_count = 0
                result.append(
                    {
                        "tenant_id": t.tenant_id,
                        "name": t.name,
                        "status": t.status,
                        "created_at": t.created_at,
                        "user_count": user_count,
                    }
                )
            return JSONResponse({"tenants": result, "count": len(result)})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def create_tenant(self, request: Request) -> Response:
        try:
            body = await request.json()
            tenant_name = body.get("tenant_name", "").strip()
            admin_email = body.get("admin_email", "").strip().lower()
            admin_name = body.get("admin_name", "").strip()

            if not tenant_name or not admin_email or not admin_name:
                return JSONResponse(
                    {"error": "tenant_name, admin_email, and admin_name are required"},
                    status_code=400,
                )

            slug = re.sub(r"[^a-z0-9-]+", "-", tenant_name.lower()).strip("-")
            if not slug or len(slug) < 2:
                return JSONResponse(
                    {"error": "Tenant name cannot be slugified to a valid ID"},
                    status_code=400,
                )

            candidate = slug
            suffix = 2
            while True:
                try:
                    await self.tenants.get_tenant(candidate)
                    candidate = f"{slug}-{suffix}"
                    suffix += 1
                except Exception:
                    break

            tenant_id = candidate
            now = datetime.now(timezone.utc).isoformat()
            tenant = Tenant(
                tenant_id=tenant_id,
                name=tenant_name,
                status="active",
                created_at=now,
                settings=TenantSettings(enabled_skills=self.skills.list_skill_names()),
            )
            await self.tenants.create_tenant(tenant)

            invitation = Invitation(
                invite_code=Invitation.generate_code(),
                tenant_id=tenant_id,
                email=admin_email,
                role="admin",
                status="pending",
                invited_by="platform-admin",
                created_at=now,
                expires_at=Invitation.default_expiry(),
            )
            await self.tenants.create_invitation(invitation)

            host = request.headers.get("host", "localhost:8080")
            scheme = "http" if "localhost" in host else "https"
            invite_url = f"{scheme}://{host}/login?invite={invitation.invite_code}"

            return JSONResponse(
                {
                    "tenant_id": tenant_id,
                    "tenant_name": tenant_name,
                    "invite_code": invitation.invite_code,
                    "invite_url": invite_url,
                    "admin_name": admin_name,
                    "admin_email": admin_email,
                },
                status_code=201,
            )
        except Exception as e:
            logger.exception("Platform create tenant error")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def tenant_detail(self, request: Request) -> Response:
        """Catch-all for /api/platform/tenants/{rest:path}."""
        rest = request.path_params["rest"]
        method = request.method
        path = f"/api/platform/tenants/{rest}"

        try:
            if method == "DELETE":
                parts = path.rstrip("/").split("/")
                tenant_id = parts[-1]
                if tenant_id == "default":
                    return JSONResponse(
                        {"error": "Cannot delete the default tenant"}, status_code=400
                    )
                tenant = await self.tenants.get_tenant(tenant_id)
                tenant.status = "deleted"
                await self.tenants.update_tenant(tenant)
                return JSONResponse({"tenant_id": tenant_id, "status": "deleted"})

            elif method == "PATCH":
                if path.endswith("/suspend"):
                    parts = path.rstrip("/").split("/")
                    tenant_id = parts[-2]
                    if tenant_id == "default":
                        return JSONResponse(
                            {"error": "Cannot suspend the default tenant"},
                            status_code=400,
                        )
                    tenant = await self.tenants.get_tenant(tenant_id)
                    tenant.status = "suspended"
                    await self.tenants.update_tenant(tenant)
                    return JSONResponse({"tenant_id": tenant_id, "status": "suspended"})
                elif path.endswith("/activate"):
                    parts = path.rstrip("/").split("/")
                    tenant_id = parts[-2]
                    tenant = await self.tenants.get_tenant(tenant_id)
                    tenant.status = "active"
                    await self.tenants.update_tenant(tenant)
                    return JSONResponse({"tenant_id": tenant_id, "status": "active"})

            return Response(status_code=404)

        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
