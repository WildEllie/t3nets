"""
Local Tenant Store — SQLite.

For local development. Seeds a default tenant on startup.
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

from agent.interfaces.tenant_store import TenantStore, TenantNotFound, UserNotFound
from agent.models.tenant import Tenant, TenantSettings, TenantUser


class SQLiteTenantStore(TenantStore):
    """SQLite-backed tenant store for local development."""

    def __init__(self, db_path: str = "data/t3nets.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
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
                "INSERT OR IGNORE INTO tenant_users VALUES (?, ?, ?, ?, ?, ?)",
                ("admin", tenant_id, admin_email, admin_name, "admin", "{}"),
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

    async def create_user(self, user: TenantUser) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO tenant_users VALUES (?, ?, ?, ?, ?, ?)",
                (user.user_id, user.tenant_id, user.email,
                 user.display_name, user.role,
                 json.dumps(user.channel_identities)),
            )

    async def update_user(self, user: TenantUser) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE tenant_users SET email=?, display_name=?, role=?, channel_identities=? WHERE tenant_id=? AND user_id=?",
                (user.email, user.display_name, user.role,
                 json.dumps(user.channel_identities),
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

    # --- Helpers ---

    def _row_to_tenant(self, row: tuple) -> Tenant:
        settings_dict = json.loads(row[4])
        settings = TenantSettings(**settings_dict)
        return Tenant(
            tenant_id=row[0],
            name=row[1],
            status=row[2],
            created_at=row[3],
            settings=settings,
        )

    def _row_to_user(self, row: tuple) -> TenantUser:
        return TenantUser(
            user_id=row[0],
            tenant_id=row[1],
            email=row[2],
            display_name=row[3],
            role=row[4],
            channel_identities=json.loads(row[5]),
        )
