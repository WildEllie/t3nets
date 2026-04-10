"""Shared Teams and Telegram webhook handlers.

Extracted from ``adapters/aws/server.py`` and ``adapters/local/dev_server.py``.
The dispatch/routing logic is identical across servers; only the adapter
*resolution* differs (DynamoDB lookup vs. env-var/SQLite lookup).  Each server
provides its own resolver callable at construction time.

WhatsApp is intentionally **not** included here — it has no local equivalent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from adapters.shared.multi_provider import MultiAIProvider
from adapters.shared.server_utils import _format_raw_json, _strip_metadata
from agent.channels.teams import TeamsAdapter
from agent.channels.telegram import TelegramAdapter
from agent.interfaces.ai_provider import AIProvider
from agent.interfaces.conversation_store import ConversationStore
from agent.interfaces.event_bus import EventBus
from agent.models.message import ChannelType, OutboundMessage
from agent.router.compiled_engine import CompiledRuleEngine, is_conversational, strip_raw_flag
from agent.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

# Type aliases for the resolver callables each server must supply.
TeamsResolverT = Callable[[str], Awaitable[TeamsAdapter | None]]
TelegramResolverT = Callable[[str], Awaitable[TelegramAdapter | None]]

# Type alias for the tenant resolver used inside message handlers.
# Given (channel, channel_key) -> Tenant (or raises).
TenantByChannelT = Callable[[str, str], Awaitable[Any]]

# Callable that resolves (tenant) -> (provider_name, api_model_id, short_name)
ModelResolverT = Callable[[Any], tuple[str, str, str]]

# Callable for skill invocation — same pattern as chat.py
SkillInvokerT = Callable[
    [str, str, dict[str, Any], str, str, str, str],
    Awaitable[None],
]

# Optional async-skill dispatcher (AWS only); when provided the handler
# delegates to it instead of running the synchronous invoke+format path.
AsyncSkillHandlerT = Callable[..., None]

# Background-task set to prevent GC of fire-and-forget coroutines.
_bg_tasks: set[asyncio.Task[None]] = set()


def _fire_and_forget(coro: Any) -> None:  # type: ignore[type-arg]
    task: asyncio.Task[None] = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


class WebhookHandlers:
    """Teams and Telegram webhook dispatch logic.

    Parameters
    ----------
    ai:
        Multi-provider AI dispatcher.
    memory:
        Conversation store for history retrieval and turn persistence.
    bus:
        Event bus for synchronous skill invocations.
    skills:
        Skill registry — provides tool definitions and skill metadata.
    stats:
        Shared mutable stats dict (``conversational``, ``rule_routed``,
        ``ai_routed``, ``raw``, ``total_tokens``, …).
    compiled_engines:
        ``{tenant_id: CompiledRuleEngine}`` dict — same cache used by chat.
    fallback_router:
        Trigger-based ``RuleBasedRouter`` used when no compiled engine exists.
    resolve_model:
        ``(tenant) -> (provider, model_id, short_name)``
    resolve_teams_adapter:
        ``(bot_app_id) -> TeamsAdapter | None``  (awaitable)
    resolve_telegram_adapter:
        ``(token_hash) -> TelegramAdapter | None``  (awaitable)
    resolve_tenant_by_channel:
        ``(channel, channel_key) -> Tenant``  (awaitable, raises on miss)
    log_training:
        Coroutine that logs a training example (fire-and-forget).
    enrich_match_params:
        Inject original user text into match params when the skill expects it.
    async_skill_handler:
        Optional AWS-only async skill dispatcher.  When ``None`` (local), the
        sync invoke path is always used.
    use_async_skills:
        Whether to attempt async dispatch (requires ``async_skill_handler``).
    event_bus:
        Optional secondary event bus for async dispatch (AWS only).
    """

    def __init__(
        self,
        *,
        ai: MultiAIProvider,
        memory: ConversationStore,
        bus: EventBus,
        skills: SkillRegistry,
        stats: dict[str, Any],
        compiled_engines: dict[str, CompiledRuleEngine],
        fallback_router: Any | None,
        resolve_model: ModelResolverT,
        resolve_teams_adapter: TeamsResolverT,
        resolve_telegram_adapter: TelegramResolverT,
        resolve_tenant_by_channel: TenantByChannelT,
        log_training: Callable[..., Awaitable[None]],
        enrich_match_params: Callable[[Any, str], None],
        async_skill_handler: AsyncSkillHandlerT | None = None,
        use_async_skills: bool = False,
        event_bus: EventBus | None = None,
        pending_store: Any | None = None,
    ) -> None:
        self._ai = ai
        self._memory = memory
        self._bus = bus
        self._skills = skills
        self._stats = stats
        self._engines = compiled_engines
        self._fallback = fallback_router
        self._resolve_model = resolve_model
        self._resolve_teams = resolve_teams_adapter
        self._resolve_telegram = resolve_telegram_adapter
        self._resolve_tenant = resolve_tenant_by_channel
        self._log_training = log_training
        self._enrich = enrich_match_params
        self._async_handler = async_skill_handler
        self._use_async = use_async_skills
        self._event_bus = event_bus
        self._pending_store = pending_store

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_engine(self, tenant_id: str) -> CompiledRuleEngine | None:
        return self._engines.get(tenant_id)

    # ------------------------------------------------------------------
    # Teams webhook
    # ------------------------------------------------------------------

    async def handle_teams_webhook(self, request: Request) -> Response:
        """POST /api/channels/teams/webhook"""
        try:
            body_bytes = await request.body()
            activity = json.loads(body_bytes) if body_bytes else {}
            activity_type = activity.get("type", "")
            logger.info(f"Teams webhook: type={activity_type}")

            recipient_id = activity.get("recipient", {}).get("id", "")
            teams_adapter = await self._resolve_teams(recipient_id)

            if not teams_adapter:
                logger.warning(f"No Teams adapter for recipient {recipient_id}")
                return JSONResponse({"error": "Bot not configured"}, status_code=401)

            auth_header = request.headers.get("authorization", "")
            if auth_header and not teams_adapter.validate_webhook(
                dict(request.headers), body_bytes
            ):
                logger.warning("Teams webhook JWT validation failed")
                return JSONResponse({"error": "Unauthorized"}, status_code=401)

            if activity_type == "message" and TeamsAdapter.is_message_activity(activity):
                await self._handle_teams_message(teams_adapter, activity)
            elif TeamsAdapter.is_bot_added(activity):
                await self._handle_teams_bot_added(teams_adapter, activity)
            else:
                logger.debug(f"Ignoring Teams activity type: {activity_type}")

            return JSONResponse({"ok": True})
        except Exception as e:
            logger.exception("Teams webhook error")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_teams_message(
        self, teams_adapter: TeamsAdapter, activity: dict[str, Any]
    ) -> None:
        message = teams_adapter.parse_inbound(activity)
        text = message.text
        if not text:
            return

        recipient_id = activity.get("recipient", {}).get("id", "")
        try:
            tenant = await self._resolve_tenant("teams", recipient_id)
        except Exception:
            logger.warning(f"No tenant mapped for Teams bot {recipient_id}")
            return

        tenant_id = tenant.tenant_id
        conversation_id = f"teams-{message.conversation_id}"
        logger.info(f"Teams [{tenant_id}]: {text[:100]}")

        await teams_adapter.send_typing_indicator(message.conversation_id)

        clean_text, is_raw = strip_raw_flag(text)
        active_provider, active_model, model_short_name = self._resolve_model(tenant)
        provider_ai = self._ai.for_provider(active_provider)
        system = (
            f"You are an AI assistant for {tenant.name} on the T3nets platform.\n"
            "Be direct, helpful, and action-oriented. Flag risks early. "
            "Suggest actions.\nYou are communicating via Microsoft Teams. "
            "Keep responses clear and well-formatted.\n"
            "When you have data to present, format it clearly with structure."
        )
        history = _strip_metadata(await self._memory.get_conversation(tenant_id, conversation_id))

        engine = self._get_engine(tenant_id)

        if not is_raw and is_conversational(clean_text):
            self._stats["conversational"] += 1
            messages = history + [{"role": "user", "content": clean_text}]
            response = await provider_ai.chat(active_model, system, messages, [])
            assistant_text = response.text or "Hey! How can I help?"
            total_tokens = response.input_tokens + response.output_tokens
        else:
            assistant_text, total_tokens = await self._route_channel_message(
                channel="teams",
                channel_type=ChannelType.TEAMS,
                engine=engine,
                clean_text=clean_text,
                is_raw=is_raw,
                tenant=tenant,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                raw_conversation_id=message.conversation_id,
                channel_user_id=message.channel_user_id,
                active_provider=active_provider,
                active_model=active_model,
                model_short_name=model_short_name,
                provider_ai=provider_ai,
                system=system,
                history=history,
                adapter=teams_adapter,
            )

        self._stats["total_tokens"] += total_tokens
        await self._memory.save_turn(
            tenant_id,
            conversation_id,
            clean_text,
            assistant_text,
            metadata={
                "route": "teams",
                "model": model_short_name,
                "tokens": total_tokens,
                "channel": "teams",
            },
        )
        outbound = OutboundMessage(
            channel=ChannelType.TEAMS,
            conversation_id=message.conversation_id,
            recipient_id=message.channel_user_id,
            text=assistant_text,
        )
        await teams_adapter.send_response(outbound)

    async def _handle_teams_bot_added(
        self, teams_adapter: TeamsAdapter, activity: dict[str, Any]
    ) -> None:
        conversation_id = activity.get("conversation", {}).get("id", "")
        if not conversation_id:
            return
        service_url = activity.get("serviceUrl", "")
        if service_url:
            teams_adapter._service_urls[conversation_id] = service_url.rstrip("/")

        welcome = OutboundMessage(
            channel=ChannelType.TEAMS,
            conversation_id=conversation_id,
            recipient_id="",
            text=(
                "Hi! I'm your T3nets assistant. "
                "Ask me about sprint status, release notes, and more. "
                "Type **help** to see what I can do."
            ),
        )
        await teams_adapter.send_response(welcome)

    # ------------------------------------------------------------------
    # Telegram webhook
    # ------------------------------------------------------------------

    async def handle_telegram_webhook(self, request: Request) -> Response:
        """POST /api/channels/telegram/webhook/{token_hash}

        Always returns 200 to Telegram (except auth failures) to prevent
        indefinite retry loops.
        """
        try:
            body_bytes = await request.body()
            update = json.loads(body_bytes) if body_bytes else {}

            token_hash = request.path_params.get("token_hash", "")
            telegram_adapter = await self._resolve_telegram(token_hash)
            if not telegram_adapter:
                logger.warning(f"No Telegram adapter for token hash {token_hash[:8]}...")
                return JSONResponse({"error": "Bot not configured"}, status_code=401)

            if not telegram_adapter.validate_webhook(dict(request.headers), body_bytes):
                logger.warning("Telegram webhook secret validation failed")
                return JSONResponse({"error": "Unauthorized"}, status_code=401)

            if TelegramAdapter.is_message_update(update):
                _fire_and_forget(self._handle_telegram_message(telegram_adapter, update))

        except Exception:
            logger.exception("Telegram webhook error")

        return JSONResponse({"ok": True})

    async def _handle_telegram_message(
        self, adapter: TelegramAdapter, update: dict[str, Any]
    ) -> None:
        message = adapter.parse_inbound(update)
        text = message.text
        if not text:
            return

        token_hash = hashlib.sha256(adapter.bot_token.encode()).hexdigest()[:16]
        try:
            tenant = await self._resolve_tenant("telegram", token_hash)
        except Exception:
            logger.warning(f"No tenant mapped for Telegram bot {token_hash[:8]}")
            return

        tenant_id = tenant.tenant_id
        conversation_id = f"tg-{message.conversation_id}"
        logger.info(f"Telegram [{tenant_id}]: {text[:100]}")

        await adapter.send_typing_indicator(message.conversation_id)

        clean_text, is_raw = strip_raw_flag(text)
        active_provider, active_model, model_short_name = self._resolve_model(tenant)
        provider_ai = self._ai.for_provider(active_provider)
        system = (
            f"You are an AI assistant for {tenant.name} on the T3nets "
            "platform.\nBe direct, helpful, and action-oriented. "
            "Flag risks early. Suggest actions.\nYou are communicating via "
            "Telegram. Keep responses concise and well-formatted.\n"
            "Use Markdown sparingly \u2014 Telegram supports "
            "*bold*, _italic_, and `code`."
        )
        history = _strip_metadata(await self._memory.get_conversation(tenant_id, conversation_id))

        engine = self._get_engine(tenant_id)

        if not is_raw and is_conversational(clean_text):
            self._stats["conversational"] += 1
            messages = history + [{"role": "user", "content": clean_text}]
            response = await provider_ai.chat(active_model, system, messages, [])
            assistant_text = response.text or "Hey! How can I help?"
            total_tokens = response.input_tokens + response.output_tokens
        else:
            assistant_text, total_tokens = await self._route_channel_message(
                channel="telegram",
                channel_type=ChannelType.TELEGRAM,
                engine=engine,
                clean_text=clean_text,
                is_raw=is_raw,
                tenant=tenant,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                raw_conversation_id=message.conversation_id,
                channel_user_id=message.channel_user_id,
                active_provider=active_provider,
                active_model=active_model,
                model_short_name=model_short_name,
                provider_ai=provider_ai,
                system=system,
                history=history,
                adapter=adapter,
            )

        self._stats["total_tokens"] += total_tokens
        await self._memory.save_turn(
            tenant_id,
            conversation_id,
            clean_text,
            assistant_text,
            metadata={
                "route": "telegram",
                "model": model_short_name,
                "tokens": total_tokens,
                "channel": "telegram",
            },
        )
        outbound = OutboundMessage(
            channel=ChannelType.TELEGRAM,
            conversation_id=message.conversation_id,
            recipient_id=message.channel_user_id,
            text=assistant_text,
        )
        await adapter.send_response(outbound)

    # ------------------------------------------------------------------
    # Shared Tier 1/2/3 routing for channel messages
    # ------------------------------------------------------------------

    async def _route_channel_message(
        self,
        *,
        channel: str,
        channel_type: ChannelType,
        engine: CompiledRuleEngine | None,
        clean_text: str,
        is_raw: bool,
        tenant: Any,
        tenant_id: str,
        conversation_id: str,
        raw_conversation_id: str,
        channel_user_id: str,
        active_provider: str,
        active_model: str,
        model_short_name: str,
        provider_ai: AIProvider,
        system: str,
        history: list[dict[str, Any]],
        adapter: Any,
    ) -> tuple[str, int]:
        """Run Tier 2 (rule) / Tier 3 (AI) routing for a channel message.

        Returns ``(assistant_text, total_tokens)``.
        """
        prefix = "tg" if channel == "telegram" else channel
        router = engine or self._fallback
        match = router.match(clean_text, tenant.settings.enabled_skills) if router else None

        if match:
            self._enrich(match, clean_text)

            # --- async dispatch (AWS only) ---
            if self._use_async and self._event_bus and self._pending_store:
                service_url = ""
                if channel == "teams":
                    service_url = adapter._service_urls.get(raw_conversation_id, "")
                self._async_handler(  # type: ignore[misc]
                    tenant_id=tenant_id,
                    channel=channel,
                    skill_name=match.skill_name,
                    params=match.params,
                    conversation_id=conversation_id,
                    reply_target=raw_conversation_id,
                    user_key=channel_user_id,
                    user_message=clean_text,
                    is_raw=is_raw,
                    route_type="rule",
                    model_id=active_model,
                    model_short_name=model_short_name,
                    service_url=service_url,
                )
                # Caller must detect (0, 0) sentinel and skip save_turn.
                # In practice the async handler returns early in both servers.
                return ("", 0)

            # --- sync dispatch ---
            request_id = f"{prefix}-rule-{conversation_id}"
            await self._bus.publish_skill_invocation(
                tenant_id,
                match.skill_name,
                match.params,
                conversation_id,
                request_id,
                channel,
                channel_user_id,
            )
            skill_result = self._bus.get_result(request_id) or {"error": "No result"}

            # Audio results: send directly, skip AI formatting
            if skill_result.get("type") == "audio":
                return self._build_audio_response(
                    skill_result=skill_result,
                    channel_type=channel_type,
                    raw_conversation_id=raw_conversation_id,
                    channel_user_id=channel_user_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    clean_text=clean_text,
                    route="rule",
                    skill_name=match.skill_name,
                    adapter=adapter,
                )

            if is_raw and engine and engine.supports_raw(match.skill_name):
                self._stats["raw"] += 1
                self._stats["rule_routed"] += 1
                return (_format_raw_json(skill_result), 0)

            self._stats["rule_routed"] += 1
            prompt = (
                f'{system}\n\nThe user asked: "{clean_text}"\n\n'
                f"Tool data:\n{json.dumps(skill_result, indent=2)}\n\n"
                f"Format this clearly."
            )
            messages = history + [{"role": "user", "content": prompt}]
            response = await provider_ai.chat(active_model, system, messages, [])
            return (
                response.text or "Got data but couldn't format.",
                response.input_tokens + response.output_tokens,
            )

        # --- disabled skill check ---
        if engine and (disabled_skill := engine.check_disabled_skill(clean_text)):
            skill_display = disabled_skill.replace("_", " ")
            assistant_text = (
                f"The {skill_display} feature isn't enabled for your "
                f"workspace. Contact your admin to enable it."
            )
            _fire_and_forget(
                self._log_training(tenant_id, clean_text, None, None, was_disabled_skill=True)
            )
            return (assistant_text, 0)

        # --- Tier 3: full AI with tools ---
        self._stats["ai_routed"] += 1
        tools = self._skills.get_tools_for_tenant(type("C", (), {"tenant": tenant})())
        messages = history + [{"role": "user", "content": clean_text}]
        response = await provider_ai.chat(active_model, system, messages, tools)

        if response.has_tool_use:
            tc = response.tool_calls[0]

            # --- async dispatch (AWS only) ---
            if self._use_async and self._event_bus and self._pending_store:
                service_url = ""
                if channel == "teams":
                    service_url = adapter._service_urls.get(raw_conversation_id, "")
                self._async_handler(  # type: ignore[misc]
                    tenant_id=tenant_id,
                    channel=channel,
                    skill_name=tc.tool_name,
                    params=tc.tool_params,
                    conversation_id=conversation_id,
                    reply_target=raw_conversation_id,
                    user_key=channel_user_id,
                    user_message=clean_text,
                    is_raw=is_raw,
                    route_type="ai",
                    model_id=active_model,
                    model_short_name=model_short_name,
                    service_url=service_url,
                )
                return ("", 0)

            # --- sync dispatch ---
            request_id = f"{prefix}-ai-{conversation_id}"
            await self._bus.publish_skill_invocation(
                tenant_id,
                tc.tool_name,
                tc.tool_params,
                conversation_id,
                request_id,
                channel,
                channel_user_id,
            )
            skill_result = self._bus.get_result(request_id) or {"error": "No result"}
            _fire_and_forget(
                self._log_training(
                    tenant_id,
                    clean_text,
                    tc.tool_name,
                    tc.tool_params.get("action"),
                )
            )

            # Audio results: send directly, skip AI formatting
            if skill_result.get("type") == "audio":
                return self._build_audio_response(
                    skill_result=skill_result,
                    channel_type=channel_type,
                    raw_conversation_id=raw_conversation_id,
                    channel_user_id=channel_user_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    clean_text=clean_text,
                    route="ai",
                    skill_name=tc.tool_name,
                    adapter=adapter,
                )

            if is_raw and engine and engine.supports_raw(tc.tool_name):
                self._stats["raw"] += 1
                return (
                    _format_raw_json(skill_result),
                    response.input_tokens + response.output_tokens,
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
            total_tokens = (
                response.input_tokens
                + response.output_tokens
                + final.input_tokens
                + final.output_tokens
            )
            return (final.text or "Got data but couldn't format.", total_tokens)

        # No tool use — plain text response
        _fire_and_forget(self._log_training(tenant_id, clean_text, None, None))
        return (
            response.text or "Not sure how to help.",
            response.input_tokens + response.output_tokens,
        )

    # ------------------------------------------------------------------
    # Audio result helper
    # ------------------------------------------------------------------

    def _build_audio_response(
        self,
        *,
        skill_result: dict[str, Any],
        channel_type: ChannelType,
        raw_conversation_id: str,
        channel_user_id: str,
        tenant_id: str,
        conversation_id: str,
        clean_text: str,
        route: str,
        skill_name: str,
        adapter: Any,
    ) -> tuple[str, int]:
        """Build and send an audio attachment response, then return sentinel.

        The caller should treat the returned ``("", 0)`` as a signal that
        the response has already been sent (including memory persistence).
        """
        assistant_text = skill_result.get("text", "")
        audio_att: dict[str, Any] = {
            "type": "audio",
            "format": skill_result.get("format", "wav"),
        }
        if skill_result.get("audio_url"):
            audio_att["audio_url"] = skill_result["audio_url"]
        if skill_result.get("audio_b64"):
            audio_att["audio_b64"] = skill_result["audio_b64"]

        outbound = OutboundMessage(
            channel=channel_type,
            conversation_id=raw_conversation_id,
            recipient_id=channel_user_id,
            text=assistant_text,
            attachments=[audio_att],
        )

        async def _send_and_save() -> None:
            await adapter.send_response(outbound)
            await self._memory.save_turn(
                tenant_id,
                conversation_id,
                clean_text,
                assistant_text,
                metadata={
                    "route": route,
                    "skill": skill_name,
                    "channel": channel_type.value
                    if hasattr(channel_type, "value")
                    else str(channel_type),
                },
            )

        _fire_and_forget(_send_and_save())
        # Sentinel: response already sent + saved; caller should skip.
        return ("", 0)
