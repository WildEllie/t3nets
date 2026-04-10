"""Shared chat and clear handlers for T3nets server adapters.

Extracts the core hybrid-routing chat logic used by both the AWS and local
servers.  Divergent behaviour (async skill dispatch vs. sync DirectBus,
parameter enrichment, fallback router) is injected via constructor callables
so that neither ``adapters.aws`` nor ``adapters.local`` are imported here.
"""

from __future__ import annotations

import json
import logging
import time
import uuid as _uuid
from collections.abc import Awaitable, Callable
from datetime import datetime as _dt
from datetime import timezone as _tz
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from adapters.shared.multi_provider import MultiAIProvider
from adapters.shared.server_utils import _format_raw_json, _strip_metadata
from agent.errors.handler import ErrorHandler
from agent.interfaces.conversation_store import ConversationStore
from agent.interfaces.rule_store import RuleStore
from agent.interfaces.tenant_store import TenantStore
from agent.interfaces.training_store import TrainingStore
from agent.router.compiled_engine import (
    CompiledRuleEngine,
    is_conversational,
    strip_raw_flag,
)
from agent.router.models import TrainingExample
from agent.router.rule_engine_builder import RuleEngineBuilder
from agent.skills.registry import SkillRegistry

logger = logging.getLogger("t3nets.handlers.chat")

# Type aliases for injected callables -----------------------------------------

# (tenant_id, user_email) — extracted from the request by the owning server
AuthResolver = Callable[[Request], Awaitable[tuple[str, str]]]

# model resolver: tenant -> (provider_name, api_model_id, short_name)
ModelResolver = Callable[[Any], tuple[str, str, str]]

# fire-and-forget scheduler for background coroutines
FireAndForget = Callable[[Any], None]

# skill_invoker: the server passes either a sync or async skill dispatch fn.
# Signature: (tenant_id, skill_name, params, conversation_id, request_id,
#              reply_channel, reply_target) -> skill_result dict | None
SkillInvoker = Callable[..., Awaitable[dict[str, Any] | None]]

# Optional enricher called on Tier-1/2 matches (AWS only).
MatchEnricher = Callable[[Any, str], None]

# Optional fallback router used when no compiled engine exists (AWS only).
FallbackRouter = Any  # RuleBasedRouter or None


