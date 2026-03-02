"""
Local Tenant Store — SQLite.

For local development. Seeds a default tenant on startup.
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

from agent.interfaces.tenant_store import TenantStore, TenantNotFound, UserNotFound
from agent.models.tenant import Invitation, Tenant, TenantSettings, TenantUser


class SQLiteTenantStore(TenantStore):
    """SQLite-backed tenant store for local development."""

    def __init__(self, db_path: str = "data/t3nets.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tenants (
                    tenant_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    settings TEXT NOT NULL DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tenant_users (
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member',
                    channel_identities TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (tenant_id, user_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS channel_mappings (
                    channel_type TEXT NOT NULL,
                    channel_specific_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    PRIMARY KEY (channel_type, channel_specific_id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS invitations (
                    invite_code TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member',
                    status TEXT NOT NULL DEFAULT 'pending',
                    invited_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL DEFAULT '',
                    accepted_at TEXT NOT NULL DEFAULT ''
                )
            """)

            # --- Safe migrations for new columns ---
            # Add cognito_sub and last_login columns if they don't exist yet
            existing_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(tenant_users)").fetchall()
            }
            if "cognito_sub" not in existing_cols:
                conn.execute(
                    "ALTER TABLE tenant_users ADD COLUMN cognito_sub TEXT NOT NULL DEFAULT ''"
                )
            if "last_login" not in existing_cols:
                conn.execute(
                    "ALTER TABLE tenant_users ADD COLUMN last_login TEXT NOT NULL DEFAULT ''"
                )
            if "avatar_url" not in existing_cols:
                conn.execute(
                    "ALTER TABLE tenant_users ADD COLUMN avatar_url TEXT NOT NULL DEFAULT ''"
                )

    def seed_default_tenant(
        self,
        tenant_id: str = "local",
        name: str = "Local Development",
        admin_email: str = "admin@local.dev",
        admin_name: str = "Admin",
        enabled_skills: list[str] | None = None,
    ) -> Tenant:
        """Create a default tenant for local dev if it doesn't exist."""
        with sqlite3.connect(self.db_path) as conn:
            existing = conn.execute(
                "SELECT tenant_id FROM tenants WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()

            if existing:
                # Update enabled skills if provided
                if enabled_skills is not None:
                    row = conn.execute(
                        "SELECT settings FROM tenants WHERE tenant_id = ?",
                        (tenant_id,),
                    ).fetchone()
                    settings = json.loads(row[0]) if row else {}
                    settings["enabled_skills"] = enabled_skills
                    conn.execute(
                        "UPDATE tenants SET settings = ? WHERE tenant_id = ?",
                        (json.dumps(settings), tenant_id),
                    )
                return self._row_to_tenant(conn.execute(
                    "SELECT * FROM tenants WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone())

            settings = TenantSettings(
                enabled_skills=enabled_skills or ["sprint_status"],
            )
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()

            conn.execute(
                "INSERT INTO tenants VALUES (?, ?, ?, ?, ?)",
                (tenant_id, name, "active", now, json.dumps(settings.__dict__)),
            )
            conn.execute(
                "INSERT OR IGNORE INTO tenant_users"
                " (user_id, tenant_id, email, display_name, role,"
                " channel_identities, cognito_sub, last_login, avatar_url)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("admin", tenant_id, admin_email, admin_name, "admin", "{}", "", "", ""),
            )

        return Tenant(
            tenant_id=tenant_id,
            name=name,
            status="active",
            created_at=now,
            settings=settings,
        )

    # --- Tenant operations ---

    async def get_tenant(self, tenant_id: str) -> Tenant:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM tenants WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        if not row:
            raise TenantNotFound(f"Tenant '{tenant_id}' not found")
        return self._row_to_tenant(row)

    async def create_tenant(self, tenant: Tenant) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO tenants VALUES (?, ?, ?, ?, ?)",
                (tenant.tenant_id, tenant.name, tenant.status,
                 tenant.created_at, json.dumps(tenant.settings.__dict__)),
            )

    async def update_tenant(self, tenant: Tenant) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE tenants SET name=?, status=?, settings=? WHERE tenant_id=?",
                (tenant.name, tenant.status,
                 json.dumps(tenant.settings.__dict__), tenant.tenant_id),
            )

    async def list_tenants(self) -> list[Tenant]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM tenants").fetchall()
        return [self._row_to_tenant(r) for r in rows]

    # --- Channel mapping ---

    async def get_by_channel_id(self, channel_type: str, channel_specific_id: str) -> Tenant:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT tenant_id FROM channel_mappings WHERE channel_type=? AND channel_specific_id=?",
                (channel_type, channel_specific_id),
            ).fetchone()
        if not row:
            raise TenantNotFound(
                f"No tenant mapped to {channel_type}:{channel_specific_id}"
            )
        return await self.get_tenant(row[0])

    async def set_channel_mapping(self, tenant_id: str, channel_type: str, channel_specific_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO channel_mappings VALUES (?, ?, ?)
                   ON CONFLICT(channel_type, channel_specific_id)
                   DO UPDATE SET tenant_id = ?""",
                (channel_type, channel_specific_id, tenant_id, tenant_id),
            )

    # --- User operations ---

    async def get_user(self, tenant_id: str, user_id: str) -> TenantUser:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM tenant_users WHERE tenant_id=? AND user_id=?",
                (tenant_id, user_id),
            ).fetchone()
        if not row:
            raise UserNotFound(f"User '{user_id}' not found in tenant '{tenant_id}'")
        return self._row_to_user(row)

    async def get_user_by_email(self, tenant_id: str, email: str) -> Optional[TenantUser]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM tenant_users WHERE tenant_id=? AND email=?",
                (tenant_id, email),
            ).fetchone()
        return self._row_to_user(row) if row else None

    async def get_user_by_channel_identity(
        self, tenant_id: str, channel_type: str, channel_user_id: str,
    ) -> Optional[TenantUser]:
        # For local dev, just return the admin user
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM tenant_users WHERE tenant_id=? LIMIT 1",
                (tenant_id,),
            ).fetchone()
        return self._row_to_user(row) if row else None

    async def get_user_by_cognito_sub(self, cognito_sub: str) -> Optional[TenantUser]:
        """Cross-tenant lookup by Cognito sub."""
        if not cognito_sub:
            return None
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM tenant_users WHERE cognito_sub = ? LIMIT 1",
                (cognito_sub,),
            ).fetchone()
        return self._row_to_user(row) if row else None

    async def create_user(self, user: TenantUser) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO tenant_users (user_id, tenant_id, email, display_name,"
                " role, channel_identities, cognito_sub, last_login, avatar_url)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user.user_id, user.tenant_id, user.email,
                 user.display_name, user.role,
                 json.dumps(user.channel_identities),
                 user.cognito_sub, user.last_login, user.avatar_url),
            )

    async def update_user(self, user: TenantUser) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE tenant_users SET email=?, display_name=?, role=?,"
                " channel_identities=?, cognito_sub=?, last_login=?,"
                " avatar_url=?"
                " WHERE tenant_id=? AND user_id=?",
                (user.email, user.display_name, user.role,
                 json.dumps(user.channel_identities),
                 user.cognito_sub, user.last_login, user.avatar_url,
                 user.tenant_id, user.user_id),
            )

    async def delete_user(self, tenant_id: str, user_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM tenant_users WHERE tenant_id=? AND user_id=?",
                (tenant_id, user_id),
            )

    async def list_users(self, tenant_id: str) -> list[TenantUser]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM tenant_users WHERE tenant_id=?",
                (tenant_id,),
            ).fetchall()
        return [self._row_to_user(r) for r in rows]

    # --- Invitation operations ---

    async def create_invitation(self, invitation: Invitation) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO invitations"
                " (invite_code, tenant_id, email, role, status,"
                " invited_by, created_at, expires_at, accepted_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    invitation.invite_code, invitation.tenant_id,
                    invitation.email, invitation.role, invitation.status,
                    invitation.invited_by, invitation.created_at,
                    invitation.expires_at, invitation.accepted_at,
                ),
            )

    async def get_invitation(self, invite_code: str) -> Optional[Invitation]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM invitations WHERE invite_code = ?",
                (invite_code,),
            ).fetchone()
        if not row:
            return None
        return self._row_to_invitation(row)

    async def update_invitation(self, invitation: Invitation) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE invitations SET status=?, accepted_at=? WHERE invite_code=?",
                (invitation.status, invitation.accepted_at, invitation.invite_code),
            )

    async def list_invitations(self, tenant_id: str) -> list[Invitation]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM invitations WHERE tenant_id=? AND status='pending'",
                (tenant_id,),
            ).fetchall()
        return [
            inv for inv in (self._row_to_invitation(r) for r in rows)
            if inv.is_valid()
        ]

    def _row_to_invitation(self, row: tuple[object, ...]) -> Invitation:
        return Invitation(
            invite_code=str(row[0]),
            tenant_id=str(row[1]),
            email=str(row[2]),
            role=str(row[3]),
            status=str(row[4]),
            invited_by=str(row[5]),
            created_at=str(row[6]),
            expires_at=str(row[7]),
            accepted_at=str(row[8]) if row[8] is not None else "",
        )

    # --- Helpers ---

    def _row_to_tenant(self, row: tuple[object, ...]) -> Tenant:
        settings_dict = json.loads(str(row[4]))
        settings = TenantSettings(**settings_dict)
        return Tenant(
            tenant_id=str(row[0]),
            name=str(row[1]),
            status=str(row[2]),
            created_at=str(row[3]),
            settings=settings,
        )

    def _row_to_user(self, row: tuple[object, ...]) -> TenantUser:
        return TenantUser(
            user_id=str(row[0]),
            tenant_id=str(row[1]),
            email=str(row[2]),
            display_name=str(row[3]),
            role=str(row[4]),
            channel_identities=json.loads(str(row[5])),
            cognito_sub=str(row[6]) if len(row) > 6 else "",
            last_login=str(row[7]) if len(row) > 7 else "",
            avatar_url=str(row[8]) if len(row) > 8 else "",
        )
