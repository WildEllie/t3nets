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
import base64
import hashlib
import json
import logging
import os
import sys
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
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

from adapters.local.anthropic_provider import AnthropicProvider
from adapters.local.direct_bus import DirectBus
from adapters.local.env_secrets import EnvSecretsProvider
from adapters.local.file_blob_store import FileStore
from adapters.local.local_pending_store import LocalPendingStore
from adapters.local.sqlite_rule_store import SQLiteRuleStore
from adapters.local.sqlite_store import SQLiteConversationStore
from adapters.local.sqlite_tenant_store import SQLiteTenantStore
from adapters.local.sqlite_training_store import SQLiteTrainingStore
from adapters.ollama.provider import OllamaProvider
from adapters.shared.handlers.chat import ChatHandlers
from adapters.shared.handlers.health import HealthHandlers
from adapters.shared.handlers.history import HistoryHandlers
from adapters.shared.handlers.integrations import IntegrationHandlers
from adapters.shared.handlers.practices import PracticeHandlers
from adapters.shared.handlers.settings import SettingsHandlers
from adapters.shared.handlers.training import TrainingHandlers
from adapters.shared.handlers.webhooks import WebhookHandlers
from adapters.shared.multi_provider import MultiAIProvider
from agent.channels.base import ChannelRegistry
from agent.channels.dashboard import DashboardAdapter
from agent.channels.teams import TeamsAdapter
from agent.channels.telegram import TelegramAdapter
from agent.errors.handler import ErrorHandler
from agent.models.ai_models import (
    DEFAULT_MODEL_ID,
    get_model,
    get_model_for_provider,
)
from agent.models.tenant import Invitation
from agent.practices.registry import PracticeRegistry
from agent.router.compiled_engine import CompiledRuleEngine
from agent.skills.registry import SkillRegistry
from agent.sse import SSEConnectionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("t3nets.dev")

# --- Global state (initialized in main) ---
ai: MultiAIProvider
memory: SQLiteConversationStore
tenants: SQLiteTenantStore
secrets: EnvSecretsProvider
skills: SkillRegistry
bus: DirectBus
rule_store: SQLiteRuleStore
training_store: SQLiteTrainingStore
error_handler: ErrorHandler
practices: PracticeRegistry
blobs: FileStore
pending_store: LocalPendingStore
started_at: float = 0.0

# Shared handler instances (initialized in init())
settings_handlers: SettingsHandlers
integration_handlers: IntegrationHandlers
chat_handlers: ChatHandlers
history_handlers: HistoryHandlers
training_handlers: TrainingHandlers
health_handlers: HealthHandlers
practice_handlers: PracticeHandlers
webhook_handlers: WebhookHandlers

# Per-tenant compiled rule engines (keyed by tenant_id)
_compiled_engines: dict[str, CompiledRuleEngine] = {}
_bg_tasks: set[asyncio.Task[None]] = set()  # strong refs to fire-and-forget tasks


def _fire_and_forget(coro: Any) -> None:  # type: ignore[type-arg]
    """Schedule a coroutine as a background task, retaining a strong reference
    so the GC cannot collect it before it completes."""
    task: asyncio.Task[None] = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


DEFAULT_TENANT = "local"
DEFAULT_CONVERSATION = "dashboard-default"
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "")

# Build number -- read from version.txt at startup
_version_path = Path(__file__).resolve().parent.parent.parent / "version.txt"
BUILD_NUMBER = _version_path.read_text().strip() if _version_path.exists() else "0"

# Stats for the session
stats: dict[str, int] = {
    "rule_routed": 0,
    "ai_routed": 0,
    "conversational": 0,
    "raw": 0,
    "errors": 0,
    "total_tokens": 0,
}

# --- SSE Connection Manager ---
sse_manager = SSEConnectionManager()


def _resolve_model(tenant: Any) -> tuple[str, str, str]:
    """Resolve the tenant's ai_model to (provider_name, api_model_id, short_name).

    Picks the first active provider that supports the requested model.
    Falls back gracefully when the selected model isn't available.
    """
    model_id = tenant.settings.ai_model or DEFAULT_MODEL_ID
    model = get_model(model_id)
    active = ai.active_providers  # e.g. ["anthropic", "ollama"]

    # Find the first active provider that supports this model
    selected_provider: str | None = None
    if model:
        for p in active:
            if p in model.providers:
                selected_provider = p
                break

    if not selected_provider:
        # Model not supported by any active provider -- use a safe fallback
        selected_provider = active[0]
        fallback_id = "llama-3.2-3b" if "ollama" in active else DEFAULT_MODEL_ID
        logger.warning(
            f"Model '{model_id}' not available for {active}, falling back to {fallback_id}"
        )
        model_id = fallback_id
        model = get_model(model_id)
    assert model is not None, f"Fallback model {model_id} not found in registry"

    api_id = get_model_for_provider(model_id, selected_provider)
    if not api_id:
        api_id = model.ollama_id if selected_provider == "ollama" else model.anthropic_id
    return selected_provider, api_id, model.short_name


def _file_response(filename: str, search_dir: str | None = None) -> Response:
    """Serve an HTML file from the project root or a subdirectory."""
    base = Path(__file__).parent.parent.parent
    path = base / search_dir / filename if search_dir else base / filename
    if path.exists():
        return FileResponse(str(path), media_type="text/html")
    return Response(status_code=404, content=f"{filename} not found")


