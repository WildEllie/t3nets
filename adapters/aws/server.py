"""
T3nets AWS Server Entrypoint

Same HTTP server as local, but wired to AWS adapters:
  - Bedrock instead of direct Anthropic API
  - DynamoDB instead of SQLite
  - Secrets Manager instead of .env
  - DirectBus (sync) or EventBridge→Lambda→SQS (async, Phase 3b)

Runs inside ECS Fargate container.

Usage:
    python -m adapters.aws.server
"""

import asyncio
import logging
import os
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from adapters.aws.bootstrap import ServerState
from adapters.aws.bootstrap import init as bootstrap_init
from adapters.aws.server_helpers import (
    QueueBridge,
    WebSocketEventMiddleware,
    extract_user_key,
    file_response,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("t3nets.aws")

# Runtime state — populated by init() before the server starts handling traffic.
state: ServerState = ServerState()


# ---------------------------------------------------------------------------
# Static pages
# ---------------------------------------------------------------------------


def _page(filename: str) -> Any:
    """Build a route handler that serves a static HTML file from adapters/local."""

    async def handler(request: Request) -> Response:
        return file_response(filename, "adapters/local")

    return handler


async def practice_page(request: Request) -> Response:
    practice_name = request.path_params["practice"]
    page_slug = request.path_params["page"]
    assert state.practices is not None
    page_path = state.practices.get_page_path(practice_name, page_slug)
    if page_path and page_path.exists():
        from starlette.responses import FileResponse

        return FileResponse(str(page_path), media_type="text/html")
    return Response(status_code=404, content="Practice page not found")


# ---------------------------------------------------------------------------
# SSE endpoint (only used when WS_MANAGEMENT_ENDPOINT is unset)
# ---------------------------------------------------------------------------


async def sse_endpoint(request: Request) -> Response:
    if state.sse_manager is None:
        return JSONResponse(
            {"error": "SSE not available — WebSocket transport is active"}, status_code=400
        )
    user_key = extract_user_key(request, state.default_tenant)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    bridge = QueueBridge(queue, loop)
    state.sse_manager.register(user_key, bridge)
    logger.info(f"SSE: client connected (user={user_key})")

    async def event_stream() -> AsyncGenerator[bytes, None]:
        yield b'event: connected\ndata: {"status": "ok"}\n\n'
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield data
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            state.sse_manager.unregister(user_key, bridge)  # type: ignore[union-attr]
            logger.info(f"SSE: client disconnected (user={user_key})")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Thin wrappers — delegate to shared handler instances on `state`
# ---------------------------------------------------------------------------


def _d(path: str) -> Any:
    """Route handler that resolves `state.<path>` (dotted) and calls it with request."""
    parts = path.split(".")

    async def handler(request: Request) -> Response:
        target: Any = state
        for p in parts:
            target = getattr(target, p)
        result: Response = await target(request)
        return result

    return handler


def _da(path: str) -> Any:
    """Route handler that resolves auth, then calls `state.<path>(request, tenant_id)`."""
    parts = path.split(".")

    async def handler(request: Request) -> Response:
        tenant_id, _ = await state.get_auth_info(request)
        target: Any = state
        for p in parts:
            target = getattr(target, p)
        result: Response = await target(request, tenant_id)
        return result

    return handler


# ---------------------------------------------------------------------------
# Routes & middleware
# ---------------------------------------------------------------------------

routes = [
    # Static pages
    Route("/", _page("chat.html")),
    Route("/chat", _page("chat.html")),
    Route("/health", _page("health.html")),
    Route("/settings", _page("settings.html")),
    Route("/login", _page("login.html")),
    Route("/callback", _page("callback.html")),
    Route("/onboard", _page("onboard.html")),
    Route("/platform", _page("platform.html")),
    Route("/training", _page("training.html")),
    # API
    Route("/api/events", sse_endpoint),
    Route("/api/health", _d("health_handlers.handle_health_api")),
    Route("/api/settings", _da("settings_handlers.get_settings"), methods=["GET"]),
    Route("/api/settings", _da("settings_handlers.post_settings"), methods=["POST"]),
    Route("/api/history", _d("history")),
    Route("/api/auth/config", _d("auth_api.config")),
    Route("/api/auth/me", _d("auth_api.me")),
    Route("/api/auth/login", _d("auth_api.login"), methods=["POST"]),
    Route("/api/auth/signup", _d("auth_api.signup"), methods=["POST"]),
    Route("/api/auth/confirm", _d("auth_api.confirm"), methods=["POST"]),
    Route("/api/auth/refresh", _d("auth_api.refresh"), methods=["POST"]),
    Route("/api/auth/forgot-password", _d("auth_api.forgot_password"), methods=["POST"]),
    Route("/api/auth/confirm-reset", _d("auth_api.confirm_reset"), methods=["POST"]),
    Route("/api/chat", _d("chat_handlers.handle_chat"), methods=["POST"]),
    Route("/api/clear", _d("chat_handlers.handle_clear"), methods=["POST"]),
    Route("/api/integrations", _da("integration_handlers.list_integrations")),
    Route(
        "/api/integrations/{name}/test",
        _da("integration_handlers.test_integration"),
        methods=["POST"],
    ),
    Route("/api/integrations/{name}", _da("integration_handlers.get_integration"), methods=["GET"]),
    Route(
        "/api/integrations/{name}", _da("integration_handlers.post_integration"), methods=["POST"]
    ),
    Route("/api/invitations/validate", _d("admin_api.validate_invitation")),
    Route("/api/invitations/accept", _d("admin_api.accept_invitation"), methods=["POST"]),
    Route(
        "/api/channels/teams/webhook",
        _d("webhook_handlers.handle_teams_webhook"),
        methods=["POST"],
    ),
    Route(
        "/api/channels/telegram/webhook/{token_hash}",
        _d("webhook_handlers.handle_telegram_webhook"),
        methods=["POST"],
    ),
    Route(
        "/api/channels/whatsapp/webhook/{token_hash}",
        _d("webhook_handlers.handle_whatsapp_webhook"),
        methods=["POST"],
    ),
    # Practices
    Route("/api/skill/{name}", _da("practice_handlers.invoke_skill"), methods=["POST"]),
    Route("/api/practices", _da("practice_handlers.list_practices")),
    Route("/api/practices/pages", _da("practice_handlers.list_practice_pages")),
    Route("/api/practices/upload", _da("practice_handlers.upload_practice"), methods=["POST"]),
    Route("/api/callback/{request_id}", _da("practice_handlers.handle_callback"), methods=["POST"]),
    Route("/p/{practice}/{page}", practice_page),
    # Admin and Platform (delegated to API objects via thread pool)
    Route("/api/admin/rules/{rest:path}", _d("rules_admin"), methods=["GET", "POST"]),
    Route(
        "/api/admin/{rest:path}",
        _d("admin_dispatch"),
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    ),
    Route(
        "/api/platform/{rest:path}",
        _d("platform_dispatch"),
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    ),
]

middleware = [
    Middleware(WebSocketEventMiddleware, state_getter=lambda: state),
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    ),
]

app = Starlette(routes=routes, middleware=middleware)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def init() -> None:
    """Bootstrap server state — invoked from main()."""
    global state
    state = await bootstrap_init()


def main() -> None:
    asyncio.run(init())

    port = int(os.getenv("PORT", "8080"))
    async_status = "ON (EventBridge→Lambda→SQS)" if state.event_bus else "OFF (DirectBus)"
    push_transport = "WebSocket" if state.ws_manager else "SSE"
    logger.info("")
    logger.info("  ╔══════════════════════════════════════════════╗")
    logger.info("  ║  T3nets AWS Server                           ║")
    logger.info(f"  ║  http://0.0.0.0:{port}                       ║")
    logger.info(f"  ║  Model: {state.bedrock_model_id[:30]}      ║")
    logger.info(f"  ║  Push:    {push_transport:<35}║")
    logger.info(f"  ║  Async:   {async_status:<35}║")
    logger.info("  ╚══════════════════════════════════════════════╝")
    logger.info("")

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
