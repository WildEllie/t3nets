"""Shared settings handlers — GET/POST /api/settings.

Used by both ``adapters.aws.server`` and ``adapters.local.dev_server`` to
avoid duplicating ~150 lines of validation and mutation logic.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from agent.interfaces.secrets_provider import SecretsProvider
from agent.interfaces.tenant_store import TenantStore
from agent.models.ai_models import DEFAULT_MODEL_ID, get_model, get_models_for_providers
from agent.practices.registry import PracticeRegistry
from agent.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

# Type alias for the optional background callback invoked after skill changes.
RebuildCallback = Callable[[str], Any]  # (tenant_id) -> None or coroutine


class SettingsHandlers:
    """Handlers for ``GET /api/settings`` and ``POST /api/settings``.

    Dependencies are injected via ``__init__`` so the class never imports
    anything from ``adapters.aws`` or ``adapters.local``.

    Parameters
    ----------
    tenant_store:
        Reads / writes ``Tenant`` objects.
    secrets_provider:
        Used to list connected integrations for a tenant.
    skill_registry:
        Provides the catalogue of available skills.
    practice_registry:
        Provides the catalogue of available practices.
    active_providers:
        Callable returning the list of active AI provider names
        (e.g. ``["bedrock"]`` or ``["anthropic", "ollama"]``).
    platform:
        Platform identifier included in the settings response
        (e.g. ``"aws"`` or ``"local"``).
    stage:
        Deployment stage included in the settings response
        (e.g. ``"dev"`` or ``"prod"``).
    build_number:
        Build identifier included in the settings response.
    rebuild_callback:
        Optional async callback invoked when enabled skills change and
        the rule engine needs to be rebuilt.  Receives ``tenant_id``.
    """

    def __init__(
        self,
        *,
        tenant_store: TenantStore,
        secrets_provider: SecretsProvider,
        skill_registry: SkillRegistry,
        practice_registry: PracticeRegistry,
        active_providers: Callable[[], list[str]],
        platform: str,
        stage: str,
        build_number: str,
        rebuild_callback: RebuildCallback | None = None,
    ) -> None:
        self._tenants = tenant_store
        self._secrets = secrets_provider
        self._skills = skill_registry
        self._practices = practice_registry
        self._active_providers = active_providers
        self._platform = platform
        self._stage = stage
        self._build_number = build_number
        self._rebuild_callback = rebuild_callback

    # ------------------------------------------------------------------
    # GET /api/settings
    # ------------------------------------------------------------------

    async def get_settings(self, request: Request, tenant_id: str) -> Response:
        """Return current tenant settings and available models."""
        try:
            tenant = await self._tenants.get_tenant(tenant_id)
            s = tenant.settings

            available_skills = [
                {
                    "name": sk.name,
                    "description": sk.description.strip(),
                    "requires_integration": sk.requires_integration,
                }
                for sk in self._skills.list_skills()
            ]

            connected_integrations = await self._secrets.list_integrations(tenant_id)

            providers = self._active_providers()
            return JSONResponse(
                {
                    "ai_model": s.ai_model or DEFAULT_MODEL_ID,
                    "providers": providers,
                    "models": get_models_for_providers(providers),
                    "platform": self._platform,
                    "stage": self._stage,
                    "build": self._build_number,
                    "enabled_skills": s.enabled_skills,
                    "available_skills": available_skills,
                    "connected_integrations": connected_integrations,
                    "enabled_channels": s.enabled_channels,
                    "system_prompt_override": s.system_prompt_override,
                    "max_tokens_per_message": s.max_tokens_per_message,
                    "messages_per_day": s.messages_per_day,
                    "max_conversation_history": s.max_conversation_history,
                    "primary_practice": s.primary_practice,
                }
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ------------------------------------------------------------------
    # POST /api/settings
    # ------------------------------------------------------------------

    async def post_settings(self, request: Request, tenant_id: str) -> Response:
        """Validate and apply settings changes for a tenant."""
        try:
            body = await request.json()
            tenant = await self._tenants.get_tenant(tenant_id)
            changed = False
            rebuild_skills = False

            if "ai_model" in body:
                model_id = body["ai_model"]
                model = get_model(model_id)
                if not model:
                    return JSONResponse({"error": f"Unknown model: {model_id}"}, status_code=400)
                active = self._active_providers()
                if not any(p in model.providers for p in active):
                    return JSONResponse(
                        {"error": f"Model '{model_id}' not available for {active}"},
                        status_code=400,
                    )
                tenant.settings.ai_model = model_id
                changed = True
                logger.info("Model changed to: %s (%s)", model.display_name, model_id)

            if "enabled_skills" in body:
                skill_list = body["enabled_skills"]
                if not isinstance(skill_list, list):
                    return JSONResponse({"error": "enabled_skills must be a list"}, status_code=400)
                known = set(self._skills.list_skill_names())
                unknown = [s for s in skill_list if s not in known]
                if unknown:
                    return JSONResponse(
                        {"error": f"Unknown skills: {', '.join(unknown)}"},
                        status_code=400,
                    )
                tenant.settings.enabled_skills = skill_list
                changed = True
                rebuild_skills = True
                logger.info("Enabled skills updated: %s", skill_list)

            if "system_prompt_override" in body:
                tenant.settings.system_prompt_override = body["system_prompt_override"]
                changed = True

            if "max_tokens_per_message" in body:
                val = body["max_tokens_per_message"]
                if not isinstance(val, int) or val < 256 or val > 16384:
                    return JSONResponse(
                        {"error": "max_tokens_per_message must be 256-16384"},
                        status_code=400,
                    )
                tenant.settings.max_tokens_per_message = val
                changed = True

            if "messages_per_day" in body:
                val = body["messages_per_day"]
                if not isinstance(val, int) or val < 1:
                    return JSONResponse(
                        {"error": "messages_per_day must be a positive integer"},
                        status_code=400,
                    )
                tenant.settings.messages_per_day = val
                changed = True

            if "max_conversation_history" in body:
                val = body["max_conversation_history"]
                if not isinstance(val, int) or val < 1 or val > 100:
                    return JSONResponse(
                        {"error": "max_conversation_history must be 1-100"},
                        status_code=400,
                    )
                tenant.settings.max_conversation_history = val
                changed = True

            if "primary_practice" in body:
                practice_name = body["primary_practice"]
                tenant.settings.primary_practice = practice_name
                # Auto-add the practice's skills to enabled_skills
                for p in self._practices.list_all():
                    if p.name == practice_name:
                        for skill_name in p.skills:
                            if skill_name not in tenant.settings.enabled_skills:
                                tenant.settings.enabled_skills.append(skill_name)
                        break
                changed = True
                rebuild_skills = True
                logger.info("Primary practice set to: %s", practice_name)

            if changed:
                await self._tenants.update_tenant(tenant)
            if rebuild_skills and self._rebuild_callback is not None:
                self._rebuild_callback(tenant_id)

            return JSONResponse({"ok": True})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
