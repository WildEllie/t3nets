"""
T3nets Local Admin API — tenant/user/invitation management for the local dev server.

Mirrors the surface of ``adapters/aws/admin_api.py`` but with no authentication
checks: this only runs against the local SQLite-backed stores and is not exposed
to untrusted callers.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from agent.models.tenant import Invitation, Tenant, TenantSettings, TenantUser

logger = logging.getLogger("t3nets.local.admin")


class LocalAdminAPI:
    """Tenant management endpoints for the local dev server."""

    def __init__(self, tenants: Any, skills: Any) -> None:
        self.tenants = tenants
        self.skills = skills

    async def create_tenant(self, request: Request) -> Response:
        """POST /api/admin/tenants -- create a tenant."""
        try:
            body = await request.json()
            tenant_id = body.get("tenant_id", "").strip()
            name = body.get("name", "").strip()
            if not tenant_id or not name:
                return JSONResponse({"error": "tenant_id and name are required"}, status_code=400)

            now = datetime.now(timezone.utc).isoformat()
            status = body.get("status", "active")
            tenant = Tenant(
                tenant_id=tenant_id,
                name=name,
                status=status,
                created_at=now,
                settings=TenantSettings(enabled_skills=self.skills.list_skill_names()),
            )
            await self.tenants.create_tenant(tenant)
            logger.info(f"Created tenant: {tenant_id} ({name})")

            admin_data = body.get("admin_user")
            if admin_data:
                user = TenantUser(
                    user_id=admin_data.get("cognito_sub", f"admin-{tenant_id}"),
                    tenant_id=tenant_id,
                    email=admin_data.get("email", "admin@local.dev"),
                    display_name=admin_data.get("display_name", "Admin"),
                    role="admin",
                )
                await self.tenants.create_user(user)

            return JSONResponse({"tenant_id": tenant_id, "created": True}, status_code=201)
        except Exception as e:
            logger.exception("Create tenant error")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def tenant_detail(self, request: Request) -> Response:
        """Catch-all for /api/admin/tenants/{rest:path}."""
        rest = request.path_params["rest"]
        method = request.method
        path = f"/api/admin/tenants/{rest}"

        try:
            if method == "GET":
                if "/invitations" in path:
                    return await self._list_invitations(path)
                elif "/users" in path:
                    return await self._list_users(path)
                return Response(status_code=404)

            elif method == "POST":
                if "/invitations" in path:
                    body = await request.json()
                    return await self._create_invitation(request, path, body)
                return Response(status_code=404)

            elif method == "PUT":
                body = await request.json()
                return await self._update_tenant(path, body)

            elif method == "PATCH":
                if path.endswith("/activate"):
                    return await self._activate_tenant(path)
                if re.search(r"/users/[^/]+$", path):
                    body = await request.json()
                    return await self._update_user(path, body)
                return Response(status_code=404)

            elif method == "DELETE":
                if "/invitations/" in path:
                    return await self._revoke_invitation(path)
                return Response(status_code=404)

            return Response(status_code=405)

        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _list_invitations(self, path: str) -> Response:
        parts = path.rstrip("/").split("/")
        tenant_id = parts[4]
        invitations = await self.tenants.list_invitations(tenant_id)
        return JSONResponse(
            {
                "invitations": [
                    {
                        "invite_code": inv.invite_code,
                        "email": inv.email,
                        "role": inv.role,
                        "status": inv.status,
                        "created_at": inv.created_at,
                        "expires_at": inv.expires_at,
                    }
                    for inv in invitations
                ],
                "count": len(invitations),
            }
        )

    async def _list_users(self, path: str) -> Response:
        parts = path.rstrip("/").split("/")
        tenant_id = parts[4]
        users = await self.tenants.list_users(tenant_id)
        return JSONResponse(
            {
                "users": [
                    {
                        "user_id": u.user_id,
                        "email": u.email,
                        "display_name": u.display_name,
                        "role": u.role,
                        "last_login": u.last_login,
                    }
                    for u in users
                ],
                "count": len(users),
            }
        )

    async def _create_invitation(
        self, request: Request, path: str, body: dict[str, Any]
    ) -> Response:
        parts = path.rstrip("/").split("/")
        tenant_id = parts[4]
        email = body.get("email", "").strip().lower()
        role = body.get("role", "member")

        if not email:
            return JSONResponse({"error": "email is required"}, status_code=400)
        if role not in ("member", "admin"):
            return JSONResponse({"error": "role must be 'member' or 'admin'"}, status_code=400)

        try:
            await self.tenants.get_tenant(tenant_id)
        except Exception:
            return JSONResponse({"error": f"Tenant '{tenant_id}' not found"}, status_code=404)

        existing = await self.tenants.get_user_by_email(tenant_id, email)
        if existing:
            return JSONResponse({"error": f"{email} is already a member"}, status_code=409)

        now = datetime.now(timezone.utc).isoformat()
        invitation = Invitation(
            invite_code=Invitation.generate_code(),
            tenant_id=tenant_id,
            email=email,
            role=role,
            status="pending",
            invited_by="local-admin",
            created_at=now,
            expires_at=Invitation.default_expiry(),
        )
        await self.tenants.create_invitation(invitation)

        host = request.headers.get("host", "localhost:8080")
        scheme = "http" if "localhost" in host else "https"
        invite_url = f"{scheme}://{host}/login?invite={invitation.invite_code}"

        return JSONResponse(
            {
                "invite_code": invitation.invite_code,
                "invite_url": invite_url,
                "email": email,
                "role": role,
                "expires_at": invitation.expires_at,
            },
            status_code=201,
        )

    async def _update_tenant(self, path: str, body: dict[str, Any]) -> Response:
        tenant_id = path.split("/")[-1]
        tenant = await self.tenants.get_tenant(tenant_id)
        if "name" in body:
            tenant.name = body["name"]
        if "status" in body:
            tenant.status = body["status"]
        if "ai_model" in body:
            tenant.settings.ai_model = body["ai_model"]
        await self.tenants.update_tenant(tenant)
        return JSONResponse({"tenant_id": tenant_id, "updated": True})

    async def _activate_tenant(self, path: str) -> Response:
        parts = path.rstrip("/").split("/")
        tenant_id = parts[-2]
        tenant = await self.tenants.get_tenant(tenant_id)
        tenant.status = "active"
        await self.tenants.update_tenant(tenant)
        return JSONResponse({"tenant_id": tenant_id, "status": "active", "activated": True})

    async def _revoke_invitation(self, path: str) -> Response:
        parts = path.rstrip("/").split("/")
        invite_code = parts[-1]
        invitation = await self.tenants.get_invitation(invite_code)
        if not invitation:
            return JSONResponse({"error": "Invitation not found"}, status_code=404)
        invitation.status = "revoked"
        await self.tenants.update_invitation(invitation)
        return JSONResponse({"revoked": True, "invite_code": invite_code})

    async def _update_user(self, path: str, body: dict[str, Any]) -> Response:
        """PATCH /api/admin/tenants/{tid}/users/{uid} — update user fields."""
        from agent.interfaces.tenant_store import UserNotFoundError

        parts = path.rstrip("/").split("/")
        # path: /api/admin/tenants/{tid}/users/{uid}  →  parts[4]=tid, parts[6]=uid
        tenant_id = parts[4]
        user_id = parts[6]

        try:
            user = await self.tenants.get_user(tenant_id, user_id)
        except UserNotFoundError:
            return JSONResponse({"error": "User not found"}, status_code=404)

        if "display_name" in body:
            user.display_name = str(body["display_name"])

        if "role" in body:
            role = body["role"]
            if role not in ("admin", "member"):
                return JSONResponse({"error": "role must be 'admin' or 'member'"}, status_code=400)
            user.role = role

        if "channel_identities" in body:
            incoming = body["channel_identities"]
            if not isinstance(incoming, dict):
                return JSONResponse(
                    {"error": "channel_identities must be an object"}, status_code=400
                )
            normalised: dict[str, str] = {}
            for channel, value in incoming.items():
                val = str(value)
                if channel == "whatsapp":
                    val = val.split("@")[0]
                    val = re.sub(r"\D", "", val)
                normalised[channel] = val
            user.channel_identities = {**user.channel_identities, **normalised}

        await self.tenants.update_user(user)
        logger.info(f"Updated user {user_id} in tenant {tenant_id}")
        return JSONResponse({"user_id": user_id, "updated": True})

    # --- Public invitation routes ---

    async def validate_invitation(self, request: Request) -> Response:
        """GET /api/invitations/validate?code=... — public invite lookup."""
        try:
            code = request.query_params.get("code", "")
            if not code:
                return JSONResponse({"error": "Missing code parameter"}, status_code=400)
            invitation = await self.tenants.get_invitation(code)
            if not invitation or not invitation.is_valid():
                return JSONResponse({"error": "Invalid or expired invitation"}, status_code=404)
            try:
                tenant = await self.tenants.get_tenant(invitation.tenant_id)
                tenant_name = tenant.name
            except Exception:
                tenant_name = invitation.tenant_id
            return JSONResponse(
                {
                    "valid": True,
                    "tenant_name": tenant_name,
                    "tenant_id": invitation.tenant_id,
                    "email": invitation.email,
                    "role": invitation.role,
                }
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def accept_invitation(self, request: Request) -> Response:
        """POST /api/invitations/accept — link user to tenant (no JWT in local dev)."""
        try:
            body = await request.json()
            invite_code = body.get("invite_code", "")
            email = body.get("email", "")
            display_name = body.get("display_name", email.split("@")[0] if email else "")
            cognito_sub = body.get("cognito_sub", "")

            if not invite_code:
                return JSONResponse({"error": "invite_code is required"}, status_code=400)

            invitation = await self.tenants.get_invitation(invite_code)
            if not invitation or not invitation.is_valid():
                return JSONResponse({"error": "Invalid or expired invitation"}, status_code=404)

            if email and invitation.email.lower() != email.lower():
                return JSONResponse({"error": "Email does not match invitation"}, status_code=403)

            existing = await self.tenants.get_user_by_email(invitation.tenant_id, invitation.email)
            if existing:
                invitation.status = "accepted"
                invitation.accepted_at = datetime.now(timezone.utc).isoformat()
                await self.tenants.update_invitation(invitation)
                return JSONResponse(
                    {
                        "accepted": True,
                        "tenant_id": invitation.tenant_id,
                        "already_member": True,
                    }
                )

            user_id = cognito_sub or f"user-{invitation.email.split('@')[0]}"
            user = TenantUser(
                user_id=user_id,
                tenant_id=invitation.tenant_id,
                email=invitation.email,
                display_name=display_name or invitation.email.split("@")[0],
                role=invitation.role,
                cognito_sub=cognito_sub,
            )
            await self.tenants.create_user(user)

            invitation.status = "accepted"
            invitation.accepted_at = datetime.now(timezone.utc).isoformat()
            await self.tenants.update_invitation(invitation)

            return JSONResponse(
                {
                    "accepted": True,
                    "tenant_id": invitation.tenant_id,
                    "user_id": user_id,
                    "role": invitation.role,
                }
            )
        except Exception as e:
            logger.exception("Invitation accept error")
            return JSONResponse({"error": str(e)}, status_code=500)