def _extract_user_key(request: Request) -> str:
    """Extract user identity from JWT in query param or Authorization header."""
    user_key = DEFAULT_TENANT
    token = request.query_params.get("token")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if token:
        try:
            payload_b64 = token.split(".")[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            claims = json.loads(base64.urlsafe_b64decode(payload_b64))
            user_key = claims.get("email", "") or claims.get("sub", "") or user_key
        except Exception:
            pass
    return user_key


def _enrich_match_params(match: Any, clean_text: str) -> None:
    """Inject original user text into match params for skills that expect a 'text' field."""
    if not match:
        return
    skill_def = skills.get_skill(match.skill_name)
    if skill_def:
        schema_props = skill_def.parameters.get("properties", {})
        if "text" in schema_props and "text" not in match.params:
            match.params["text"] = clean_text


# ---------------------------------------------------------------------------
# SSE bridge
# ---------------------------------------------------------------------------


class _QueueBridge:
    """File-like object that forwards write() calls into an asyncio.Queue."""

    def __init__(self, queue: asyncio.Queue[bytes], loop: asyncio.AbstractEventLoop) -> None:
        self._queue = queue
        self._loop = loop

    def write(self, data: bytes | str) -> int:
        if isinstance(data, str):
            data = data.encode()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, data)
        return len(data)

    def flush(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Static page routes
# ---------------------------------------------------------------------------


async def homepage(request: Request) -> Response:
    return _file_response("chat.html", "adapters/local")


async def health_page(request: Request) -> Response:
    return _file_response("health.html", "adapters/local")


async def settings_page(request: Request) -> Response:
    return _file_response("settings.html", "adapters/local")


async def onboard_page(request: Request) -> Response:
    return _file_response("onboard.html", "adapters/local")


async def platform_page(request: Request) -> Response:
    return _file_response("platform.html", "adapters/local")


async def training_page(request: Request) -> Response:
    return _file_response("training.html", "adapters/local")


async def practice_page(request: Request) -> Response:
    """Serve a practice page at /p/{practice}/{page}."""
    practice_name = request.path_params["practice"]
    page_slug = request.path_params["page"]
    page_path = practices.get_page_path(practice_name, page_slug)
    if page_path and page_path.exists():
        return FileResponse(str(page_path), media_type="text/html")
    return Response(status_code=404, content="Practice page not found")


async def serve_logo(request: Request) -> Response:
    base = Path(__file__).parent.parent.parent
    path = base / "adapters/local/logo.png"
    if path.exists():
        return FileResponse(str(path), media_type="image/png")
    return Response(status_code=404, content="logo not found")


async def serve_theme_css(request: Request) -> Response:
    _base = Path(__file__).parent.parent.parent
    path = _base / "adapters/local/theme.css"
    if path.exists():
        return FileResponse(str(path), media_type="text/css")
    return Response(status_code=404, content="theme.css not found")


async def serve_theme_js(request: Request) -> Response:
    _base = Path(__file__).parent.parent.parent
    path = _base / "adapters/local/theme.js"
    if path.exists():
        return FileResponse(str(path), media_type="application/javascript")
    return Response(status_code=404, content="theme.js not found")


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------


async def sse_endpoint(request: Request) -> StreamingResponse:
    """Server-Sent Events push channel."""
    user_key = _extract_user_key(request)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    bridge = _QueueBridge(queue, loop)
    sse_manager.register(user_key, bridge)
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
            sse_manager.unregister(user_key, bridge)
            logger.info(f"SSE: client disconnected (user={user_key})")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Thin wrappers: delegate to shared handler classes
# ---------------------------------------------------------------------------


async def handle_health_api(request: Request) -> Response:
    return await health_handlers.handle_health_api(request)


async def handle_settings_get(request: Request) -> Response:
    return await settings_handlers.get_settings(request, DEFAULT_TENANT)


async def handle_settings_post(request: Request) -> Response:
    return await settings_handlers.post_settings(request, DEFAULT_TENANT)


async def handle_history(request: Request) -> Response:
    return await history_handlers.get_history(request, DEFAULT_TENANT, DEFAULT_CONVERSATION)


async def handle_integrations_list(request: Request) -> Response:
    return await integration_handlers.list_integrations(request, DEFAULT_TENANT)


async def handle_integration_get(request: Request) -> Response:
    return await integration_handlers.get_integration(request, DEFAULT_TENANT)


async def handle_integrations_post(request: Request) -> Response:
    return await integration_handlers.post_integration(request, DEFAULT_TENANT)


async def handle_integrations_test(request: Request) -> Response:
    return await integration_handlers.test_integration(request, DEFAULT_TENANT)


async def handle_chat(request: Request) -> Response:
    return await chat_handlers.handle_chat(request)


async def handle_clear(request: Request) -> Response:
    return await chat_handlers.handle_clear(request)


async def handle_teams_webhook(request: Request) -> Response:
    return await webhook_handlers.handle_teams_webhook(request)


async def handle_telegram_webhook(request: Request) -> Response:
    return await webhook_handlers.handle_telegram_webhook(request)


async def handle_skill_invoke(request: Request) -> Response:
    return await practice_handlers.invoke_skill(request, DEFAULT_TENANT)


async def handle_practices_list(request: Request) -> Response:
    return await practice_handlers.list_practices(request, DEFAULT_TENANT)


async def handle_practices_pages(request: Request) -> Response:
    return await practice_handlers.list_practice_pages(request, DEFAULT_TENANT)


async def handle_practices_upload(request: Request) -> Response:
    return await practice_handlers.upload_practice(request, DEFAULT_TENANT)


async def handle_callback(request: Request) -> Response:
    return await practice_handlers.handle_callback(request, DEFAULT_TENANT)


# ---------------------------------------------------------------------------
# Training & rules admin (thin wrappers)
# ---------------------------------------------------------------------------


async def handle_training_admin(request: Request) -> Response:
    """Handle /api/admin/training and /api/admin/training/{id} routes."""
    method = request.method
    path = str(request.url.path)
    parts = path.rstrip("/").split("/")
    example_id = parts[4] if len(parts) > 4 else ""

    try:
        if method == "GET" and not example_id:
            limit = int(request.query_params.get("limit", "50"))
            unannotated = request.query_params.get("unannotated", "false").lower() == "true"
            data, status = await training_handlers.list_training(
                DEFAULT_TENANT, limit=limit, unannotated=unannotated
            )
            return JSONResponse(data, status_code=status)

        elif method == "PATCH" and example_id:
            body = await request.json()
            data, status = await training_handlers.annotate_training(
                DEFAULT_TENANT, example_id, body
            )
            return JSONResponse(data, status_code=status)

        elif method == "DELETE" and example_id:
            data, status = await training_handlers.delete_training(DEFAULT_TENANT, example_id)
            return JSONResponse(data, status_code=status)

        return JSONResponse({"error": "Not found"}, status_code=404)
    except Exception as e:
        logger.exception("Training admin error")
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_rules_admin(request: Request) -> Response:
    """Handle /api/admin/rules/rebuild and /api/admin/rules/status."""
    method = request.method
    path = str(request.url.path)

    try:
        if method == "POST" and path.endswith("/rebuild"):
            _fire_and_forget(chat_handlers.rebuild_rules(DEFAULT_TENANT))
            data, status = await training_handlers.rebuild_rules(DEFAULT_TENANT)
            return JSONResponse(data, status_code=status)

        if method == "GET" and path.endswith("/status"):
            data, status = await training_handlers.rules_status(DEFAULT_TENANT)
            return JSONResponse(data, status_code=status)

        return JSONResponse({"error": "Not found"}, status_code=404)
    except Exception as e:
        logger.exception("Rules admin error")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Auth stubs (local dev -- always unauthenticated)
# ---------------------------------------------------------------------------


async def handle_auth_config(request: Request) -> Response:
    return JSONResponse(
        {
            "enabled": False,
            "client_id": "",
            "auth_domain": "",
            "user_pool_id": "",
        }
    )


async def handle_auth_me(request: Request) -> Response:
    tenant = await tenants.get_tenant(DEFAULT_TENANT)
    return JSONResponse(
        {
            "authenticated": True,
            "user_id": "local-admin",
            "tenant_id": DEFAULT_TENANT,
            "email": "admin@local.dev",
            "role": "admin",
            "tenant_status": tenant.status,
            "tenant_name": tenant.name,
        }
    )


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


async def handle_invitation_validate(request: Request) -> Response:
    """Public: validate an invite code, return tenant name + email."""
    try:
        code = request.query_params.get("code", "")
        if not code:
            return JSONResponse({"error": "Missing code parameter"}, status_code=400)
        invitation = await tenants.get_invitation(code)
        if not invitation or not invitation.is_valid():
            return JSONResponse({"error": "Invalid or expired invitation"}, status_code=404)
        try:
            tenant = await tenants.get_tenant(invitation.tenant_id)
            tenant_name = tenant.name
        except Exception:
            tenant_name = invitation.tenant_id
        return JSONResponse(
            {
                "valid": True,
                "tenant_name": tenant_name,
                "tenant_id": invitation.tenant_id,
                "email": invitation.email,
                "role": invitation.role,
            }
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_invitation_accept(request: Request) -> Response:
    """Accept an invitation -- link user to tenant."""
    try:
        body = await request.json()
        invite_code = body.get("invite_code", "")
        email = body.get("email", "")
        display_name = body.get("display_name", email.split("@")[0] if email else "")
        cognito_sub = body.get("cognito_sub", "")

        if not invite_code:
            return JSONResponse({"error": "invite_code is required"}, status_code=400)

        invitation = await tenants.get_invitation(invite_code)
        if not invitation or not invitation.is_valid():
            return JSONResponse({"error": "Invalid or expired invitation"}, status_code=404)

        if email and invitation.email.lower() != email.lower():
            return JSONResponse({"error": "Email does not match invitation"}, status_code=403)

        existing = await tenants.get_user_by_email(invitation.tenant_id, invitation.email)
        if existing:
            invitation.status = "accepted"
            invitation.accepted_at = datetime.now(timezone.utc).isoformat()
            await tenants.update_invitation(invitation)
            return JSONResponse(
                {
                    "accepted": True,
                    "tenant_id": invitation.tenant_id,
                    "already_member": True,
                }
            )

        from agent.models.tenant import TenantUser

        user_id = cognito_sub or f"user-{invitation.email.split('@')[0]}"
        user = TenantUser(
            user_id=user_id,
            tenant_id=invitation.tenant_id,
            email=invitation.email,
            display_name=display_name or invitation.email.split("@")[0],
            role=invitation.role,
            cognito_sub=cognito_sub,
        )
        await tenants.create_user(user)

        invitation.status = "accepted"
        invitation.accepted_at = datetime.now(timezone.utc).isoformat()
        await tenants.update_invitation(invitation)

        return JSONResponse(
            {
                "accepted": True,
                "tenant_id": invitation.tenant_id,
                "user_id": user_id,
                "role": invitation.role,
            }
        )
    except Exception as e:
        logger.exception("Invitation accept error")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Admin tenant management (local dev -- no auth required)
# ---------------------------------------------------------------------------


async def handle_create_tenant(request: Request) -> Response:
    """POST /api/admin/tenants -- create a tenant."""
    try:
        body = await request.json()
        tenant_id = body.get("tenant_id", "").strip()
        name = body.get("name", "").strip()
        if not tenant_id or not name:
            return JSONResponse({"error": "tenant_id and name are required"}, status_code=400)

        from agent.models.tenant import Tenant, TenantSettings, TenantUser

        now = datetime.now(timezone.utc).isoformat()
        status = body.get("status", "active")
        tenant = Tenant(
            tenant_id=tenant_id,
            name=name,
            status=status,
            created_at=now,
            settings=TenantSettings(enabled_skills=skills.list_skill_names()),
        )
        await tenants.create_tenant(tenant)
        logger.info(f"Created tenant: {tenant_id} ({name})")

        admin_data = body.get("admin_user")
        if admin_data:
            user = TenantUser(
                user_id=admin_data.get("cognito_sub", f"admin-{tenant_id}"),
                tenant_id=tenant_id,
                email=admin_data.get("email", "admin@local.dev"),
                display_name=admin_data.get("display_name", "Admin"),
                role="admin",
            )
            await tenants.create_user(user)

        return JSONResponse({"tenant_id": tenant_id, "created": True}, status_code=201)
    except Exception as e:
        logger.exception("Create tenant error")
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_admin_tenant_detail(request: Request) -> Response:
    """Catch-all for /api/admin/tenants/{rest:path} -- dispatch by method + sub-path."""
    rest = request.path_params["rest"]
    method = request.method
    path = f"/api/admin/tenants/{rest}"

    try:
        if method == "GET":
            if "/invitations" in path:
                return await _admin_list_invitations(path)
            elif "/users" in path:
                return await _admin_list_users(path)
            return Response(status_code=404)

        elif method == "POST":
            if "/invitations" in path:
                body = await request.json()
                return await _admin_create_invitation(request, path, body)
            return Response(status_code=404)

        elif method == "PUT":
            body = await request.json()
            return await _admin_update_tenant(path, body)

        elif method == "PATCH":
            if path.endswith("/activate"):
                return await _admin_activate_tenant(path)
            return Response(status_code=404)

        elif method == "DELETE":
            if "/invitations/" in path:
                return await _admin_revoke_invitation(path)
            return Response(status_code=404)

        return Response(status_code=405)

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def _admin_list_invitations(path: str) -> Response:
    parts = path.rstrip("/").split("/")
    tenant_id = parts[4]
    invitations = await tenants.list_invitations(tenant_id)
    return JSONResponse(
        {
            "invitations": [
                {
                    "invite_code": inv.invite_code,
                    "email": inv.email,
                    "role": inv.role,
                    "status": inv.status,
                    "created_at": inv.created_at,
                    "expires_at": inv.expires_at,
                }
                for inv in invitations
            ],
            "count": len(invitations),
        }
    )


async def _admin_list_users(path: str) -> Response:
    parts = path.rstrip("/").split("/")
    tenant_id = parts[4]
    users = await tenants.list_users(tenant_id)
    return JSONResponse(
        {
            "users": [
                {
                    "user_id": u.user_id,
                    "email": u.email,
                    "display_name": u.display_name,
                    "role": u.role,
                    "last_login": u.last_login,
                }
                for u in users
            ],
            "count": len(users),
        }
    )


async def _admin_create_invitation(request: Request, path: str, body: dict[str, Any]) -> Response:
    parts = path.rstrip("/").split("/")
    tenant_id = parts[4]
    email = body.get("email", "").strip().lower()
    role = body.get("role", "member")

    if not email:
        return JSONResponse({"error": "email is required"}, status_code=400)
    if role not in ("member", "admin"):
        return JSONResponse({"error": "role must be 'member' or 'admin'"}, status_code=400)

    try:
        await tenants.get_tenant(tenant_id)
    except Exception:
        return JSONResponse({"error": f"Tenant '{tenant_id}' not found"}, status_code=404)

    existing = await tenants.get_user_by_email(tenant_id, email)
    if existing:
        return JSONResponse({"error": f"{email} is already a member"}, status_code=409)

    now = datetime.now(timezone.utc).isoformat()
    invitation = Invitation(
        invite_code=Invitation.generate_code(),
        tenant_id=tenant_id,
        email=email,
        role=role,
        status="pending",
        invited_by="local-admin",
        created_at=now,
        expires_at=Invitation.default_expiry(),
    )
    await tenants.create_invitation(invitation)

    host = request.headers.get("host", "localhost:8080")
    scheme = "http" if "localhost" in host else "https"
    invite_url = f"{scheme}://{host}/login?invite={invitation.invite_code}"

    return JSONResponse(
        {
            "invite_code": invitation.invite_code,
            "invite_url": invite_url,
            "email": email,
            "role": role,
            "expires_at": invitation.expires_at,
        },
        status_code=201,
    )


async def _admin_update_tenant(path: str, body: dict[str, Any]) -> Response:
    tenant_id = path.split("/")[-1]
    tenant = await tenants.get_tenant(tenant_id)
    if "name" in body:
        tenant.name = body["name"]
    if "status" in body:
        tenant.status = body["status"]
    if "ai_model" in body:
        tenant.settings.ai_model = body["ai_model"]
    await tenants.update_tenant(tenant)
    return JSONResponse({"tenant_id": tenant_id, "updated": True})


async def _admin_activate_tenant(path: str) -> Response:
    parts = path.rstrip("/").split("/")
    tenant_id = parts[-2]
    tenant = await tenants.get_tenant(tenant_id)
    tenant.status = "active"
    await tenants.update_tenant(tenant)
    return JSONResponse({"tenant_id": tenant_id, "status": "active", "activated": True})


async def _admin_revoke_invitation(path: str) -> Response:
    parts = path.rstrip("/").split("/")
    invite_code = parts[-1]
    invitation = await tenants.get_invitation(invite_code)
    if not invitation:
        return JSONResponse({"error": "Invitation not found"}, status_code=404)
    invitation.status = "revoked"
    await tenants.update_invitation(invitation)
    return JSONResponse({"revoked": True, "invite_code": invite_code})


# ---------------------------------------------------------------------------
# Platform API (local dev -- no auth)
# ---------------------------------------------------------------------------


async def handle_platform_list_tenants(request: Request) -> Response:
    try:
        tenant_list = await tenants.list_tenants()
        result = []
        for t in tenant_list:
            try:
                users = await tenants.list_users(t.tenant_id)
                user_count = len(users)
            except Exception:
                user_count = 0
            result.append(
                {
                    "tenant_id": t.tenant_id,
                    "name": t.name,
                    "status": t.status,
                    "created_at": t.created_at,
                    "user_count": user_count,
                }
            )
        return JSONResponse({"tenants": result, "count": len(result)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_platform_create_tenant(request: Request) -> Response:
    try:
        import re

        body = await request.json()
        tenant_name = body.get("tenant_name", "").strip()
        admin_email = body.get("admin_email", "").strip().lower()
        admin_name = body.get("admin_name", "").strip()

        if not tenant_name or not admin_email or not admin_name:
            return JSONResponse(
                {"error": "tenant_name, admin_email, and admin_name are required"},
                status_code=400,
            )

        from agent.models.tenant import Tenant, TenantSettings

        slug = re.sub(r"[^a-z0-9-]+", "-", tenant_name.lower()).strip("-")
        if not slug or len(slug) < 2:
            return JSONResponse(
                {"error": "Tenant name cannot be slugified to a valid ID"}, status_code=400
            )

        candidate = slug
        suffix = 2
        while True:
            try:
                await tenants.get_tenant(candidate)
                candidate = f"{slug}-{suffix}"
                suffix += 1
            except Exception:
                break

        tenant_id = candidate
        now = datetime.now(timezone.utc).isoformat()
        tenant = Tenant(
            tenant_id=tenant_id,
            name=tenant_name,
            status="active",
            created_at=now,
            settings=TenantSettings(enabled_skills=skills.list_skill_names()),
        )
        await tenants.create_tenant(tenant)

        invitation = Invitation(
            invite_code=Invitation.generate_code(),
            tenant_id=tenant_id,
            email=admin_email,
            role="admin",
            status="pending",
            invited_by="platform-admin",
            created_at=now,
            expires_at=Invitation.default_expiry(),
        )
        await tenants.create_invitation(invitation)

        host = request.headers.get("host", "localhost:8080")
        scheme = "http" if "localhost" in host else "https"
        invite_url = f"{scheme}://{host}/login?invite={invitation.invite_code}"

        return JSONResponse(
            {
                "tenant_id": tenant_id,
                "tenant_name": tenant_name,
                "invite_code": invitation.invite_code,
                "invite_url": invite_url,
                "admin_name": admin_name,
                "admin_email": admin_email,
            },
            status_code=201,
        )
    except Exception as e:
        logger.exception("Platform create tenant error")
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_platform_tenant_detail(request: Request) -> Response:
    """Catch-all for /api/platform/tenants/{rest:path} -- dispatch by method + sub-path."""
    rest = request.path_params["rest"]
    method = request.method
    path = f"/api/platform/tenants/{rest}"

    try:
        if method == "DELETE":
            parts = path.rstrip("/").split("/")
            tenant_id = parts[-1]
            if tenant_id == "default":
                return JSONResponse({"error": "Cannot delete the default tenant"}, status_code=400)
            tenant = await tenants.get_tenant(tenant_id)
            tenant.status = "deleted"
            await tenants.update_tenant(tenant)
            return JSONResponse({"tenant_id": tenant_id, "status": "deleted"})

        elif method == "PATCH":
            if path.endswith("/suspend"):
                parts = path.rstrip("/").split("/")
                tenant_id = parts[-2]
                if tenant_id == "default":
                    return JSONResponse(
                        {"error": "Cannot suspend the default tenant"}, status_code=400
                    )
                tenant = await tenants.get_tenant(tenant_id)
                tenant.status = "suspended"
                await tenants.update_tenant(tenant)
                return JSONResponse({"tenant_id": tenant_id, "status": "suspended"})
            elif path.endswith("/activate"):
                parts = path.rstrip("/").split("/")
                tenant_id = parts[-2]
                tenant = await tenants.get_tenant(tenant_id)
                tenant.status = "active"
                await tenants.update_tenant(tenant)
                return JSONResponse({"tenant_id": tenant_id, "status": "active"})

        return Response(status_code=404)

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Blob upload/download (local FileStore)
# ---------------------------------------------------------------------------


async def handle_blob_upload(request: Request) -> Response:
    """POST /api/blobs/{key:path} -- upload a binary blob to BlobStore."""
    key = request.path_params["key"]
    try:
        body = await request.body()
        await blobs.put(DEFAULT_TENANT, key, body)
        return JSONResponse({"ok": True, "key": key, "size": len(body)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_blob_read(request: Request) -> Response:
    """GET /api/blobs/{key:path} -- read a binary blob from BlobStore."""
    key = request.path_params["key"]
    try:
        data = await blobs.get(DEFAULT_TENANT, key)
        # Guess content type from key extension
        ct = "application/octet-stream"
        if key.endswith(".json"):
            ct = "application/json"
        elif key.endswith(".wav"):
            ct = "audio/wav"
        elif key.endswith(".webm"):
            ct = "audio/webm"
        elif key.endswith(".html"):
            ct = "text/html"
        return Response(data, media_type=ct)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=404)


# ---------------------------------------------------------------------------
# Local adapter resolvers (Teams / Telegram)
# ---------------------------------------------------------------------------


async def _get_teams_adapter_local(recipient_id: str) -> TeamsAdapter | None:
    """Resolve a Teams adapter for local dev -- tries secrets then env vars."""
    try:
        creds = await secrets.get(DEFAULT_TENANT, "teams")
        app_id = creds.get("app_id", "")
        app_secret = creds.get("app_secret", "")
        if app_id and app_secret:
            return TeamsAdapter(app_id, app_secret)
    except Exception:
        pass
    app_id = os.environ.get("TEAMS_APP_ID", "")
    app_secret = os.environ.get("TEAMS_APP_SECRET", "")
    if app_id and app_secret:
        return TeamsAdapter(app_id, app_secret)
    # Fallback: mock adapter for Bot Framework Emulator
    return TeamsAdapter("local-test-app", "local-test-secret")


async def _get_telegram_adapter_local(token_hash: str) -> TelegramAdapter | None:
    """Resolve a Telegram adapter for local dev -- tries secrets then env vars."""
    try:
        creds = await secrets.get(DEFAULT_TENANT, "telegram")
        bot_token = creds.get("bot_token", "")
        if bot_token:
            # Verify the hash matches
            computed = hashlib.sha256(bot_token.encode()).hexdigest()[:16]
            if computed == token_hash or not token_hash:
                return TelegramAdapter(bot_token, creds.get("webhook_secret", ""))
    except Exception:
        pass
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if bot_token:
        return TelegramAdapter(bot_token, os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""))
    return None


async def _resolve_tenant_by_channel(channel: str, channel_key: str) -> Any:
    """Local dev: always return DEFAULT_TENANT since we only have one."""
    return await tenants.get_tenant(DEFAULT_TENANT)


# ---------------------------------------------------------------------------
# Sync skill invoker for DirectBus
# ---------------------------------------------------------------------------


async def _handle_sync_skill(
    tenant_id: str,
    skill_name: str,
    params: dict[str, Any],
    conversation_id: str,
    request_id: str,
    reply_channel: str,
    reply_target: str,
) -> dict[str, Any] | None:
    """Invoke a skill synchronously via DirectBus and return the result."""
    await bus.publish_skill_invocation(
        tenant_id,
        skill_name,
        params,
        conversation_id,
        request_id,
        reply_channel,
        reply_target,
    )
    return bus.get_result(request_id)


# ---------------------------------------------------------------------------
# Auth resolvers for ChatHandlers
# ---------------------------------------------------------------------------


async def _resolve_auth_single_tenant(request: Request) -> tuple[str, str]:
    """Single-tenant auth resolver: always DEFAULT_TENANT + extract email from JWT."""
    user_email = ""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            payload_b64 = auth_header[7:].split(".")[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            claims = json.loads(base64.urlsafe_b64decode(payload_b64))
            user_email = claims.get("email", "")
        except Exception:
            pass
    return (DEFAULT_TENANT, user_email)


# ---------------------------------------------------------------------------
# Callback delivery via SSE
# ---------------------------------------------------------------------------


async def _deliver_callback_via_sse(event_data: dict[str, Any], pending: Any) -> None:
    """Deliver a callback result via SSE to the connected user."""
    user_key = pending.get("user_key", "") if isinstance(pending, dict) else ""
    if user_key:
        sse_manager.send_event(user_key, "message", event_data)


# ---------------------------------------------------------------------------
# Starlette app
# ---------------------------------------------------------------------------

routes = [
    # Static pages
    Route("/", homepage),
    Route("/chat", homepage),
    Route("/logo.png", serve_logo),
    Route("/theme.css", serve_theme_css),
    Route("/theme.js", serve_theme_js),
    Route("/health", health_page),
    Route("/settings", settings_page),
    Route("/onboard", onboard_page),
    Route("/platform", platform_page),
    Route("/training", training_page),
    Route("/p/{practice}/{page}", practice_page),
    # API
    Route("/api/events", sse_endpoint),
    Route("/api/health", handle_health_api),
    Route("/api/settings", handle_settings_get, methods=["GET"]),
    Route("/api/settings", handle_settings_post, methods=["POST"]),
    Route("/api/history", handle_history),
    Route("/api/auth/config", handle_auth_config),
    Route("/api/auth/me", handle_auth_me),
    Route("/api/chat", handle_chat, methods=["POST"]),
    Route("/api/clear", handle_clear, methods=["POST"]),
    Route("/api/integrations", handle_integrations_list),
    Route("/api/integrations/{name}/test", handle_integrations_test, methods=["POST"]),
    Route("/api/integrations/{name}", handle_integration_get, methods=["GET"]),
    Route("/api/integrations/{name}", handle_integrations_post, methods=["POST"]),
    Route("/api/invitations/validate", handle_invitation_validate),
    Route("/api/invitations/accept", handle_invitation_accept, methods=["POST"]),
    Route("/api/channels/teams/webhook", handle_teams_webhook, methods=["POST"]),
    Route(
        "/api/channels/telegram/webhook/{token_hash}",
        handle_telegram_webhook,
        methods=["POST"],
    ),
    # Platform routes
    Route("/api/platform/tenants", handle_platform_list_tenants, methods=["GET"]),
    Route("/api/platform/tenants", handle_platform_create_tenant, methods=["POST"]),
    Route("/api/platform/tenants/{rest:path}", handle_platform_tenant_detail),
    # Callback endpoint for async external services
    Route("/api/callback/{request_id}", handle_callback, methods=["POST"]),
    # Practice routes
    Route("/api/skill/{name}", handle_skill_invoke, methods=["POST"]),
    Route("/api/practices", handle_practices_list, methods=["GET"]),
    Route("/api/practices/pages", handle_practices_pages, methods=["GET"]),
    Route("/api/practices/upload", handle_practices_upload, methods=["POST"]),
    Route("/api/blobs/{key:path}", handle_blob_upload, methods=["POST"]),
    Route("/api/blobs/{key:path}", handle_blob_read, methods=["GET"]),
    # Admin routes
    Route("/api/admin/rules/{rest:path}", handle_rules_admin, methods=["GET", "POST"]),
    Route(
        "/api/admin/training/{example_id}",
        handle_training_admin,
        methods=["GET", "PATCH", "DELETE"],
    ),
    Route("/api/admin/training", handle_training_admin, methods=["GET"]),
    Route("/api/admin/tenants", handle_create_tenant, methods=["POST"]),
    Route("/api/admin/tenants/{rest:path}", handle_admin_tenant_detail),
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
# Initialisation & entry point
# ---------------------------------------------------------------------------


async def init() -> None:
    """Initialize all components."""
    global \
        ai, \
        memory, \
        tenants, \
        secrets, \
        skills, \
        bus, \
        rule_store, \
        training_store, \
        error_handler, \
        practices, \
        blobs, \
        pending_store, \
        started_at, \
        settings_handlers, \
        integration_handlers, \
        chat_handlers, \
        history_handlers, \
        training_handlers, \
        health_handlers, \
        practice_handlers, \
        webhook_handlers

    started_at = time.time()

    # Load .env
    secrets = EnvSecretsProvider(".env")

    # Initialize AI providers -- both can run simultaneously
    _providers: dict[str, AnthropicProvider | OllamaProvider] = {}
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if api_key:
        logger.info("Using Anthropic provider (direct API)")
        _providers["anthropic"] = AnthropicProvider(api_key)
    if OLLAMA_API_URL:
        logger.info(f"Using Ollama provider at {OLLAMA_API_URL}")
        _providers["ollama"] = OllamaProvider(base_url=OLLAMA_API_URL)
    if not _providers:
        logger.error(
            "No AI provider configured. Set ANTHROPIC_API_KEY or OLLAMA_API_URL "
            "in .env. For free local AI: ollama serve & set OLLAMA_API_URL=http://localhost:11434"
        )
        sys.exit(1)
    ai = MultiAIProvider(_providers)
    memory = SQLiteConversationStore("data/t3nets.db")
    tenants = SQLiteTenantStore("data/t3nets.db")
    blobs = FileStore("data/blobs")
    pending_store = LocalPendingStore()

    # Load skills -- base skills (ping) from agent/skills/
    skills = SkillRegistry()
    skills_dir = Path(__file__).parent.parent.parent / "agent" / "skills"
    skills.load_from_directory(skills_dir)

    # Load practices and register their skills
    practices = PracticeRegistry()
    practices_dir = Path(__file__).parent.parent.parent / "agent" / "practices"
    practices.load_builtin(practices_dir)
    practices.load_uploaded(Path("data"))
    practices.register_skills(skills)
    logger.info(f"Loaded skills: {skills.list_skill_names()}")
    logger.info(f"Loaded practices: {[p.name for p in practices.list_all()]}")

    # Rule and training stores
    rule_store = SQLiteRuleStore("data/t3nets.db")
    training_store = SQLiteTrainingStore("data/t3nets.db")
    error_handler = ErrorHandler()

    # Direct bus -- with context for practice skills (BlobStore access)
    bus = DirectBus(skills, secrets, context={"blob_store": blobs})

    # Register channels
    channels = ChannelRegistry()
    channels.register(DashboardAdapter())

    # --- Instantiate shared handler classes ---

    settings_handlers = SettingsHandlers(
        tenant_store=tenants,
        secrets_provider=secrets,
        skill_registry=skills,
        practice_registry=practices,
        active_providers=lambda: ai.active_providers,
        platform=os.getenv("T3NETS_PLATFORM", "local"),
        stage=os.getenv("T3NETS_STAGE", "dev"),
        build_number=BUILD_NUMBER,
        rebuild_callback=lambda tid: _fire_and_forget(chat_handlers.rebuild_rules(tid)),
    )

    integration_handlers = IntegrationHandlers(secrets=secrets)

    history_handlers = HistoryHandlers(conversation_store=memory)

    training_handlers = TrainingHandlers(
        training_store=training_store,
        rule_store=rule_store,
        compiled_engines=_compiled_engines,
        rebuild_rules_fn=None,  # rebuild is triggered via handle_rules_admin
    )

    health_handlers = HealthHandlers(
        tenants=tenants,
        secrets=secrets,
        skill_registry=skills,
        started_at=started_at,
        connection_count=lambda: sse_manager.connection_count,
        get_stats=lambda: {
            "rule_routed": stats["rule_routed"],
            "ai_routed": stats["ai_routed"],
            "conversational": stats["conversational"],
            "raw": stats["raw"],
            "errors": stats["errors"],
        },
        get_ai_info=lambda: {
            "providers": ai.active_providers,
            "model": _resolve_model(
                # lazy tenant lookup -- health handler calls this rarely
                type("T", (), {"settings": type("S", (), {"ai_model": DEFAULT_MODEL_ID})()})()
            )[1],
            "api_key_preview": _api_key_preview(),
            "total_tokens": stats["total_tokens"],
        },
        platform=os.getenv("T3NETS_PLATFORM", "local"),
        stage=os.getenv("T3NETS_STAGE", "dev"),
        default_tenant=DEFAULT_TENANT,
        connection_label="sse_connections",
    )

    practice_handlers = PracticeHandlers(
        practices=practices,
        skills=skills,
        blobs=blobs,
        tenants=tenants,
        secrets=secrets,
        pending_store=pending_store,
        callback_delivery=_deliver_callback_via_sse,
    )

    chat_handlers = ChatHandlers(
        memory=memory,
        tenants=tenants,
        ai=ai,
        skills=skills,
        compiled_engines=_compiled_engines,
        rule_store=rule_store,
        training_store=training_store,
        stats=stats,
        error_handler=error_handler,
        resolve_auth=_resolve_auth_single_tenant,
        resolve_model=_resolve_model,
        fire_and_forget=_fire_and_forget,
        skill_invoker=_handle_sync_skill,
    )

    webhook_handlers = WebhookHandlers(
        ai=ai,
        memory=memory,
        bus=bus,
        skills=skills,
        stats=stats,
        compiled_engines=_compiled_engines,
        fallback_router=None,
        resolve_model=_resolve_model,
        resolve_teams_adapter=_get_teams_adapter_local,
        resolve_telegram_adapter=_get_telegram_adapter_local,
        resolve_tenant_by_channel=_resolve_tenant_by_channel,
        log_training=chat_handlers.log_training,
        enrich_match_params=_enrich_match_params,
    )

    # Seed default tenant
    tenant = tenants.seed_default_tenant(
        tenant_id="local",
        name="Dev",
        enabled_skills=skills.list_skill_names(),
    )
    active = ai.active_providers
    default = "llama-3.2-3b" if active == ["ollama"] else DEFAULT_MODEL_ID
    current_model = get_model(tenant.settings.ai_model or "")
    if (
        not tenant.settings.ai_model
        or not current_model
        or not any(p in current_model.providers for p in active)
    ):
        tenant.settings.ai_model = default
        await tenants.update_tenant(tenant)
    logger.info(f"Tenant: {tenant.name} (skills: {tenant.settings.enabled_skills})")

    # Seed second tenant for multi-tenancy testing
    acme = tenants.seed_default_tenant(
        tenant_id="acme",
        name="Acme Corp",
        admin_email="admin@acme.dev",
        admin_name="Acme Admin",
        enabled_skills=["sprint_status", "ping"],
    )
    logger.info(f"Tenant: {acme.name} (skills: {acme.settings.enabled_skills})")

    connected = await secrets.list_integrations("local")
    logger.info(f"Connected integrations: {connected}")

    # Build compiled rule engines for all tenants (load from DB or generate via AI)
    for t in [tenant, acme]:
        cached = await rule_store.load_rule_set(t.tenant_id)
        if cached:
            _compiled_engines[t.tenant_id] = CompiledRuleEngine(cached, skills)
            logger.info(
                f"Loaded rule engine for '{t.tenant_id}' "
                f"(v{cached.version}, generated {cached.generated_at[:10]})"
            )
        else:
            logger.info(f"No rules cached for '{t.tenant_id}' -- generating via AI...")
            await chat_handlers.rebuild_rules(t.tenant_id)


def _api_key_preview() -> str:
    """Return a safe preview of the Anthropic API key for health endpoint."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if len(api_key) > 12:
        return api_key[:8] + "..." + api_key[-4:]
    return "not set" if not api_key else "***"


def main() -> None:
    asyncio.run(init())

    port = int(os.getenv("PORT", "8080"))
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
