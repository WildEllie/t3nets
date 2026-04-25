"""
T3nets Admin API — Tenant management endpoints.

These endpoints require admin-level authentication (checked via JWT claims).
Used by platform operators to manage tenants, users, and configuration.

The onboarding flow uses a relaxed auth mode: users without a tenant_id
can still call POST /api/admin/tenants to create their first tenant.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from adapters.aws.auth_middleware import AuthError, extract_auth
from agent.models.tenant import Invitation, Tenant, TenantSettings, TenantUser

logger = logging.getLogger("t3nets.admin")


# Compiled route patterns — named groups carry path params.
_RE_TENANT = re.compile(r"^/api/admin/tenants/(?P<tenant_id>[^/]+)$")
_RE_TENANT_INVITES = re.compile(r"^/api/admin/tenants/(?P<tenant_id>[^/]+)/invitations$")
_RE_TENANT_USERS = re.compile(r"^/api/admin/tenants/(?P<tenant_id>[^/]+)/users$")
_RE_TENANT_INVITE = re.compile(
    r"^/api/admin/tenants/(?P<tenant_id>[^/]+)/invitations/(?P<invite_code>[^/]+)$"
)
_RE_TENANT_ACTIVATE = re.compile(r"^/api/admin/tenants/(?P<tenant_id>[^/]+)/activate$")
_RE_TRAINING_ITEM = re.compile(r"^/api/admin/training/(?P<example_id>[^/]+)$")

# tenant_id format: 3+ chars, lowercase alphanumeric + hyphens, can't start/end with hyphen.
_TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


class AdminAPI:
    """Handles admin-level API requests for tenant management."""

    def __init__(self, tenants: Any, secrets: Any, skills: Any, training_store: Any = None) -> None:
        self.tenants = tenants
        self.secrets = secrets
        self.skills = skills
        self.training_store = training_store

    async def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, Any],
        body: Optional[dict[str, Any]] = None,
    ) -> tuple[dict[str, Any], int]:
        """Route an admin API request. Returns (response_dict, status_code)."""
        path = path.rstrip("/") or "/"
        try:
            # Onboarding: relaxed auth — tenant creation without a tenant_id in JWT.
            if method == "POST" and path == "/api/admin/tenants":
                return await self._create_tenant(body or {}, headers)

            # All other admin routes require full auth (side-effect gate; raises on
            # invalid tokens). TODO: add role-based access (admin claim in JWT).
            extract_auth(headers)
            h_tenant_id = headers.get("x-tenant-id") or headers.get("X-Tenant-Id", "")
            body = body or {}

            if method == "GET":
                if path == "/api/admin/tenants":
                    return await self._list_tenants()
                if path == "/api/admin/training":
                    if not h_tenant_id:
                        return {"error": "Tenant not found for user"}, 404
                    return await self._list_training(h_tenant_id, 50, False)
                if m := _RE_TENANT_INVITES.match(path):
                    return await self._list_invitations(m["tenant_id"])
                if m := _RE_TENANT_USERS.match(path):
                    return await self._list_users(m["tenant_id"])
                if m := _RE_TENANT.match(path):
                    return await self._get_tenant(m["tenant_id"])

            elif method == "POST":
                if m := _RE_TENANT_INVITES.match(path):
                    return await self._create_invitation(m["tenant_id"], body, headers)

            elif method == "PUT":
                if m := _RE_TENANT.match(path):
                    return await self._update_tenant(m["tenant_id"], body)

            elif method == "PATCH":
                if m := _RE_TENANT_ACTIVATE.match(path):
                    return await self._activate_tenant(m["tenant_id"])
                if m := _RE_TRAINING_ITEM.match(path):
                    if not h_tenant_id:
                        return {"error": "Tenant not found for user"}, 404
                    return await self._annotate_training(h_tenant_id, m["example_id"], body)

            elif method == "DELETE":
                if m := _RE_TENANT_INVITE.match(path):
                    return await self._revoke_invitation(m["invite_code"])
                if m := _RE_TRAINING_ITEM.match(path):
                    if not h_tenant_id:
                        return {"error": "Tenant not found for user"}, 404
                    return await self._delete_training(h_tenant_id, m["example_id"])

            return {"error": "Not found"}, 404

        except AuthError as e:
            return {"error": e.message}, e.status
        except Exception as e:
            logger.exception("Admin API error")
            return {"error": str(e)}, 500

    # --- Tenant CRUD ---

    async def _list_tenants(self) -> tuple[dict[str, Any], int]:
        """List all tenants."""
        tenant_list = await self.tenants.list_tenants()
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

    async def _get_tenant(self, tenant_id: str) -> tuple[dict[str, Any], int]:
        """Get a single tenant with full details."""
        try:
            tenant = await self.tenants.get_tenant(tenant_id)
        except Exception:
            return {"error": f"Tenant '{tenant_id}' not found"}, 404

        connected = await self.secrets.list_integrations(tenant_id)

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

    async def _create_tenant(
        self, body: dict[str, Any], headers: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        """Create a new tenant, optionally with a first admin user.

        Relaxed auth: caller may not have tenant_id yet (onboarding).
        """
        tenant_id = body.get("tenant_id", "").strip()
        name = body.get("name", "").strip()

        if not tenant_id or not name:
            return {"error": "tenant_id and name are required"}, 400

        if not _TENANT_ID_RE.match(tenant_id) or len(tenant_id) < 3:
            return {
                "error": "tenant_id must be 3+ chars, lowercase letters/numbers/hyphens only"
            }, 400

        # Check if already exists
        try:
            await self.tenants.get_tenant(tenant_id)
            return {"error": f"Tenant '{tenant_id}' already exists"}, 409
        except Exception:
            pass  # expected — tenant doesn't exist yet

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
        await self.tenants.create_tenant(tenant)
        logger.info(f"Created tenant: {tenant_id} ({name}) [status={status}]")

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
            await self.tenants.create_user(user)
            logger.info(f"Created admin user for tenant {tenant_id}: {user.email}")

        return {"tenant_id": tenant_id, "created": True}, 201

    async def _activate_tenant(self, tenant_id: str) -> tuple[dict[str, Any], int]:
        """Set tenant status from 'onboarding' to 'active'."""
        try:
            tenant = await self.tenants.get_tenant(tenant_id)
        except Exception:
            return {"error": f"Tenant '{tenant_id}' not found"}, 404

        if tenant.status == "active":
            return {"tenant_id": tenant_id, "status": "active", "message": "Already active"}, 200

        tenant.status = "active"
        await self.tenants.update_tenant(tenant)
        logger.info(f"Activated tenant: {tenant_id}")

        return {"tenant_id": tenant_id, "status": "active", "activated": True}, 200

    async def _update_tenant(
        self, tenant_id: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        """Update an existing tenant's settings."""
        try:
            tenant = await self.tenants.get_tenant(tenant_id)
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

        await self.tenants.update_tenant(tenant)
        logger.info(f"Updated tenant: {tenant_id}")

        return {"tenant_id": tenant_id, "updated": True}, 200

    # --- Invitation management ---

    async def _create_invitation(
        self,
        tenant_id: str,
        body: dict[str, Any],
        headers: dict[str, Any],
    ) -> tuple[dict[str, Any], int]:
        """Create an invitation to join a tenant."""
        email = body.get("email", "").strip().lower()
        role = body.get("role", "member")

        if not email:
            return {"error": "email is required"}, 400
        if role not in ("member", "admin"):
            return {"error": "role must be 'member' or 'admin'"}, 400

        try:
            await self.tenants.get_tenant(tenant_id)
        except Exception:
            return {"error": f"Tenant '{tenant_id}' not found"}, 404

        existing = await self.tenants.get_user_by_email(tenant_id, email)
        if existing:
            return {"error": f"{email} is already a member"}, 409

        try:
            auth = extract_auth(headers)
            invited_by = auth.user_id
        except AuthError:
            invited_by = ""

        now = datetime.now(timezone.utc).isoformat()
        invitation = Invitation(
            invite_code=Invitation.generate_code(),
            tenant_id=tenant_id,
            email=email,
            role=role,
            status="pending",
            invited_by=invited_by,
            created_at=now,
            expires_at=Invitation.default_expiry(),
        )
        await self.tenants.create_invitation(invitation)
        logger.info(f"Created invitation for {email} to tenant {tenant_id}")

        return {
            "invite_code": invitation.invite_code,
            "email": email,
            "role": role,
            "expires_at": invitation.expires_at,
        }, 201

    async def _list_invitations(self, tenant_id: str) -> tuple[dict[str, Any], int]:
        """List pending invitations for a tenant."""
        invitations = await self.tenants.list_invitations(tenant_id)
        return {
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
        }, 200

    async def _revoke_invitation(self, invite_code: str) -> tuple[dict[str, Any], int]:
        """Revoke a pending invitation."""
        invitation = await self.tenants.get_invitation(invite_code)
        if not invitation:
            return {"error": "Invitation not found"}, 404

        invitation.status = "revoked"
        await self.tenants.update_invitation(invitation)
        logger.info(f"Revoked invitation {invite_code}")

        return {"revoked": True, "invite_code": invite_code}, 200

    async def _resolve_tenant_id(self, user_id: str) -> Optional[str]:
        """Resolve the caller's tenant_id from their cognito sub via DynamoDB."""
        try:
            user = await self.tenants.get_user_by_cognito_sub(user_id)
            return user.tenant_id if user else None
        except Exception:
            return None

    # --- Training data ---

    async def _list_training(
        self, tenant_id: str, limit: int, unannotated: bool
    ) -> tuple[dict[str, Any], int]:
        if not self.training_store:
            return {"examples": [], "count": 0}, 200
        examples = await self.training_store.list_examples(tenant_id, limit=limit)
        if unannotated:
            examples = [e for e in examples if not e.admin_override_skill]
        return {
            "examples": [
                {
                    "example_id": e.example_id,
                    "message_text": e.message_text,
                    "timestamp": e.timestamp,
                    "matched_skill": e.matched_skill,
                    "matched_action": e.matched_action,
                    "was_disabled_skill": e.was_disabled_skill,
                    "confidence": e.confidence,
                    "admin_override_skill": e.admin_override_skill,
                    "admin_override_action": e.admin_override_action,
                }
                for e in examples
            ],
            "count": len(examples),
        }, 200

    async def _annotate_training(
        self, tenant_id: str, example_id: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        if not self.training_store:
            return {"error": "Training store not configured"}, 500
        skill = body.get("skill", "")
        action = body.get("action", "")
        found = await self.training_store.annotate_example(tenant_id, example_id, skill, action)
        if not found:
            return {"error": "Example not found"}, 404
        return {"example_id": example_id, "annotated": True}, 200

    async def _delete_training(self, tenant_id: str, example_id: str) -> tuple[dict[str, Any], int]:
        if not self.training_store:
            return {"error": "Training store not configured"}, 500
        found = await self.training_store.delete_example(tenant_id, example_id)
        if not found:
            return {"error": "Example not found"}, 404
        return {"example_id": example_id, "deleted": True}, 200

    async def _list_users(self, tenant_id: str) -> tuple[dict[str, Any], int]:
        """List all users in a tenant."""
        users = await self.tenants.list_users(tenant_id)
        return {
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
        }, 200
