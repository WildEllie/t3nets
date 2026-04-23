"""
Async Result Router — routes skill results from SQS to the appropriate channel.

This is the callback function passed to SQSResultPoller. When Lambda completes
a skill and publishes the result to SQS, this module:

1. Reads the pending request from DynamoDB (for channel context)
2. Routes the result to the correct channel:
   - Dashboard: push to browser via SSEConnectionManager or WebSocketConnectionManager
   - Teams: Bot Framework reply using stored service_url
   - Telegram: Telegram Bot API message

The AI formatting step (converting raw skill output into a human-friendly
message) is optional — handled here if the pending request wasn't --raw.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Coroutine, Optional, Protocol

from t3nets_sdk.contracts import pop_render_meta, strip_render_meta

from adapters.aws.pending_requests import PendingRequest, PendingRequestsStore
from agent.interfaces.ai_provider import AIProvider
from agent.interfaces.conversation_store import ConversationStore
from agent.interfaces.secrets_provider import SecretsProvider

logger = logging.getLogger(__name__)


class PushClient(Protocol):
    """Common interface for SSEConnectionManager and WebSocketConnectionManager."""

    def send_event(self, user_key: str, event_type: str, data: dict[str, Any]) -> int: ...

    @property
    def connection_count(self) -> int: ...


def _run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run an async coroutine from a synchronous background thread."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class AsyncResultRouter:
    """
    Routes async skill results to the correct channel.

    Initialized with references to shared server state (push client,
    AI provider, conversation store, etc.) so it can format and deliver
    responses.
    """

    def __init__(
        self,
        push_client: PushClient,
        pending_store: PendingRequestsStore,
        ai_provider: Optional[AIProvider] = None,
        conversation_store: Optional[ConversationStore] = None,
        bedrock_model_id: str = "",
        secrets_provider: Optional[SecretsProvider] = None,
    ) -> None:
        self.sse = push_client
        self.pending = pending_store
        self.ai = ai_provider
        self.memory = conversation_store
        self._bedrock_model_id = bedrock_model_id
        self.secrets = secrets_provider

    def handle_result(self, message: dict[str, Any]) -> None:
        """
        Route a skill result to the appropriate channel.
        Called by SQSResultPoller for each received message.

        Message format (from Lambda → SQS):
            {
                "request_id": "...",
                "tenant_id": "...",
                "skill_name": "...",
                "reply_channel": "dashboard|teams|telegram",
                "reply_target": "...",
                "session_id": "...",
                "result": { ... }
            }
        """
        request_id = message.get("request_id", "")
        reply_channel = message.get("reply_channel", "")
        result = message.get("result", {})
        skill_name = message.get("skill_name", "")

        logger.info(
            f"AsyncResultRouter: routing result for "
            f"skill={skill_name}, channel={reply_channel}, "
            f"request={request_id[:8]}"
        )

        # Look up the pending request for extra context (user_key, service_url, etc.)
        pending_req = self.pending.get(request_id)

        if reply_channel == "dashboard":
            self._route_dashboard(request_id, result, skill_name, pending_req)
        elif reply_channel == "teams":
            self._route_teams(request_id, result, skill_name, pending_req, message)
        elif reply_channel == "telegram":
            self._route_telegram(request_id, result, skill_name, pending_req, message)
        elif reply_channel == "whatsapp":
            self._route_whatsapp(request_id, result, skill_name, pending_req, message)
        else:
            logger.warning(f"AsyncResultRouter: unknown channel '{reply_channel}'")

    def _route_dashboard(
        self,
        request_id: str,
        result: dict[str, Any],
        skill_name: str,
        pending_req: Optional[PendingRequest],
    ) -> None:
        """Push result to dashboard via push client (SSE or WebSocket)."""
        user_key = pending_req.user_key if pending_req else ""
        is_raw = pending_req.is_raw if pending_req else False
        route_type = (pending_req.route_type if pending_req else "") or "rule"

        if not user_key:
            logger.warning(f"AsyncResultRouter: no user_key for dashboard result {request_id[:8]}")
            return

        # For audio results, skip AI formatting and send directly
        is_audio = result.get("type") == "audio"
        if is_audio:
            delivered = self.sse.send_event(
                user_key,
                "message",
                {
                    "request_id": request_id,
                    "text": result.get("text", ""),
                    "audio": {
                        "audio_b64": result.get("audio_b64", ""),
                        "audio_url": result.get("audio_url", ""),
                        "format": result.get("format", "wav"),
                    },
                    "raw": False,
                    "skill": skill_name,
                    "route": route_type,
                    "tokens": 0,
                    "model": "",
                },
            )
        # For raw mode, send the result directly (minus skill-render metadata)
        elif is_raw:
            delivered = self.sse.send_event(
                user_key,
                "message",
                {
                    "request_id": request_id,
                    "text": json.dumps(strip_render_meta(result), indent=2),
                    "raw": True,
                    "skill": skill_name,
                    "route": route_type,
                    "tokens": 0,
                    "model": "",
                },
            )
        else:
            # Format with AI if available, otherwise send raw
            formatted_text, fmt_tokens, fmt_model = self._format_result(
                result,
                skill_name,
                pending_req,
            )
            delivered = self.sse.send_event(
                user_key,
                "message",
                {
                    "request_id": request_id,
                    "text": formatted_text,
                    "raw": False,
                    "skill": skill_name,
                    "route": route_type,
                    "tokens": fmt_tokens,
                    "model": fmt_model,
                },
            )

        # Save conversation turn with full metadata (survives page reload)
        if pending_req and self.memory:
            try:
                text = (
                    formatted_text
                    if not is_raw
                    else json.dumps(strip_render_meta(result), indent=2)
                )
                save_tokens = fmt_tokens if not is_raw else 0
                save_model = fmt_model if not is_raw else ""
                import time as _time

                roundtrip_sec = (
                    round(_time.time() - pending_req.created_at, 1) if pending_req.created_at else 0
                )
                _run_async(
                    self.memory.save_turn(
                        pending_req.tenant_id,
                        pending_req.conversation_id,
                        pending_req.user_message,
                        text,
                        metadata={
                            "route": route_type,
                            "skill": skill_name,
                            "tokens": save_tokens,
                            "model": save_model,
                            "user_email": pending_req.user_key,
                            "timestamp": int(pending_req.created_at * 1000)
                            if pending_req.created_at
                            else 0,
                            "roundtrip_sec": roundtrip_sec,
                        },
                    )
                )
            except Exception as e:
                logger.error(f"AsyncResultRouter: failed to save turn: {e}")

        logger.info(
            f"AsyncResultRouter: delivered to {delivered} connection(s) for user {user_key[:20]}"
        )

    def _route_teams(
        self,
        request_id: str,
        result: dict[str, Any],
        skill_name: str,
        pending_req: Optional[PendingRequest],
        message: dict[str, Any],
    ) -> None:
        """Send result back to Teams via Bot Framework."""
        from agent.channels.teams import TeamsAdapter
        from agent.models.message import ChannelType, OutboundMessage

        if not pending_req:
            logger.warning(f"AsyncResultRouter: no pending request for Teams {request_id[:8]}")
            return

        service_url = pending_req.service_url
        if not service_url:
            logger.error(f"AsyncResultRouter: no service_url for Teams result {request_id[:8]}")
            return

        try:
            # Load Teams credentials from Secrets Manager
            if not self.secrets:
                logger.error("AsyncResultRouter: no secrets provider configured")
                return
            creds = _run_async(self.secrets.get(pending_req.tenant_id, "teams"))
            app_id = creds.get("app_id", "")
            app_secret = creds.get("app_secret", "")
            if not app_id or not app_secret:
                logger.error(
                    f"AsyncResultRouter: missing Teams credentials for "
                    f"tenant {pending_req.tenant_id}"
                )
                return

            # Create adapter and inject cached service_url
            adapter = TeamsAdapter(app_id, app_secret)
            adapter._service_urls[pending_req.reply_target] = service_url

            # Check for audio result — skip AI formatting
            is_audio = result.get("type") == "audio"
            is_raw = pending_req.is_raw

            if is_audio:
                formatted_text = result.get("text", "")
                fmt_tokens, fmt_model = 0, ""
                teams_audio: dict[str, Any] = {
                    "type": "audio",
                    "format": result.get("format", "wav"),
                }
                if result.get("audio_url"):
                    teams_audio["audio_url"] = result["audio_url"]
                if result.get("audio_b64"):
                    teams_audio["audio_b64"] = result["audio_b64"]
                attachments = [teams_audio]
            elif is_raw:
                formatted_text = json.dumps(strip_render_meta(result), indent=2)
                fmt_tokens, fmt_model = 0, ""
                attachments = []
            else:
                formatted_text, fmt_tokens, fmt_model = self._format_result(
                    result,
                    skill_name,
                    pending_req,
                )
                attachments = []

            # Send response
            outbound = OutboundMessage(
                channel=ChannelType.TEAMS,
                conversation_id=pending_req.reply_target,
                recipient_id="",
                text=formatted_text,
                attachments=attachments,
            )
            _run_async(adapter.send_response(outbound))

            # Save conversation turn
            if self.memory:
                import time as _time

                route_type = pending_req.route_type or "rule"
                roundtrip_sec = (
                    round(_time.time() - pending_req.created_at, 1) if pending_req.created_at else 0
                )
                _run_async(
                    self.memory.save_turn(
                        pending_req.tenant_id,
                        pending_req.conversation_id,
                        pending_req.user_message,
                        formatted_text,
                        metadata={
                            "route": route_type,
                            "skill": skill_name,
                            "tokens": fmt_tokens,
                            "model": fmt_model,
                            "channel": "teams",
                            "roundtrip_sec": roundtrip_sec,
                        },
                    )
                )

            logger.info(
                f"AsyncResultRouter: Teams reply sent via {service_url} "
                f"to {pending_req.reply_target[:30]}"
            )
        except Exception as e:
            logger.exception(f"AsyncResultRouter: Teams routing failed: {e}")

    def _route_telegram(
        self,
        request_id: str,
        result: dict[str, Any],
        skill_name: str,
        pending_req: Optional[PendingRequest],
        message: dict[str, Any],
    ) -> None:
        """Send result back to Telegram via Bot API."""
        from agent.channels.telegram import TelegramAdapter
        from agent.models.message import ChannelType, OutboundMessage

        if not pending_req:
            logger.warning(f"AsyncResultRouter: no pending request for Telegram {request_id[:8]}")
            return

        try:
            # Load Telegram credentials from Secrets Manager
            if not self.secrets:
                logger.error("AsyncResultRouter: no secrets provider configured")
                return
            creds = _run_async(self.secrets.get(pending_req.tenant_id, "telegram"))
            bot_token = creds.get("bot_token", "")
            if not bot_token:
                logger.error(
                    f"AsyncResultRouter: missing Telegram bot_token for "
                    f"tenant {pending_req.tenant_id}"
                )
                return

            adapter = TelegramAdapter(bot_token)

            # Check for audio result — skip AI formatting
            is_audio = result.get("type") == "audio"
            is_raw = pending_req.is_raw

            if is_audio:
                formatted_text = result.get("text", "")
                fmt_tokens, fmt_model = 0, ""
                audio_att: dict[str, Any] = {"type": "audio", "format": result.get("format", "wav")}
                if result.get("audio_url"):
                    audio_att["audio_url"] = result["audio_url"]
                if result.get("audio_b64"):
                    audio_att["audio_b64"] = result["audio_b64"]
                attachments = [audio_att]
            elif is_raw:
                formatted_text = json.dumps(strip_render_meta(result), indent=2)
                fmt_tokens, fmt_model = 0, ""
                attachments = []
            else:
                formatted_text, fmt_tokens, fmt_model = self._format_result(
                    result,
                    skill_name,
                    pending_req,
                )
                attachments = []

            # Truncate for Telegram's 4096 char limit
            if len(formatted_text) > 4096:
                formatted_text = formatted_text[:4090] + "\n..."

            # Send response
            outbound = OutboundMessage(
                channel=ChannelType.TELEGRAM,
                conversation_id=pending_req.reply_target,
                recipient_id="",
                text=formatted_text,
                attachments=attachments,
            )
            sent = _run_async(adapter.send_response(outbound))
            if not sent:
                logger.error(
                    f"AsyncResultRouter: Telegram send_response returned False for {request_id[:8]}"
                )

            # Save conversation turn
            if self.memory:
                import time as _time

                route_type = pending_req.route_type or "rule"
                roundtrip_sec = (
                    round(_time.time() - pending_req.created_at, 1) if pending_req.created_at else 0
                )
                _run_async(
                    self.memory.save_turn(
                        pending_req.tenant_id,
                        pending_req.conversation_id,
                        pending_req.user_message,
                        formatted_text,
                        metadata={
                            "route": route_type,
                            "skill": skill_name,
                            "tokens": fmt_tokens,
                            "model": fmt_model,
                            "channel": "telegram",
                            "roundtrip_sec": roundtrip_sec,
                        },
                    )
                )

            logger.info(
                f"AsyncResultRouter: Telegram reply sent to chat {pending_req.reply_target}"
            )
        except Exception as e:
            logger.exception(f"AsyncResultRouter: Telegram routing failed: {e}")

    def _route_whatsapp(
        self,
        request_id: str,
        result: dict[str, Any],
        skill_name: str,
        pending_req: Optional[PendingRequest],
        message: dict[str, Any],
    ) -> None:
        """Send result back to WhatsApp via Whapi.cloud API."""
        from agent.channels.whatsapp import WhatsAppAdapter
        from agent.models.message import ChannelType, OutboundMessage

        if not pending_req:
            logger.warning(f"AsyncResultRouter: no pending request for WhatsApp {request_id[:8]}")
            return

        try:
            if not self.secrets:
                logger.error("AsyncResultRouter: no secrets provider configured")
                return
            creds = _run_async(self.secrets.get(pending_req.tenant_id, "whatsapp"))
            api_token = creds.get("api_token", "")
            if not api_token:
                logger.error(
                    f"AsyncResultRouter: missing WhatsApp api_token for "
                    f"tenant {pending_req.tenant_id}"
                )
                return

            adapter = WhatsAppAdapter(api_token)

            is_audio = result.get("type") == "audio"
            is_raw = pending_req.is_raw

            if is_audio:
                formatted_text = result.get("text", "")
                fmt_tokens, fmt_model = 0, ""
                audio_att: dict[str, Any] = {"type": "audio", "format": result.get("format", "wav")}
                if result.get("audio_url"):
                    audio_att["audio_url"] = result["audio_url"]
                # Whapi.cloud voice needs a URL — skip base64
                attachments = [audio_att] if audio_att.get("audio_url") else []
            elif is_raw:
                formatted_text = json.dumps(strip_render_meta(result), indent=2)
                fmt_tokens, fmt_model = 0, ""
                attachments = []
            else:
                formatted_text, fmt_tokens, fmt_model = self._format_result(
                    result,
                    skill_name,
                    pending_req,
                )
                attachments = []

            outbound = OutboundMessage(
                channel=ChannelType.WHATSAPP,
                conversation_id=pending_req.reply_target,
                recipient_id="",
                text=formatted_text,
                attachments=attachments,
            )
            sent = _run_async(adapter.send_response(outbound))
            if not sent:
                logger.error(
                    f"AsyncResultRouter: WhatsApp send_response returned False for {request_id[:8]}"
                )

            if self.memory:
                import time as _time

                route_type = pending_req.route_type or "rule"
                roundtrip_sec = (
                    round(_time.time() - pending_req.created_at, 1) if pending_req.created_at else 0
                )
                _run_async(
                    self.memory.save_turn(
                        pending_req.tenant_id,
                        pending_req.conversation_id,
                        pending_req.user_message,
                        formatted_text,
                        metadata={
                            "route": route_type,
                            "skill": skill_name,
                            "tokens": fmt_tokens,
                            "model": fmt_model,
                            "channel": "whatsapp",
                            "roundtrip_sec": roundtrip_sec,
                        },
                    )
                )

            logger.info(
                f"AsyncResultRouter: WhatsApp reply sent to chat {pending_req.reply_target}"
            )
        except Exception as e:
            logger.exception(f"AsyncResultRouter: WhatsApp routing failed: {e}")

    def _format_result(
        self,
        result: dict[str, Any],
        skill_name: str,
        pending_req: Optional[PendingRequest],
    ) -> tuple[str, int, str]:
        """Format a skill result into human-readable text.

        Precedence (set by the skill worker via `SkillResult`):
            1. Worker-rendered `text`  → sent verbatim, zero tokens.
            2. Worker-supplied `render_prompt` → AI formatter uses it.
            3. Neither → legacy generic "format this clearly" prompt.

        Mutates `result` to strip the render-metadata keys so downstream
        JSON dumps and conversation storage see clean data.
        """
        if "error" in result:
            # Make sure the failure path doesn't leak meta keys either.
            strip_render_meta(result)  # no-op copy, but defends future changes
            return (
                f"Sorry, the {skill_name} skill encountered an error: {result['error']}",
                0,
                "",
            )

        worker_text, render_prompt = pop_render_meta(result)

        # Skill rendered its own output — send verbatim.
        if worker_text:
            return (worker_text, 0, "")

        # Use tenant's model from the pending request, fall back to server default
        active_model = (pending_req.model_id if pending_req else "") or self._bedrock_model_id
        model_short = (pending_req.model_short_name if pending_req else "") or ""

        if self.ai and active_model:
            try:
                user_msg = pending_req.user_message if pending_req else ""
                instruction = render_prompt or "Format this clearly and concisely for the user."
                prompt = (
                    f'The user asked: "{user_msg}"\n\n'
                    f"Tool data from {skill_name}:\n"
                    f"{json.dumps(result, indent=2)}\n\n"
                    f"{instruction}"
                )
                provider = (
                    self.ai.for_provider("bedrock") if hasattr(self.ai, "for_provider") else self.ai
                )
                response = _run_async(
                    provider.chat(
                        active_model,
                        "You are a helpful assistant. Format the data clearly.",
                        [{"role": "user", "content": prompt}],
                        [],
                    )
                )
                tokens = response.input_tokens + response.output_tokens
                return (
                    response.text or json.dumps(result, indent=2),
                    tokens,
                    model_short,
                )
            except Exception as e:
                logger.warning(f"AsyncResultRouter: AI formatting failed: {e}")

        # Fallback: simple text format
        return (
            f"**{skill_name}** result:\n```json\n{json.dumps(result, indent=2)}\n```",
            0,
            "",
        )
