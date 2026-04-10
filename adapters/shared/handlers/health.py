"""Health endpoint handler — shared between local and AWS servers."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from adapters.shared.server_utils import _uptime_human
from agent.interfaces.secrets_provider import SecretsProvider
from agent.interfaces.tenant_store import TenantStore
from agent.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


class HealthHandlers:
    """Handles GET /api/health.

    Divergence between AWS and local is abstracted via callables:
    - ``connection_count``: AWS uses ``push_client.connection_count``,
      local uses ``sse_manager.connection_count``.
    - ``get_stats``: each server provides its own routing-stats snapshot.
    - ``get_ai_info``: each server provides AI provider/model/key info.
    """

    def __init__(
        self,
        tenants: TenantStore,
        secrets: SecretsProvider,
        skill_registry: SkillRegistry,
        started_at: float,
        connection_count: Callable[[], int],
        get_stats: Callable[[], dict[str, Any]],
        get_ai_info: Callable[[], dict[str, Any]],
        platform: str = "local",
        stage: str = "dev",
        default_tenant: str = "default",
        connection_label: str = "sse_connections",
    ) -> None:
        self._tenants = tenants
        self._secrets = secrets
        self._skills = skill_registry
        self._started_at = started_at
        self._connection_count = connection_count
        self._get_stats = get_stats
        self._get_ai_info = get_ai_info
        self._platform = platform
        self._stage = stage
        self._default_tenant = default_tenant
        self._connection_label = connection_label

    async def handle_health_api(self, request: Request) -> Response:
        """Rich health/status JSON endpoint — GET /api/health."""
        try:
            uptime_secs = time.time() - self._started_at
            tenant = await self._tenants.get_tenant(self._default_tenant)
            connected = await self._secrets.list_integrations(self._default_tenant)

            integration_names = ["jira", "github", "teams", "telegram", "twilio"]
            integrations = {name: {"connected": name in connected} for name in integration_names}

            skills_info = [
                {
                    "name": s.name,
                    "description": s.description.strip()[:120],
                    "requires_integration": s.requires_integration,
                    "supports_raw": s.supports_raw,
                    "triggers": s.triggers[:8],
                }
                for s in self._skills.list_skills()
            ]

            health: dict[str, Any] = {
                "status": "ok",
                "platform": self._platform,
                "stage": self._stage,
                "started_at": datetime.fromtimestamp(self._started_at, tz=timezone.utc).isoformat(),
                "uptime_seconds": round(uptime_secs, 1),
                "uptime_human": _uptime_human(uptime_secs),
                "python_version": (
                    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
                ),
                "tenant": {
                    "tenant_id": tenant.tenant_id,
                    "name": tenant.name,
                    "status": tenant.status,
                    "enabled_skills": tenant.settings.enabled_skills,
                    "ai_model": tenant.settings.ai_model,
                },
                "ai": self._get_ai_info(),
                "routing": self._get_stats(),
                "integrations": integrations,
                "skills": skills_info,
                self._connection_label: self._connection_count(),
            }
            return JSONResponse(health)

        except Exception as e:
            logger.exception("Health check error")
            return JSONResponse({"status": "error", "error": str(e)}, status_code=500)