class ChatHandlers:
    """Shared logic for ``POST /api/chat`` and ``POST /api/clear``.

    All cloud-specific behaviour is injected via constructor arguments so
    this class imports nothing from ``adapters.aws`` or ``adapters.local``.
    """

    def __init__(
        self,
        *,
        memory: ConversationStore,
        tenants: TenantStore,
        ai: MultiAIProvider,
        skills: SkillRegistry,
        compiled_engines: dict[str, CompiledRuleEngine],
        rule_store: RuleStore,
        training_store: TrainingStore,
        stats: dict[str, int],
        error_handler: ErrorHandler,
        resolve_auth: AuthResolver,
        resolve_model: ModelResolver,
        fire_and_forget: FireAndForget,
        skill_invoker: SkillInvoker,
        enrich_match: MatchEnricher | None = None,
        fallback_router: FallbackRouter = None,
    ) -> None:
        self._memory = memory
        self._tenants = tenants
        self._ai = ai
        self._skills = skills
        self._compiled_engines = compiled_engines
        self._rule_store = rule_store
        self._training_store = training_store
        self._stats = stats
        self._error_handler = error_handler
        self._resolve_auth = resolve_auth
        self._resolve_model = resolve_model
        self._fire_and_forget = fire_and_forget
        self._skill_invoker = skill_invoker
        self._enrich_match = enrich_match
        self._fallback_router = fallback_router

    # ------------------------------------------------------------------
    # Public handlers
    # ------------------------------------------------------------------

    async def handle_chat(self, request: Request) -> Response:
        """Handle ``POST /api/chat`` — hybrid three-tier routing."""
        try:
            tenant_id, user_email = await self._resolve_auth(request)
            body = await request.json()
            text = body.get("text", "").strip()
            if not text:
                return JSONResponse({"error": "Empty message"}, status_code=400)

            conversation_id = body.get("conversation_id", "default")
            clean_text, is_raw = strip_raw_flag(text)
            is_raw_response = False
            request_start = time.time()

            logger.info(f"Chat [{tenant_id}]: {text[:100]}" + (" [RAW]" if is_raw else ""))

            history = _strip_metadata(
                await self._memory.get_conversation(tenant_id, conversation_id)
            )
            tenant = await self._tenants.get_tenant(tenant_id)
            active_provider, active_model, model_short_name = self._resolve_model(tenant)
            provider_ai = self._ai.for_provider(active_provider)

            system = (
                f"You are an AI assistant for {tenant.name} on the T3nets platform.\n"
                "Be direct, helpful, and action-oriented. Flag risks early. "
                "Suggest actions.\n"
                "When you have data to present, format it clearly with structure."
            )

            engine = self._compiled_engines.get(tenant_id)

            # === TIER 0: Conversational ===
            if not is_raw and is_conversational(clean_text):
                self._stats["conversational"] += 1
                messages = history + [{"role": "user", "content": clean_text}]
                response = await provider_ai.chat(active_model, system, messages, [])
                assistant_text = response.text or "Hey! How can I help?"
                total_tokens = response.input_tokens + response.output_tokens
                route_type = "conversational"
            else:
                result = await self._route_with_skills(
                    tenant_id=tenant_id,
                    user_email=user_email,
                    tenant=tenant,
                    engine=engine,
                    clean_text=clean_text,
                    is_raw=is_raw,
                    conversation_id=conversation_id,
                    history=history,
                    system=system,
                    provider_ai=provider_ai,
                    active_model=active_model,
                    model_short_name=model_short_name,
                    request_start=request_start,
                )
                # Early return: the invoker may have returned a Response directly
                # (e.g. async skill dispatch or audio result).
                if isinstance(result, Response):
                    return result
                assistant_text, total_tokens, route_type, is_raw_response = result

            self._stats["total_tokens"] += total_tokens
            roundtrip_sec = round(time.time() - request_start, 1)

            chat_metadata: dict[str, Any] = {
                "route": route_type,
                "model": model_short_name,
                "tokens": total_tokens,
                "timestamp": int(request_start * 1000),
                "roundtrip_sec": roundtrip_sec,
            }
            if user_email:
                chat_metadata["user_email"] = user_email
            if not is_raw_response:
                await self._memory.save_turn(
                    tenant_id,
                    conversation_id,
                    clean_text,
                    assistant_text,
                    metadata=chat_metadata,
                )

            return JSONResponse(
                {
                    "text": assistant_text,
                    "conversation_id": conversation_id,
                    "tokens": total_tokens,
                    "route": route_type,
                    "raw": is_raw_response,
                    "model": model_short_name,
                    "user_email": user_email,
                }
            )

        except Exception as e:
            logger.exception("Chat error")
            self._stats["errors"] += 1
            friendly = self._error_handler.handle(e, context="chat")
            return JSONResponse({"error": friendly.message, **friendly.to_dict()}, status_code=500)

    async def handle_clear(self, request: Request) -> Response:
        """Handle ``POST /api/clear`` — clear conversation history."""
        try:
            tenant_id, _ = await self._resolve_auth(request)
            body = await request.json()
            cid = body.get("conversation_id", "default")
            await self._memory.clear_conversation(tenant_id, cid)
            return JSONResponse({"cleared": True})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def rebuild_rules(self, tenant_id: str) -> None:
        """(Re)build AI-generated routing rules for *tenant_id* and cache."""
        try:
            tenant = await self._tenants.get_tenant(tenant_id)
            all_skills = self._skills.list_skills()
            enabled = [s for s in all_skills if s.name in tenant.settings.enabled_skills]
            disabled = [s for s in all_skills if s.name not in tenant.settings.enabled_skills]

            existing = await self._rule_store.load_rule_set(tenant_id)
            old_version = existing.version if existing else 0

            training_data = await self._training_store.list_examples(tenant_id, limit=50)

            active_provider, api_model, _ = self._resolve_model(tenant)
            builder = RuleEngineBuilder()
            rule_set = await builder.build_rules(
                tenant_id=tenant_id,
                enabled_skills=enabled,
                disabled_skills=disabled,
                ai=self._ai.for_provider(active_provider),
                model=api_model,
                training_data=training_data or None,
            )
            rule_set.version = old_version + 1

            await self._rule_store.save_rule_set(rule_set)
            self._compiled_engines[tenant_id] = CompiledRuleEngine(rule_set, self._skills)
            logger.info(f"Rules rebuilt for tenant '{tenant_id}' (v{rule_set.version})")
        except Exception:
            logger.exception(f"Failed to rebuild rules for tenant '{tenant_id}'")

    async def log_training(
        self,
        tenant_id: str,
        message_text: str,
        matched_skill: str | None,
        matched_action: str | None,
        was_disabled_skill: bool = False,
    ) -> None:
        """Fire-and-forget helper: log a routing decision as training data."""
        try:
            example = TrainingExample(
                tenant_id=tenant_id,
                example_id=_uuid.uuid4().hex,
                message_text=message_text,
                timestamp=_dt.now(_tz.utc).isoformat(),
                matched_skill=matched_skill,
                matched_action=matched_action,
                was_disabled_skill=was_disabled_skill,
            )
            logger.info(
                f"Training: logging example for tenant={tenant_id} "
                f"skill={matched_skill} msg={message_text[:40]!r}"
            )
            await self._training_store.log_example(example)
            logger.info(f"Training: logged example {example.example_id}")
        except Exception:
            logger.exception("Failed to log training example")

    # ------------------------------------------------------------------
    # Skill routing internals
    # ------------------------------------------------------------------

    async def _route_with_skills(  # noqa: N802
        self,
        *,
        tenant_id: str,
        user_email: str,
        tenant: Any,
        engine: CompiledRuleEngine | None,
        clean_text: str,
        is_raw: bool,
        conversation_id: str,
        history: list[dict[str, Any]],
        system: str,
        provider_ai: Any,
        active_model: str,
        model_short_name: str,
        request_start: float,
    ) -> Response | tuple[str, int, str, bool]:
        """Run Tier 1 / 2 / 3 routing.  Returns either:

        * A ``Response`` (early exit for async dispatch or audio results)
        * A tuple ``(assistant_text, total_tokens, route_type, is_raw_response)``
        """
        router = engine or self._fallback_router
        match = router.match(clean_text, tenant.settings.enabled_skills) if router else None

        # === TIER 1/2: Compiled rule match ===
        if match:
            if self._enrich_match:
                self._enrich_match(match, clean_text)

            result = await self._skill_invoker(
                tenant_id,
                match.skill_name,
                match.params,
                conversation_id,
                f"rule-{conversation_id}",
                "dashboard",
                user_email or "user",
            )

            # skill_invoker may return a Response (async dispatch)
            if isinstance(result, Response):
                self._stats["rule_routed"] += 1
                return result

            skill_result = result or {"error": "No result"}

            # Audio results: return directly with audio data
            if skill_result.get("type") == "audio":
                roundtrip_sec = round(time.time() - request_start, 1)
                await self._memory.save_turn(
                    tenant_id,
                    conversation_id,
                    clean_text,
                    skill_result.get("text", ""),
                    metadata={"route": "rule", "skill": match.skill_name},
                )
                return JSONResponse(
                    {
                        "text": skill_result.get("text", ""),
                        "audio": {
                            "audio_b64": skill_result.get("audio_b64", ""),
                            "audio_url": skill_result.get("audio_url", ""),
                            "format": skill_result.get("format", "wav"),
                        },
                        "conversation_id": conversation_id,
                        "tokens": 0,
                        "route": "rule",
                        "skill": match.skill_name,
                        "raw": False,
                        "model": "",
                        "roundtrip_sec": roundtrip_sec,
                    }
                )

            if is_raw and engine and engine.supports_raw(match.skill_name):
                self._stats["raw"] += 1
                self._stats["rule_routed"] += 1
                return (
                    _format_raw_json(skill_result),
                    0,
                    "rule",
                    True,
                )

            self._stats["rule_routed"] += 1
            prompt = (
                f'{system}\n\nThe user asked: "{clean_text}"\n\n'
                f"Tool data:\n{json.dumps(skill_result, indent=2)}\n\n"
                "Format this clearly."
            )
            messages = history + [{"role": "user", "content": prompt}]
            response = await provider_ai.chat(active_model, system, messages, [])
            assistant_text = response.text or "Got data but couldn't format."
            total_tokens = response.input_tokens + response.output_tokens
            return (assistant_text, total_tokens, "rule", False)

        # Check for disabled skill
        if engine and (disabled_skill := engine.check_disabled_skill(clean_text)):
            skill_display = disabled_skill.replace("_", " ")
            logger.info(f"Route: DISABLED SKILL -> {disabled_skill}")
            assistant_text = (
                f"The {skill_display} feature isn't enabled for your workspace. "
                f"Contact your admin to enable it."
            )
            self._fire_and_forget(
                self.log_training(tenant_id, clean_text, None, None, was_disabled_skill=True)
            )
            return (assistant_text, 0, "disabled_skill", False)

        # === TIER 3: Full Claude with tools ===
        self._stats["ai_routed"] += 1
        tools = self._skills.get_tools_for_tenant(type("C", (), {"tenant": tenant})())
        messages = history + [{"role": "user", "content": clean_text}]
        response = await provider_ai.chat(active_model, system, messages, tools)

        if response.has_tool_use:
            tc = response.tool_calls[0]

            result = await self._skill_invoker(
                tenant_id,
                tc.tool_name,
                tc.tool_params,
                conversation_id,
                f"ai-{conversation_id}",
                "dashboard",
                user_email or "user",
            )

            # skill_invoker may return a Response (async dispatch)
            if isinstance(result, Response):
                self._stats["ai_routed"] += 1
                self._fire_and_forget(
                    self.log_training(
                        tenant_id,
                        clean_text,
                        tc.tool_name,
                        tc.tool_params.get("action"),
                    )
                )
                return result

            skill_result = result or {"error": "No result"}
            self._fire_and_forget(
                self.log_training(
                    tenant_id,
                    clean_text,
                    tc.tool_name,
                    tc.tool_params.get("action"),
                )
            )

            # Audio results: return directly
            if skill_result.get("type") == "audio":
                roundtrip_sec = round(time.time() - request_start, 1)
                await self._memory.save_turn(
                    tenant_id,
                    conversation_id,
                    clean_text,
                    skill_result.get("text", ""),
                    metadata={"route": "ai", "skill": tc.tool_name},
                )
                return JSONResponse(
                    {
                        "text": skill_result.get("text", ""),
                        "audio": {
                            "audio_b64": skill_result.get("audio_b64", ""),
                            "audio_url": skill_result.get("audio_url", ""),
                            "format": skill_result.get("format", "wav"),
                        },
                        "conversation_id": conversation_id,
                        "tokens": 0,
                        "route": "ai",
                        "skill": tc.tool_name,
                        "raw": False,
                        "model": "",
                        "roundtrip_sec": roundtrip_sec,
                    }
                )

            if is_raw and engine and engine.supports_raw(tc.tool_name):
                self._stats["raw"] += 1
                return (
                    _format_raw_json(skill_result),
                    response.input_tokens + response.output_tokens,
                    "ai",
                    True,
                )

            messages_with_tool = messages + [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tc.tool_use_id,
                            "name": tc.tool_name,
                            "input": tc.tool_params,
                        }
                    ],
                }
            ]
            final = await provider_ai.chat_with_tool_result(
                active_model,
                system,
                messages_with_tool,
                tools,
                tc.tool_use_id,
                skill_result,
            )
            assistant_text = final.text or "Got data but couldn't format."
            total_tokens = (
                response.input_tokens
                + response.output_tokens
                + final.input_tokens
                + final.output_tokens
            )
            return (assistant_text, total_tokens, "ai", False)

        # No tool use — freeform AI response
        self._fire_and_forget(self.log_training(tenant_id, clean_text, None, None))
        assistant_text = response.text or "Not sure how to help."
        total_tokens = response.input_tokens + response.output_tokens
        return (assistant_text, total_tokens, "ai", False)
