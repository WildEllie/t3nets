"""
T3nets Local Development Server

A simple HTTP server that wires all local adapters together.
Uses HYBRID ROUTING:
  1. Conversational -> Claude direct (no tools, fewer tokens)
  2. Rule-matched -> Skill direct, then Claude formats (1 API call instead of 2)
  3. Ambiguous -> Full Claude with tools (2 API calls)

Debug mode:
  Append --raw to any message to skip Claude formatting and see raw skill output.
  Only works for skills that support it (e.g. sprint_status).

Usage:
    python -m adapters.local.dev_server
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
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from adapters.local.bootstrap import LocalServerState
from adapters.local.bootstrap import init as bootstrap_init
from adapters.local.server_helpers import (
    QueueBridge,
    extract_user_key,
    file_response,
    serve_static,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("t3nets.dev")

# Runtime state — populated by init() before the server starts.
state: LocalServerState = LocalServerState()


# ---------------------------------------------------------------------------
# Static page / asset routes
# ---------------------------------------------------------------------------


def _page(filename: str) -> Any:
    async def handler(request: Request) -> Response:
        return file_response(filename, "adapters/local")

    return handler


def _asset(filename: str, media_type: str) -> Any:
    async def handler(request: Request) -> Response:
        return serve_static(filename, media_type)

    return handler


async def practice_page(request: Request) -> Response:
    practice_name = request.path_params["practice"]
    page_slug = request.path_params["page"]
    assert state.practices is not None
    page_path = state.practices.get_page_path(practice_name, page_slug)
    if page_path and page_path.exists():
        return FileResponse(str(page_path), media_type="text/html")
    return Response(status_code=404, content="Practice page not found")


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------


async def sse_endpoint(request: Request) -> StreamingResponse:
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
            state.sse_manager.unregister(user_key, bridge)
            logger.info(f"SSE: client disconnected (user={user_key})")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Thin wrappers — delegate to handlers/APIs on `state`
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


def _dt(path: str) -> Any:
    """Route handler that resolves `state.<path>` and calls it with (request, default_tenant)."""
    parts = path.split(".")

    async def handler(request: Request) -> Response:
        target: Any = state
        for p in parts:
            target = getattr(target, p)
        result: Response = await target(request, state.default_tenant)
        return result

    return handler


async def handle_auth_config(request: Request) -> Response:
    return JSONResponse({"enabled": False, "client_id": "", "auth_domain": "", "user_pool_id": ""})


# ---------------------------------------------------------------------------
# Routes & middleware
# ---------------------------------------------------------------------------

routes = [
    # Static pages
    Route("/", _page("chat.html")),
    Route("/chat", _page("chat.html")),
    Route("/logo.png", _asset("logo.png", "image/png")),
    Route("/theme.css", _asset("theme.css", "text/css")),
    Route("/theme.js", _asset("theme.js", "application/javascript")),
    Route("/health", _page("health.html")),
    Route("/settings", _page("settings.html")),
    Route("/onboard", _page("onboard.html")),
    Route("/platform", _page("platform.html")),
    Route("/training", _page("training.html")),
    Route("/p/{practice}/{page}", practice_page),
    # API
    Route("/api/events", sse_endpoint),
    Route("/api/health", _d("health_handlers.handle_health_api")),
    Route("/api/settings", _dt("settings_handlers.get_settings"), methods=["GET"]),
    Route("/api/settings", _dt("settings_handlers.post_settings"), methods=["POST"]),
    Route("/api/history", _d("history")),
    Route("/api/auth/config", handle_auth_config),
    Route("/api/auth/me", _d("auth_me")),
    Route("/api/chat", _d("chat_handlers.handle_chat"), methods=["POST"]),
    Route("/api/clear", _d("chat_handlers.handle_clear"), methods=["POST"]),
    Route("/api/integrations", _dt("integration_handlers.list_integrations")),
    Route(
        "/api/integrations/{name}/test",
        _dt("integration_handlers.test_integration"),
        methods=["POST"],
    ),
    Route("/api/integrations/{name}", _dt("integration_handlers.get_integration"), methods=["GET"]),
    Route(
        "/api/integrations/{name}", _dt("integration_handlers.post_integration"), methods=["POST"]
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
    # Platform routes
    Route("/api/platform/tenants", _d("platform_api.list_tenants"), methods=["GET"]),
    Route("/api/platform/tenants", _d("platform_api.create_tenant"), methods=["POST"]),
    Route("/api/platform/tenants/{rest:path}", _d("platform_api.tenant_detail")),
    # Callback endpoint for async external services
    Route("/api/callback/{request_id}", _dt("practice_handlers.handle_callback"), methods=["POST"]),
    # Practice routes
    Route("/api/skill/{name}", _dt("practice_handlers.invoke_skill"), methods=["POST"]),
    Route("/api/practices", _dt("practice_handlers.list_practices"), methods=["GET"]),
    Route("/api/practices/pages", _dt("practice_handlers.list_practice_pages"), methods=["GET"]),
    Route("/api/practices/upload", _dt("practice_handlers.upload_practice"), methods=["POST"]),
    Route("/api/blobs/{key:path}", _d("blob_upload"), methods=["POST"]),
    Route("/api/blobs/{key:path}", _d("blob_read"), methods=["GET"]),
    # Admin routes
    Route("/api/admin/rules/{rest:path}", _d("rules_admin"), methods=["GET", "POST"]),
    Route(
        "/api/admin/training/{example_id}",
        _d("training_admin"),
        methods=["GET", "PATCH", "DELETE"],
    ),
    Route("/api/admin/training", _d("training_admin"), methods=["GET"]),
    Route("/api/admin/tenants", _d("admin_api.create_tenant"), methods=["POST"]),
    Route("/api/admin/tenants/{rest:path}", _d("admin_api.tenant_detail")),
]

middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )
]

app = Starlette(routes=routes, middleware=middleware)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def init(extra_practice_dirs: list[Path] | None = None) -> None:
    """Bootstrap server state — invoked from main()."""
    global state
    state = await bootstrap_init(extra_practice_dirs=extra_practice_dirs)


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="T3nets local dev server")
    parser.add_argument(
        "--extra-practice-dir",
        action="append",
        default=[],
        dest="extra_practice_dirs",
        help="Additional practice directory to load (can be repeated)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on (default: PORT env var or 8080)",
    )
    args = parser.parse_args(argv)

    extra_dirs = [Path(d).resolve() for d in args.extra_practice_dirs]
    asyncio.run(init(extra_practice_dirs=extra_dirs))

    port = args.port or int(os.getenv("PORT", "8080"))
    logger.info("")
    logger.info("  +======================================+")
    logger.info("  |  T3nets Dev Server                   |")
    logger.info("  |                                      |")
    logger.info(f"  |  Chat:   http://localhost:{port}       |")
    logger.info(f"  |  Health: http://localhost:{port}/health |")
    logger.info("  |                                      |")
    logger.info("  |  Routing: Rules -> Claude (hybrid)    |")
    logger.info("  |  Debug:   append --raw to messages   |")
    logger.info("  |  SSE:     /api/events (async push)   |")
    logger.info("  +======================================+")
    logger.info("")

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning", timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
