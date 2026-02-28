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
from typing import Protocol

from adapters.aws.pending_requests import PendingRequestsStore

logger = logging.getLogger(__name__)


class PushClient(Protocol):
    """Common interface for SSEConnectionManager and WebSocketConnectionManager."""

    def send_event(self, user_key: str, event_type: str, data: dict) -> int: ...

    @property
    def connection_count(self) -> int: ...


def _run_async(coro):
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
        ai_provider=None,
        conversation_store=None,
        bedrock_model_id: str = "",
    ):
        self.sse = push_client
        self.pending = pending_store
        self.ai = ai_provider
        self.memory = conversation_store
        self._bedrock_model_id = bedrock_model_id

    def handle_result(self, message: dict) -> None:
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
        else:
            logger.warning(f"AsyncResultRouter: unknown channel '{reply_channel}'")

    def _route_dashboard(self, request_id, result, skill_name, pending_req):
        """Push result to dashboard via push client (SSE or WebSocket)."""
        user_key = pending_req.user_key if pending_req else ""
        is_raw = pending_req.is_raw if pending_req else False
        route_type = (pending_req.route_type if pending_req else "") or "rule"

        if not user_key:
            logger.warning(
                f"AsyncResultRouter: no user_key for dashboard result {request_id[:8]}"
            )
            return

        # For raw mode, send the result directly
        if is_raw:
            delivered = self.sse.send_event(user_key, "message", {
                "request_id": request_id,
                "text": json.dumps(result, indent=2),
                "raw": True,
                "skill": skill_name,
                "route": route_type,
                "tokens": 0,
                "model": "",
            })
        else:
            # Format with AI if available, otherwise send raw
            formatted_text, fmt_tokens, fmt_model = self._format_result(
                result, skill_name, pending_req,
            )
            delivered = self.sse.send_event(user_key, "message", {
                "request_id": request_id,
                "text": formatted_text,
                "raw": False,
                "skill": skill_name,
                "route": route_type,
                "tokens": fmt_tokens,
                "model": fmt_model,
            })

        # Save conversation turn with full metadata (survives page reload)
        if pending_req and self.memory:
            try:
                text = formatted_text if not is_raw else json.dumps(result, indent=2)
                save_tokens = fmt_tokens if not is_raw else 0
                save_model = fmt_model if not is_raw else ""
                import time as _time
                roundtrip_sec = round(_time.time() - pending_req.created_at, 1) if pending_req.created_at else 0
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
                            "timestamp": int(pending_req.created_at * 1000) if pending_req.created_at else 0,
                            "roundtrip_sec": roundtrip_sec,
                        },
                    )
                )
            except Exception as e:
                logger.error(f"AsyncResultRouter: failed to save turn: {e}")

        logger.info(
            f"AsyncResultRouter: delivered to {delivered} connection(s) "
            f"for user {user_key[:20]}"
        )

    def _route_teams(self, request_id, result, skill_name, pending_req, message):
        """Send result back to Teams via Bot Framework."""
        if not pending_req:
            logger.warning(f"AsyncResultRouter: no pending request for Teams {request_id[:8]}")
            return

        service_url = pending_req.service_url
        if not service_url:
            logger.error(
                f"AsyncResultRouter: no service_url for Teams result {request_id[:8]}"
            )
            return

        # Teams reply is handled by the TeamsAdapter — import here to avoid
        # circular imports and keep Lambda handler lightweight
        try:
            from agent.channels.teams import TeamsAdapter

            formatted_text, _, _ = self._format_result(result, skill_name, pending_req)
            # TeamsAdapter.send_proactive_message() would go here
            # For now, log that we'd send to Teams
            logger.info(
                f"AsyncResultRouter: would send Teams reply via {service_url} "
                f"to {pending_req.reply_target}"
            )
            # TODO: Implement Teams proactive messaging in Phase 3c
        except Exception as e:
            logger.exception(f"AsyncResultRouter: Teams routing failed: {e}")

    def _route_telegram(self, request_id, result, skill_name, pending_req, message):
        """Send result back to Telegram via Bot API."""
        if not pending_req:
            logger.warning(
                f"AsyncResultRouter: no pending request for Telegram {request_id[:8]}"
            )
            return

        try:
            from agent.channels.telegram import TelegramAdapter

            formatted_text, _, _ = self._format_result(result, skill_name, pending_req)
            # TelegramAdapter.send_message() would go here
            logger.info(
                f"AsyncResultRouter: would send Telegram reply to "
                f"chat {pending_req.reply_target}"
            )
            # TODO: Implement Telegram async reply in Phase 3c
        except Exception as e:
            logger.exception(f"AsyncResultRouter: Telegram routing failed: {e}")

    def _format_result(
        self, result: dict, skill_name: str, pending_req,
    ) -> tuple[str, int, str]:
        """
        Format a skill result into human-readable text.

        Returns (formatted_text, total_tokens, model_short_name).
        If AI provider is available, uses Claude to format the result.
        Otherwise, returns a simple JSON dump with 0 tokens.
        """
        if "error" in result:
            return (
                f"Sorry, the {skill_name} skill encountered an error: {result['error']}",
                0,
                "",
            )

        # Use tenant's model from the pending request, fall back to server default
        active_model = (
            (pending_req.model_id if pending_req else "")
            or self._bedrock_model_id
        )
        model_short = (pending_req.model_short_name if pending_req else "") or ""

        if self.ai and active_model:
            try:
                user_msg = pending_req.user_message if pending_req else ""
                prompt = (
                    f'The user asked: "{user_msg}"\n\n'
                    f"Tool data from {skill_name}:\n"
                    f"{json.dumps(result, indent=2)}\n\n"
                    "Format this clearly and concisely for the user."
                )
                response = _run_async(
                    self.ai.chat(
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
