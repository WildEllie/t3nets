"""
T3nets AWS Async Skill Dispatcher.

Wraps the EventBridge → Lambda → SQS skill invocation path used when
``USE_ASYNC_SKILLS`` is enabled. The dashboard `_chat_skill_invoker` and the
channel webhooks both go through this class instead of writing to the in-process
DirectBus.

Two entry points:

* ``dispatch_chat`` — for dashboard chat. Returns a JSON ``Response`` with a
  ``request_id`` the client uses to subscribe to SSE/WebSocket updates.
* ``dispatch_channel`` — for Teams / Telegram / WhatsApp inbound messages.
  Returns ``None``; the result router later sends the response over the channel
  adapter once the Lambda completes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.responses import JSONResponse, Response

from adapters.aws.event_bridge_bus import EventBridgeBus
from adapters.aws.pending_requests import PendingRequest, PendingRequestsStore

logger = logging.getLogger("t3nets.aws.async_dispatch")


class AsyncSkillDispatcher:
    """AWS-specific async skill dispatcher (EventBridge → Lambda → SQS)."""

    def __init__(
        self,
        *,
        event_bus: EventBridgeBus,
        pending_store: PendingRequestsStore,
        stats: dict[str, int],
        fire_and_forget: Callable[[Any], None] | None = None,
        log_training: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._pending_store = pending_store
        self._stats = stats
        self._fire_and_forget = fire_and_forget
        self._log_training = log_training

    async def dispatch_chat(
        self,
        tenant_id: str,
        user_email: str,
        skill_name: str,
        params: dict[str, Any],
        conversation_id: str,
        user_message: str,
        is_raw: bool,
        route_type: str,
        model_id: str = "",
        model_short_name: str = "",
    ) -> Response:
        request_id = f"async-{uuid.uuid4().hex[:12]}"
        user_key = user_email or "anonymous"
        pending_req = PendingRequest(
            request_id=request_id,
            tenant_id=tenant_id,
            skill_name=skill_name,
            channel="dashboard",
            conversation_id=conversation_id,
            reply_target=user_key,
            user_key=user_key,
            is_raw=is_raw,
            user_message=user_message,
            model_id=model_id,
            model_short_name=model_short_name,
            route_type=route_type,
        )
        self._pending_store.create(pending_req)
        await self._event_bus.publish_skill_invocation(
            tenant_id,
            skill_name,
            params,
            conversation_id,
            request_id,
            "dashboard",
            user_key,
            is_raw=is_raw,
        )
        self._stats[f"{route_type}_routed"] += 1
        logger.info(
            f"Chat: async skill '{skill_name}' dispatched, "
            f"request={request_id[:8]}, user={user_key}"
        )
        if route_type == "ai" and self._fire_and_forget and self._log_training:
            self._fire_and_forget(
                self._log_training(tenant_id, user_message, skill_name, params.get("action"))
            )
        return JSONResponse(
            {
                "status": "processing",
                "request_id": request_id,
                "conversation_id": conversation_id,
                "skill": skill_name,
                "route": route_type,
                "model": "",
                "user_email": user_email,
            }
        )

    def dispatch_channel(
        self,
        *,
        tenant_id: str,
        channel: str,
        skill_name: str,
        params: dict[str, Any],
        conversation_id: str,
        reply_target: str,
        user_key: str,
        user_message: str,
        is_raw: bool,
        route_type: str,
        model_id: str = "",
        model_short_name: str = "",
        service_url: str = "",
    ) -> None:
        request_id = f"async-{channel}-{uuid.uuid4().hex[:12]}"
        pending_req = PendingRequest(
            request_id=request_id,
            tenant_id=tenant_id,
            skill_name=skill_name,
            channel=channel,
            conversation_id=conversation_id,
            reply_target=reply_target,
            user_key=user_key,
            is_raw=is_raw,
            user_message=user_message,
            model_id=model_id,
            model_short_name=model_short_name,
            route_type=route_type,
            service_url=service_url,
        )
        self._pending_store.create(pending_req)
        asyncio.ensure_future(
            self._event_bus.publish_skill_invocation(
                tenant_id, skill_name, params, conversation_id, request_id, channel, user_key
            )
        )
        self._stats[f"{route_type}_routed"] += 1
        logger.info(
            f"{channel.capitalize()}: async skill '{skill_name}' dispatched, "
            f"request={request_id[:8]}"
        )
