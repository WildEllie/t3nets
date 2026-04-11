"""
T3nets Admin API — Tenant management endpoints.

These endpoints require admin-level authentication (checked via JWT claims).
Used by platform operators to manage tenants, users, and configuration.

The onboarding flow uses a relaxed auth mode: users without a tenant_id
can still call POST /api/admin/tenants to create their first tenant.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from adapters.aws.auth_middleware import AuthError, extract_auth
from agent.models.tenant import Invitation, Tenant, TenantSettings, TenantUser

logger = logging.getLogger("t3nets.admin")


class AdminAPI:
    """Handles admin-level API requests for tenant management."""

    def __init__(self, tenants: Any, secrets: Any, skills: Any, training_store: Any = None) -> None:
        self.tenants = tenants
        self.secrets = secrets
        self.skills = skills
        self.training_store = training_store

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, Any],
        body: Optional[dict[str, Any]] = None,
    ) -> tuple[dict[str, Any], int]:
        """Route an admin API request. Returns (response_dict, status_code)."""
        try:
            # Onboarding: allow tenant creation without a tenant_id in JWT.
            # The user just signed up via Cognito and has no tenant yet.
            if method == "POST" and path == "/api/admin/tenants":
                return self._create_tenant(body or {}, headers)

            # All other admin routes require full auth (with tenant_id).
            # extract_auth() raises on invalid tokens; we don't currently use
            # its return value, but it's a required side-effect gate.
            # TODO: add role-based access (admin claim in JWT)
            extract_auth(headers)

            # Training endpoints — tenant_id injected by server via X-Tenant-Id header
            _tenant_id = headers.get("x-tenant-id") or headers.get("X-Tenant-Id", "")
            if method == "GET" and path == "/api/admin/training":
                if not _tenant_id:
                    return {"error": "Tenant not found for user"}, 404
                return self._list_training(_tenant_id, 50, False)
            elif method == "PATCH" and path.startswith("/api/admin/training/"):
                if not _tenant_id:
                    return {"error": "Tenant not found for user"}, 404
                example_id = path.rstrip("/").split("/")[-1]
                return self._annotate_training(_tenant_id, example_id, body or {})
            elif method == "DELETE" and path.startswith("/api/admin/training/"):
                if not _tenant_id:
                    return {"error": "Tenant not found for user"}, 404
                example_id = path.rstrip("/").split("/")[-1]
                return self._delete_training(_tenant_id, example_id)

            if method == "GET" and path == "/api/admin/tenants":
                return self._list_tenants()
            elif method == "GET" and path.startswith("/api/admin/tenants/"):
                parts = path.rstrip("/").split("/")
                tenant_id = parts[4]
                if len(parts) > 5 and parts[5] == "invitations":
                    return self._list_invitations(tenant_id)
                elif len(parts) > 5 and parts[5] == "users":
                    return self._list_users(tenant_id)
                else:
                    return self._get_tenant(tenant_id)
            elif method == "POST" and path.startswith("/api/admin/tenants/"):
                parts = path.rstrip("/").split("/")
                if len(parts) > 5 and parts[5] == "invitations":
                    tenant_id = parts[4]
                    return self._create_invitation(tenant_id, body or {}, headers)
                return {"error": "Not found"}, 404
            elif method == "DELETE" and path.startswith("/api/admin/tenants/"):
                parts = path.rstrip("/").split("/")
                if len(parts) > 6 and parts[5] == "invitations":
                    return self._revoke_invitation(parts[6])
                return {"error": "Not found"}, 404
            elif method == "PUT" and path.startswith("/api/admin/tenants/"):
                tenant_id = path.rstrip("/").split("/")[4]
                return self._update_tenant(tenant_id, body or {})
            elif method == "PATCH" and path.endswith("/activate"):
                parts = path.rstrip("/").split("/")
                tenant_id = parts[-2]
                return self._activate_tenant(tenant_id)
            else:
                return {"error": "Not found"}, 404

        except AuthError as e:
            return {"error": e.message}, e.status
        except Exception as e:
            logger.exception("Admin API error")
            return {"error": str(e)}, 500

    def _list_tenants(self) -> tuple[dict[str, Any], int]:
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

    def _get_tenant(self, tenant_id: str) -> tuple[dict[str, Any], int]:
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

    def _create_tenant(
        self, body: dict[str, Any], headers: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
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

    def _activate_tenant(self, tenant_id: str) -> tuple[dict[str, Any], int]:
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

    def _update_tenant(self, tenant_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
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

    # --- Invitation management ---

    def _create_invitation(
        self,
        tenant_id: str,
        body: dict[str, Any],
        headers: dict[str, Any],
    ) -> tuple[dict[str, Any], int]:
        """Create an invitation to join a tenant."""
        import asyncio

        email = body.get("email", "").strip().lower()
        role = body.get("role", "member")

        if not email:
            return {"error": "email is required"}, 400
        if role not in ("member", "admin"):
            return {"error": "role must be 'member' or 'admin'"}, 400

        # Verify tenant exists
        try:
            asyncio.run(self.tenants.get_tenant(tenant_id))
        except Exception:
            return {"error": f"Tenant '{tenant_id}' not found"}, 404

        # Check if already a member
        existing = asyncio.run(self.tenants.get_user_by_email(tenant_id, email))
        if existing:
            return {"error": f"{email} is already a member"}, 409

        # Get inviter from JWT
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
        asyncio.run(self.tenants.create_invitation(invitation))
        logger.info(f"Created invitation for {email} to tenant {tenant_id}")

        return {
            "invite_code": invitation.invite_code,
            "email": email,
            "role": role,
            "expires_at": invitation.expires_at,
        }, 201

    def _list_invitations(self, tenant_id: str) -> tuple[dict[str, Any], int]:
        """List pending invitations for a tenant."""
        import asyncio

        invitations = asyncio.run(self.tenants.list_invitations(tenant_id))
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

    def _revoke_invitation(self, invite_code: str) -> tuple[dict[str, Any], int]:
        """Revoke a pending invitation."""
        import asyncio

        invitation = asyncio.run(self.tenants.get_invitation(invite_code))
        if not invitation:
            return {"error": "Invitation not found"}, 404

        invitation.status = "revoked"
        asyncio.run(self.tenants.update_invitation(invitation))
        logger.info(f"Revoked invitation {invite_code}")

        return {"revoked": True, "invite_code": invite_code}, 200

    def _resolve_tenant_id(self, user_id: str) -> Optional[str]:
        """Resolve the caller's tenant_id from their cognito sub via DynamoDB."""
        import asyncio

        try:
            user = asyncio.run(self.tenants.get_user_by_cognito_sub(user_id))
            return user.tenant_id if user else None
        except Exception:
            return None

    # --- Training data ---

    def _list_training(
        self, tenant_id: str, limit: int, unannotated: bool
    ) -> tuple[dict[str, Any], int]:
        import asyncio

        if not self.training_store:
            return {"examples": [], "count": 0}, 200
        examples = asyncio.run(self.training_store.list_examples(tenant_id, limit=limit))
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

    def _annotate_training(
        self, tenant_id: str, example_id: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        import asyncio

        if not self.training_store:
            return {"error": "Training store not configured"}, 500
        skill = body.get("skill", "")
        action = body.get("action", "")
        found = asyncio.run(
            self.training_store.annotate_example(tenant_id, example_id, skill, action)
        )
        if not found:
            return {"error": "Example not found"}, 404
        return {"example_id": example_id, "annotated": True}, 200

    def _delete_training(self, tenant_id: str, example_id: str) -> tuple[dict[str, Any], int]:
        import asyncio

        if not self.training_store:
            return {"error": "Training store not configured"}, 500
        found = asyncio.run(self.training_store.delete_example(tenant_id, example_id))
        if not found:
            return {"error": "Example not found"}, 404
        return {"example_id": example_id, "deleted": True}, 200

    def _list_users(self, tenant_id: str) -> tuple[dict[str, Any], int]:
        """List all users in a tenant."""
        import asyncio

        users = asyncio.run(self.tenants.list_users(tenant_id))
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
