"""
T3nets Local Development Server

A simple HTTP server that wires all local adapters together.
Uses HYBRID ROUTING:
  1. Conversational → Claude direct (no tools, fewer tokens)
  2. Rule-matched → Skill direct, then Claude formats (1 API call instead of 2)
  3. Ambiguous → Full Claude with tools (2 API calls)

Debug mode:
  Append --raw to any message to skip Claude formatting and see raw skill output.
  Only works for skills that support it (e.g. sprint_status).

Usage:
    python -m adapters.local.dev_server
"""

import asyncio
import base64
import hashlib
import inspect
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
from adapters.local.file_store import FileStore
from adapters.local.sqlite_rule_store import SQLiteRuleStore
from adapters.local.sqlite_store import SQLiteConversationStore
from adapters.local.sqlite_tenant_store import SQLiteTenantStore
from adapters.local.sqlite_training_store import SQLiteTrainingStore
from adapters.ollama.provider import OllamaProvider
from adapters.shared.multi_provider import MultiAIProvider
from adapters.shared.server_utils import (
    INTEGRATION_SCHEMAS,
    _format_raw_json,
    _strip_metadata,
    _uptime_human,
)
from agent.channels.base import ChannelRegistry
from agent.channels.dashboard import DashboardAdapter
from agent.channels.teams import TeamsAdapter
from agent.channels.telegram import TelegramAdapter
from agent.errors.handler import ErrorHandler
from agent.models.ai_models import (
    DEFAULT_MODEL_ID,
    get_model,
    get_model_for_provider,
    get_models_for_providers,
)
from agent.models.message import ChannelType
from agent.models.tenant import Invitation
from agent.practices.registry import PracticeRegistry
from agent.router.compiled_engine import CompiledRuleEngine, is_conversational, strip_raw_flag
from agent.router.models import TrainingExample
from agent.router.rule_engine_builder import RuleEngineBuilder
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
started_at: float = 0.0

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

# Build number — read from version.txt at startup
_version_path = Path(__file__).resolve().parent.parent.parent / "version.txt"
BUILD_NUMBER = _version_path.read_text().strip() if _version_path.exists() else "0"

# Stats for the session
stats = {
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
        # Model not supported by any active provider — use a safe fallback
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
# Health / status
# ---------------------------------------------------------------------------


async def handle_health_api(request: Request) -> Response:
    """Rich health/status JSON endpoint."""
    try:
        uptime_secs = time.time() - started_at
        tenant = await tenants.get_tenant(DEFAULT_TENANT)
        connected_integrations = await secrets.list_integrations(DEFAULT_TENANT)

        all_integrations = {
            "jira": {"connected": "jira" in connected_integrations},
            "github": {"connected": "github" in connected_integrations},
            "teams": {"connected": "teams" in connected_integrations},
            "telegram": {"connected": "telegram" in connected_integrations},
            "twilio": {"connected": "twilio" in connected_integrations},
        }

        skills_info = []
        for skill in skills.list_skills():
            skills_info.append(
                {
                    "name": skill.name,
                    "description": skill.description.strip()[:120],
                    "requires_integration": skill.requires_integration,
                    "supports_raw": skill.supports_raw,
                    "triggers": skill.triggers[:8],
                }
            )

        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if len(api_key) > 12:
            key_preview = api_key[:8] + "..." + api_key[-4:]
        else:
            key_preview = "not set" if not api_key else "***"

        health = {
            "status": "ok",
            "platform": os.getenv("T3NETS_PLATFORM", "local"),
            "stage": os.getenv("T3NETS_STAGE", "dev"),
            "started_at": datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat(),
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
            "ai": {
                "providers": ai.active_providers,
                "model": _resolve_model(tenant)[1],
                "api_key_preview": key_preview,
                "total_tokens": stats["total_tokens"],
            },
            "routing": {
                "rule_routed": stats["rule_routed"],
                "ai_routed": stats["ai_routed"],
                "conversational": stats["conversational"],
                "raw": stats["raw"],
                "errors": stats["errors"],
            },
            "integrations": all_integrations,
            "skills": skills_info,
            "sse_connections": sse_manager.connection_count,
        }
        return JSONResponse(health)

    except Exception as e:
        logger.exception("Health check error")
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Auth stubs (local dev — always unauthenticated)
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
# Settings
# ---------------------------------------------------------------------------


async def handle_settings_get(request: Request) -> Response:
    """Return current settings and available models."""
    try:
        tenant = await tenants.get_tenant(DEFAULT_TENANT)
        s = tenant.settings
        available_skills = [
            {
                "name": sk.name,
                "description": sk.description.strip(),
                "requires_integration": sk.requires_integration,
            }
            for sk in skills.list_skills()
        ]
        connected_integrations = await secrets.list_integrations(DEFAULT_TENANT)
        return JSONResponse(
            {
                "ai_model": s.ai_model or DEFAULT_MODEL_ID,
                "providers": ai.active_providers,
                "models": get_models_for_providers(ai.active_providers),
                "platform": os.getenv("T3NETS_PLATFORM", "local"),
                "stage": os.getenv("T3NETS_STAGE", "dev"),
                "build": BUILD_NUMBER,
                "enabled_skills": s.enabled_skills,
                "available_skills": available_skills,
                "connected_integrations": connected_integrations,
                "enabled_channels": s.enabled_channels,
                "system_prompt_override": s.system_prompt_override,
                "max_tokens_per_message": s.max_tokens_per_message,
                "messages_per_day": s.messages_per_day,
                "max_conversation_history": s.max_conversation_history,
            }
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_settings_post(request: Request) -> Response:
    """Update tenant settings."""
    try:
        body = await request.json()
        tenant = await tenants.get_tenant(DEFAULT_TENANT)
        changed = False
        rebuild_skills = False

        if "ai_model" in body:
            model_id = body["ai_model"]
            model = get_model(model_id)
            if not model:
                return JSONResponse({"error": f"Unknown model: {model_id}"}, status_code=400)
            active = ai.active_providers
            if not any(p in model.providers for p in active):
                return JSONResponse(
                    {"error": f"Model '{model_id}' not available for {active}"}, status_code=400
                )
            tenant.settings.ai_model = model_id
            changed = True
            logger.info(f"Model changed to: {model.display_name} ({model_id})")

        if "enabled_skills" in body:
            skill_list = body["enabled_skills"]
            if not isinstance(skill_list, list):
                return JSONResponse({"error": "enabled_skills must be a list"}, status_code=400)
            known = set(skills.list_skill_names())
            unknown = [s for s in skill_list if s not in known]
            if unknown:
                return JSONResponse(
                    {"error": f"Unknown skills: {', '.join(unknown)}"}, status_code=400
                )
            tenant.settings.enabled_skills = skill_list
            changed = True
            logger.info(f"Enabled skills updated: {skill_list}")
            rebuild_skills = True

        if "system_prompt_override" in body:
            tenant.settings.system_prompt_override = body["system_prompt_override"]
            changed = True

        if "max_tokens_per_message" in body:
            val = body["max_tokens_per_message"]
            if not isinstance(val, int) or val < 256 or val > 16384:
                return JSONResponse(
                    {"error": "max_tokens_per_message must be 256-16384"}, status_code=400
                )
            tenant.settings.max_tokens_per_message = val
            changed = True

        if "messages_per_day" in body:
            val = body["messages_per_day"]
            if not isinstance(val, int) or val < 1:
                return JSONResponse(
                    {"error": "messages_per_day must be a positive integer"}, status_code=400
                )
            tenant.settings.messages_per_day = val
            changed = True

        if "max_conversation_history" in body:
            val = body["max_conversation_history"]
            if not isinstance(val, int) or val < 1 or val > 100:
                return JSONResponse(
                    {"error": "max_conversation_history must be 1-100"}, status_code=400
                )
            tenant.settings.max_conversation_history = val
            changed = True

        if changed:
            await tenants.update_tenant(tenant)
        if rebuild_skills:
            _fire_and_forget(_rebuild_rules(DEFAULT_TENANT))
        return JSONResponse({"ok": True})

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_history(request: Request) -> Response:
    """Return conversation history for the default conversation."""
    try:
        history = await memory.get_conversation(DEFAULT_TENANT, DEFAULT_CONVERSATION)
        return JSONResponse(
            {
                "messages": history,
                "platform": os.getenv("T3NETS_PLATFORM", "local"),
                "stage": os.getenv("T3NETS_STAGE", "dev"),
            }
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------


async def handle_integrations_list(request: Request) -> Response:
    """GET /api/integrations — list all integrations with status and field schemas."""
    try:
        connected = await secrets.list_integrations(DEFAULT_TENANT)
        result = []
        for name, schema in INTEGRATION_SCHEMAS.items():
            result.append(
                {
                    "name": name,
                    "label": schema["label"],
                    "connected": name in connected,
                    "fields": schema["fields"],
                }
            )
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_integration_get(request: Request) -> Response:
    """GET /api/integrations/{name} — return current config with sensitive fields masked."""
    try:
        integration_name = request.path_params["name"]
        if integration_name not in INTEGRATION_SCHEMAS:
            return JSONResponse(
                {"error": f"Unknown integration: {integration_name}"}, status_code=404
            )
        schema = INTEGRATION_SCHEMAS[integration_name]
        connected = False
        config = {}
        try:
            stored = await secrets.get(DEFAULT_TENANT, integration_name)
            connected = True
            password_keys = {f["key"] for f in schema["fields"] if f["type"] == "password"}
            for key, value in stored.items():
                if key in password_keys and value:
                    config[key] = "\u2022" * 8
                else:
                    config[key] = value
        except Exception:
            pass
        return JSONResponse(
            {
                "name": integration_name,
                "label": schema["label"],
                "connected": connected,
                "config": config,
                "fields": schema["fields"],
            }
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_integrations_post(request: Request) -> Response:
    """POST /api/integrations/{name} — save integration credentials."""
    try:
        integration_name = request.path_params["name"]
        body = await request.json()
        tenant_id = body.get("tenant_id") or DEFAULT_TENANT
        await secrets.put(tenant_id, integration_name, body)
        logger.info(f"Stored {integration_name} credentials for tenant {tenant_id}")
        if integration_name == "telegram":
            _register_telegram_webhook(request, body)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("Integration endpoint error")
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_integrations_test(request: Request) -> Response:
    """POST /api/integrations/{name}/test — test integration credentials."""
    try:
        integration_name = request.path_params["name"]
        body = await request.json()
        result = _test_integration(integration_name, body)
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)
    except Exception as e:
        logger.exception("Integration test error")
        return JSONResponse({"error": str(e)}, status_code=500)


def _test_integration(name: str, creds: dict[str, Any]) -> dict[str, Any]:
    if name == "jira":
        return _test_jira(creds)
    elif name == "telegram":
        return _test_telegram(creds)
    return {"ok": False, "error": f"Testing not supported for '{name}'"}


def _register_telegram_webhook(request: Request, creds: dict[str, Any]) -> None:
    """Register the Telegram webhook URL after saving credentials."""
    bot_token = creds.get("bot_token", "")
    if not bot_token:
        return
    try:
        token_hash = hashlib.sha256(bot_token.encode()).hexdigest()[:16]
        host = request.headers.get("host", "localhost:8080")
        scheme = "https" if "443" in host else "http"
        base_url = f"{scheme}://{host}"
        webhook_url = f"{base_url}/api/channels/telegram/webhook/{token_hash}"
        webhook_secret = creds.get("webhook_secret", "")
        adapter = TelegramAdapter(bot_token, webhook_secret)
        result = adapter.register_webhook(webhook_url)
        logger.info(f"Telegram webhook registration: {result}")
    except Exception as e:
        logger.error(f"Failed to register Telegram webhook: {e}")


def _test_telegram(creds: dict[str, Any]) -> dict[str, Any]:
    bot_token = creds.get("bot_token", "")
    if not bot_token:
        return {"ok": False, "error": "Bot token is required"}
    adapter = TelegramAdapter(bot_token)
    info = adapter.get_bot_info()
    if "error" in info:
        return {"ok": False, "error": info["error"]}
    return {
        "ok": True,
        "bot_name": f"@{info.get('username', '')}",
        "display_name": info.get("first_name", ""),
    }


def _test_jira(creds: dict[str, Any]) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    url = creds.get("url", "").rstrip("/")
    email = creds.get("email", "")
    api_token = creds.get("api_token", "")
    if not all([url, email, api_token]):
        return {"ok": False, "error": "url, email, and api_token are required"}
    try:
        auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        req = urllib.request.Request(
            f"{url}/rest/api/3/myself",
            headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return {
                "ok": True,
                "user": data.get("emailAddress", email),
                "display_name": data.get("displayName", ""),
            }
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"Jira returned {e.code}: {e.reason}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Rule engine helpers
# ---------------------------------------------------------------------------


def _get_engine(tenant_id: str) -> CompiledRuleEngine | None:
    """Return the compiled rule engine for a tenant, or None if not yet built."""
    return _compiled_engines.get(tenant_id)


async def _rebuild_rules(tenant_id: str) -> None:
    """(Re)build AI-generated routing rules for a tenant and cache the engine."""
    try:
        tenant = await tenants.get_tenant(tenant_id)
        all_skills = skills.list_skills()
        enabled = [s for s in all_skills if s.name in tenant.settings.enabled_skills]
        disabled = [s for s in all_skills if s.name not in tenant.settings.enabled_skills]

        # Increment version from the last saved rule set
        existing = await rule_store.load_rule_set(tenant_id)
        old_version = existing.version if existing else 0

        # Include recent training data to improve patterns
        training_data = await training_store.list_examples(tenant_id, limit=50)

        active_provider, api_model, _ = _resolve_model(tenant)
        builder = RuleEngineBuilder()
        rule_set = await builder.build_rules(
            tenant_id=tenant_id,
            enabled_skills=enabled,
            disabled_skills=disabled,
            ai=ai.for_provider(active_provider),
            model=api_model,
            training_data=training_data or None,
        )
        rule_set.version = old_version + 1

        await rule_store.save_rule_set(rule_set)
        _compiled_engines[tenant_id] = CompiledRuleEngine(rule_set, skills)
        logger.info(f"Rules rebuilt for tenant '{tenant_id}' (v{rule_set.version})")
    except Exception:
        logger.exception(f"Failed to rebuild rules for tenant '{tenant_id}'")


async def _log_training(
    tenant_id: str,
    message_text: str,
    matched_skill: str | None,
    matched_action: str | None,
    was_disabled_skill: bool = False,
) -> None:
    """Fire-and-forget: log a Tier 2 routing decision as training data."""
    import uuid as _uuid
    from datetime import datetime as _dt
    from datetime import timezone as _tz

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
        await training_store.log_example(example)
    except Exception:
        logger.exception("Failed to log training example")


# ---------------------------------------------------------------------------
# Chat & clear
# ---------------------------------------------------------------------------


async def handle_chat(request: Request) -> Response:
    """Handle a chat message with hybrid routing."""
    try:
        body = await request.json()
        text = body.get("text", "").strip()
        if not text:
            return JSONResponse({"error": "Empty message"}, status_code=400)

        conversation_id = body.get("conversation_id", DEFAULT_CONVERSATION)

        # Extract user email from JWT (if present)
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

        clean_text, is_raw = strip_raw_flag(text)
        logger.info(f"Chat: {text[:100]}" + (" [RAW]" if is_raw else ""))
        is_raw_response = False

        history = _strip_metadata(await memory.get_conversation(DEFAULT_TENANT, conversation_id))
        tenant = await tenants.get_tenant(DEFAULT_TENANT)
        active_provider, active_model, model_short_name = _resolve_model(tenant)
        provider_ai = ai.for_provider(active_provider)

        system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
When you have data to present, format it clearly with structure."""

        # === TIER 0: Conversational ===
        if not is_raw and is_conversational(clean_text):
            logger.info("Route: CONVERSATIONAL (no tools)")
            stats["conversational"] += 1
            messages = history + [{"role": "user", "content": clean_text}]
            response = await provider_ai.chat(
                model=active_model, system=system, messages=messages, tools=[]
            )
            assistant_text = response.text or "Hey! How can I help?"
            total_tokens = response.input_tokens + response.output_tokens
            route_type = "conversational"

        else:
            # === TIER 1: Compiled rule engine ===
            engine = _get_engine(DEFAULT_TENANT)
            match = engine.match(clean_text, tenant.settings.enabled_skills) if engine else None

            if match:
                logger.info(
                    f"Route: RULE-BASED → {match.skill_name}.{match.action}"
                    + (" [RAW]" if is_raw else "")
                )
                request_id = f"rule-{conversation_id}"
                await bus.publish_skill_invocation(
                    tenant_id=DEFAULT_TENANT,
                    skill_name=match.skill_name,
                    params=match.params,
                    session_id=conversation_id,
                    request_id=request_id,
                    reply_channel="dashboard",
                    reply_target="dashboard-user",
                )
                skill_result = bus.get_result(request_id)
                if not skill_result:
                    skill_result = {"error": "Skill returned no result"}

                if is_raw and engine and engine.supports_raw(match.skill_name):
                    logger.info(f"Returning raw output for {match.skill_name}")
                    stats["raw"] += 1
                    stats["rule_routed"] += 1
                    assistant_text = _format_raw_json(skill_result)
                    total_tokens = 0
                    route_type = "rule"
                    is_raw_response = True
                else:
                    if is_raw:
                        logger.info(
                            f"Skill '{match.skill_name}' does not support --raw, "
                            f"falling back to Claude formatting"
                        )
                    stats["rule_routed"] += 1
                    logger.info(f"Skill result: {json.dumps(skill_result)[:300]}")
                    format_prompt = f"""{system}

The user asked: "{clean_text}"

You called the {match.skill_name} tool and got this data:
{json.dumps(skill_result, indent=2)}

Format this data into a clear, helpful response for the user.
Include risk assessment and actionable suggestions where relevant."""
                    messages = history + [{"role": "user", "content": format_prompt}]
                    response = await provider_ai.chat(
                        model=active_model, system=system, messages=messages, tools=[]
                    )
                    assistant_text = response.text or "Got the data but couldn't format it."
                    total_tokens = response.input_tokens + response.output_tokens
                    route_type = "rule"

            # Check for disabled skill before falling through to Claude
            elif engine and (disabled_skill := engine.check_disabled_skill(clean_text)):
                skill_display = disabled_skill.replace("_", " ")
                logger.info(f"Route: DISABLED SKILL → {disabled_skill}")
                assistant_text = (
                    f"The {skill_display} feature isn't enabled for your workspace. "
                    f"Contact your admin to enable it."
                )
                total_tokens = 0
                route_type = "disabled_skill"
                _fire_and_forget(
                    _log_training(DEFAULT_TENANT, clean_text, None, None, was_disabled_skill=True)
                )

            # === TIER 2: Full Claude routing (freeform chat + skill selection) ===
            else:
                if is_raw:
                    logger.info("--raw flag ignored: no rule match, using AI routing")
                logger.info("Route: AI (full Claude with tools)")
                stats["ai_routed"] += 1

                tools = skills.get_tools_for_tenant(type("Ctx", (), {"tenant": tenant})())
                messages = history + [{"role": "user", "content": clean_text}]
                response = await provider_ai.chat(
                    model=active_model, system=system, messages=messages, tools=tools
                )

                if response.has_tool_use:
                    tool_call = response.tool_calls[0]
                    logger.info(f"AI chose skill: {tool_call.tool_name}")
                    request_id = f"ai-{conversation_id}"
                    await bus.publish_skill_invocation(
                        tenant_id=DEFAULT_TENANT,
                        skill_name=tool_call.tool_name,
                        params=tool_call.tool_params,
                        session_id=conversation_id,
                        request_id=request_id,
                        reply_channel="dashboard",
                        reply_target="dashboard-user",
                    )
                    skill_result = bus.get_result(request_id)
                    if not skill_result:
                        skill_result = {"error": "Skill returned no result"}

                    _fire_and_forget(
                        _log_training(
                            DEFAULT_TENANT,
                            clean_text,
                            tool_call.tool_name,
                            tool_call.tool_params.get("action"),
                        )
                    )

                    if is_raw and engine and engine.supports_raw(tool_call.tool_name):
                        logger.info(f"Returning raw output for {tool_call.tool_name} (AI-routed)")
                        stats["raw"] += 1
                        assistant_text = _format_raw_json(skill_result)
                        total_tokens = response.input_tokens + response.output_tokens
                        route_type = "ai"
                        is_raw_response = True
                    else:
                        messages_with_tool = messages + [
                            {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": tool_call.tool_use_id,
                                        "name": tool_call.tool_name,
                                        "input": tool_call.tool_params,
                                    }
                                ],
                            }
                        ]
                        final_response = await provider_ai.chat_with_tool_result(
                            model=active_model,
                            system=system,
                            messages=messages_with_tool,
                            tools=tools,
                            tool_use_id=tool_call.tool_use_id,
                            tool_result=skill_result,
                        )
                        assistant_text = (
                            final_response.text or "Got the data but couldn't format it."
                        )
                        total_tokens = (
                            response.input_tokens
                            + response.output_tokens
                            + final_response.input_tokens
                            + final_response.output_tokens
                        )
                        route_type = "ai"
                else:
                    # Freeform chat response — no skill matched
                    _fire_and_forget(_log_training(DEFAULT_TENANT, clean_text, None, None))
                    assistant_text = response.text or "I'm not sure how to help with that."
                    total_tokens = response.input_tokens + response.output_tokens
                    route_type = "ai"

        stats["total_tokens"] += total_tokens

        chat_metadata: dict[str, Any] = {
            "route": route_type,
            "model": model_short_name,
            "tokens": total_tokens,
        }
        if user_email:
            chat_metadata["user_email"] = user_email
        if not is_raw_response:
            await memory.save_turn(
                DEFAULT_TENANT,
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
        stats["errors"] += 1
        friendly = error_handler.handle(e, context="chat")
        return JSONResponse({"error": friendly.message, **friendly.to_dict()}, status_code=500)


async def handle_clear(request: Request) -> Response:
    """Clear conversation history."""
    try:
        body = await request.json()
        conversation_id = body.get("conversation_id", DEFAULT_CONVERSATION)
        await memory.clear_conversation(DEFAULT_TENANT, conversation_id)
        return JSONResponse({"cleared": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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
    """Accept an invitation — link user to tenant."""
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
# Admin tenant management (local dev — no auth required)
# ---------------------------------------------------------------------------


async def handle_create_tenant(request: Request) -> Response:
    """POST /api/admin/tenants — create a tenant."""
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
    """Catch-all for /api/admin/tenants/{rest:path} — dispatch by method + sub-path."""
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
# Platform API (local dev — no auth)
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
    """Catch-all for /api/platform/tenants/{rest:path} — dispatch by method + sub-path."""
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
# Training data admin handlers (local dev — no strict auth)
# ---------------------------------------------------------------------------


async def handle_training_admin(request: Request) -> Response:
    """Handle /api/admin/training and /api/admin/training/{id} routes."""
    method = request.method
    path = str(request.url.path)
    parts = path.rstrip("/").split("/")
    # parts: ['', 'api', 'admin', 'training', maybe id]
    example_id = parts[4] if len(parts) > 4 else ""

    try:
        if method == "GET" and not example_id:
            limit = int(request.query_params.get("limit", "50"))
            unannotated = request.query_params.get("unannotated", "false").lower() == "true"
            examples = await training_store.list_examples(DEFAULT_TENANT, limit=limit)
            if unannotated:
                examples = [e for e in examples if not e.admin_override_skill]
            return JSONResponse(
                {
                    "examples": [
                        {
                            "example_id": e.example_id,
                            "message_text": e.message_text,
                            "timestamp": e.timestamp,
                            "matched_skill": e.matched_skill,
                            "matched_action": e.matched_action,
                            "was_disabled_skill": e.was_disabled_skill,
                            "confidence": e.confidence,
                            "admin_override_skill": e.admin_override_skill,
                            "admin_override_action": e.admin_override_action,
                        }
                        for e in examples
                    ],
                    "count": len(examples),
                }
            )

        elif method == "PATCH" and example_id:
            body = await request.json()
            skill = body.get("skill", "")
            action = body.get("action", "")
            found = await training_store.annotate_example(DEFAULT_TENANT, example_id, skill, action)
            if not found:
                return JSONResponse({"error": "Example not found"}, status_code=404)
            return JSONResponse({"example_id": example_id, "annotated": True})

        elif method == "DELETE" and example_id:
            found = await training_store.delete_example(DEFAULT_TENANT, example_id)
            if not found:
                return JSONResponse({"error": "Example not found"}, status_code=404)
            return JSONResponse({"example_id": example_id, "deleted": True})

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
            _fire_and_forget(_rebuild_rules(DEFAULT_TENANT))
            return JSONResponse({"rebuilding": True, "tenant_id": DEFAULT_TENANT})

        if method == "GET" and path.endswith("/status"):
            rule_set = await rule_store.load_rule_set(DEFAULT_TENANT)
            engine = _compiled_engines.get(DEFAULT_TENANT)
            return JSONResponse(
                {
                    "tenant_id": DEFAULT_TENANT,
                    "version": rule_set.version if rule_set else 0,
                    "generated_at": rule_set.generated_at if rule_set else None,
                    "skill_count": len(rule_set.rules) if rule_set else 0,
                    "engine_loaded": engine is not None,
                }
            )

        return JSONResponse({"error": "Not found"}, status_code=404)
    except Exception as e:
        logger.exception("Rules admin error")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Teams webhook
# ---------------------------------------------------------------------------


async def handle_teams_webhook(request: Request) -> Response:
    """Handle incoming Teams webhook for local development."""
    try:
        body_bytes = await request.body()
        activity = json.loads(body_bytes) if body_bytes else {}
        activity_type = activity.get("type", "")
        logger.info(f"Teams webhook (local): type={activity_type}")

        teams_adapter = await _get_teams_adapter_local()
        if not teams_adapter:
            logger.warning("No Teams config — using mock adapter for emulator")
            teams_adapter = TeamsAdapter("local-test-app", "local-test-secret")

        if TeamsAdapter.is_message_activity(activity):
            await _handle_teams_message_local(teams_adapter, activity)
        elif TeamsAdapter.is_bot_added(activity):
            logger.info("Teams: bot added to conversation (local)")

        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("Teams webhook error (local)")
        return JSONResponse({"error": str(e)}, status_code=500)


async def _get_teams_adapter_local() -> TeamsAdapter | None:
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
    return None


async def _handle_teams_message_local(
    teams_adapter: TeamsAdapter, activity: dict[str, Any]
) -> None:
    from agent.models.message import OutboundMessage

    message = teams_adapter.parse_inbound(activity)
    text = message.text
    if not text:
        return

    tenant = await tenants.get_tenant(DEFAULT_TENANT)
    tenant_id = tenant.tenant_id
    conversation_id = f"teams-{message.conversation_id}"
    logger.info(f"Teams [{tenant_id}]: {text[:100]}")

    clean_text, is_raw = strip_raw_flag(text)
    active_provider, active_model, model_short_name = _resolve_model(tenant)
    provider_ai = ai.for_provider(active_provider)
    system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
You are communicating via Microsoft Teams. Keep responses clear and well-formatted.
When you have data to present, format it clearly with structure."""
    history = _strip_metadata(await memory.get_conversation(tenant_id, conversation_id))

    teams_engine = _get_engine(tenant_id)
    if not is_raw and is_conversational(clean_text):
        stats["conversational"] += 1
        messages = history + [{"role": "user", "content": clean_text}]
        response = await provider_ai.chat(active_model, system, messages, [])
        assistant_text = response.text or "Hey! How can I help?"
        total_tokens = response.input_tokens + response.output_tokens
    else:
        match = (
            teams_engine.match(clean_text, tenant.settings.enabled_skills) if teams_engine else None
        )
        if match:
            request_id = f"teams-rule-{conversation_id}"
            await bus.publish_skill_invocation(
                tenant_id,
                match.skill_name,
                match.params,
                conversation_id,
                request_id,
                "teams",
                message.channel_user_id,
            )
            skill_result = bus.get_result(request_id) or {"error": "No result"}
            stats["rule_routed"] += 1
            if is_raw and teams_engine and teams_engine.supports_raw(match.skill_name):
                stats["raw"] += 1
                assistant_text = _format_raw_json(skill_result)
                total_tokens = 0
            else:
                prompt = (
                    f'{system}\n\nThe user asked: "{clean_text}"\n\n'
                    f"Tool data:\n{json.dumps(skill_result, indent=2)}\n\nFormat this clearly."
                )
                messages = history + [{"role": "user", "content": prompt}]
                response = await provider_ai.chat(active_model, system, messages, [])
                assistant_text = response.text or "Got data but couldn't format."
                total_tokens = response.input_tokens + response.output_tokens
        elif teams_engine and (disabled_skill := teams_engine.check_disabled_skill(clean_text)):
            skill_display = disabled_skill.replace("_", " ")
            assistant_text = (
                f"The {skill_display} feature isn't enabled for your workspace. "
                f"Contact your admin to enable it."
            )
            total_tokens = 0
            _fire_and_forget(
                _log_training(tenant_id, clean_text, None, None, was_disabled_skill=True)
            )
        else:
            stats["ai_routed"] += 1
            tools = skills.get_tools_for_tenant(type("C", (), {"tenant": tenant})())
            messages = history + [{"role": "user", "content": clean_text}]
            response = await provider_ai.chat(active_model, system, messages, tools)
            if response.has_tool_use:
                tc = response.tool_calls[0]
                request_id = f"teams-ai-{conversation_id}"
                await bus.publish_skill_invocation(
                    tenant_id,
                    tc.tool_name,
                    tc.tool_params,
                    conversation_id,
                    request_id,
                    "teams",
                    message.channel_user_id,
                )
                skill_result = bus.get_result(request_id) or {"error": "No result"}
                _fire_and_forget(
                    _log_training(
                        tenant_id,
                        clean_text,
                        tc.tool_name,
                        tc.tool_params.get("action"),
                    )
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
                    active_model, system, messages_with_tool, tools, tc.tool_use_id, skill_result
                )
                assistant_text = final.text or "Got data but couldn't format."
                total_tokens = (
                    response.input_tokens
                    + response.output_tokens
                    + final.input_tokens
                    + final.output_tokens
                )
            else:
                _fire_and_forget(_log_training(tenant_id, clean_text, None, None))
                assistant_text = response.text or "Not sure how to help."
                total_tokens = response.input_tokens + response.output_tokens

    stats["total_tokens"] += total_tokens
    await memory.save_turn(
        tenant_id,
        conversation_id,
        clean_text,
        assistant_text,
        metadata={"route": "teams", "model": model_short_name, "tokens": total_tokens},
    )
    outbound = OutboundMessage(
        channel=ChannelType.TEAMS,
        conversation_id=message.conversation_id,
        recipient_id=message.channel_user_id,
        text=assistant_text,
    )
    await teams_adapter.send_response(outbound)


# ---------------------------------------------------------------------------
# Telegram webhook
# ---------------------------------------------------------------------------


async def handle_telegram_webhook(request: Request) -> Response:
    """Handle incoming Telegram webhook for local development."""
    try:
        body_bytes = await request.body()
        update = json.loads(body_bytes) if body_bytes else {}
        logger.info(f"Telegram webhook (local): update_id={update.get('update_id', '?')}")

        adapter = await _get_telegram_adapter_local()
        if not adapter:
            logger.warning("No Telegram config found")
            return JSONResponse({"error": "Telegram not configured"}, status_code=400)

        if TelegramAdapter.is_message_update(update):
            await _handle_telegram_message_local(adapter, update)

        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("Telegram webhook error (local)")
        return JSONResponse({"error": str(e)}, status_code=500)


async def _get_telegram_adapter_local() -> TelegramAdapter | None:
    try:
        creds = await secrets.get(DEFAULT_TENANT, "telegram")
        bot_token = creds.get("bot_token", "")
        if bot_token:
            return TelegramAdapter(bot_token, creds.get("webhook_secret", ""))
    except Exception:
        pass
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if bot_token:
        return TelegramAdapter(bot_token, os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""))
    return None


async def _handle_telegram_message_local(adapter: TelegramAdapter, update: dict[str, Any]) -> None:
    from agent.models.message import OutboundMessage

    message = adapter.parse_inbound(update)
    text = message.text
    if not text:
        return

    tenant = await tenants.get_tenant(DEFAULT_TENANT)
    tenant_id = tenant.tenant_id
    conversation_id = f"tg-{message.conversation_id}"
    logger.info(f"Telegram [{tenant_id}]: {text[:100]}")

    await adapter.send_typing_indicator(message.conversation_id)

    clean_text, is_raw = strip_raw_flag(text)
    active_provider, active_model, model_short_name = _resolve_model(tenant)
    provider_ai = ai.for_provider(active_provider)
    system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
You are communicating via Telegram. Keep responses concise and well-formatted.
Use Markdown sparingly — Telegram supports *bold*, _italic_, and `code`."""
    history = _strip_metadata(await memory.get_conversation(tenant_id, conversation_id))

    tg_engine = _get_engine(tenant_id)
    if not is_raw and is_conversational(clean_text):
        stats["conversational"] += 1
        messages = history + [{"role": "user", "content": clean_text}]
        response = await provider_ai.chat(active_model, system, messages, [])
        assistant_text = response.text or "Hey! How can I help?"
        total_tokens = response.input_tokens + response.output_tokens
    else:
        match = tg_engine.match(clean_text, tenant.settings.enabled_skills) if tg_engine else None
        if match:
            request_id = f"tg-rule-{conversation_id}"
            await bus.publish_skill_invocation(
                tenant_id,
                match.skill_name,
                match.params,
                conversation_id,
                request_id,
                "telegram",
                message.channel_user_id,
            )
            skill_result = bus.get_result(request_id) or {"error": "No result"}
            stats["rule_routed"] += 1
            if is_raw and tg_engine and tg_engine.supports_raw(match.skill_name):
                stats["raw"] += 1
                assistant_text = _format_raw_json(skill_result)
                total_tokens = 0
            else:
                prompt = (
                    f'{system}\n\nThe user asked: "{clean_text}"\n\n'
                    f"Tool data:\n{json.dumps(skill_result, indent=2)}\n\n"
                    f"Format this clearly and concisely for Telegram."
                )
                messages = history + [{"role": "user", "content": prompt}]
                response = await provider_ai.chat(active_model, system, messages, [])
                assistant_text = response.text or "Got data but couldn't format."
                total_tokens = response.input_tokens + response.output_tokens
        elif tg_engine and (disabled_skill := tg_engine.check_disabled_skill(clean_text)):
            skill_display = disabled_skill.replace("_", " ")
            assistant_text = (
                f"The {skill_display} feature isn't enabled for your workspace. "
                f"Contact your admin to enable it."
            )
            total_tokens = 0
            _fire_and_forget(
                _log_training(tenant_id, clean_text, None, None, was_disabled_skill=True)
            )
        else:
            stats["ai_routed"] += 1
            tools = skills.get_tools_for_tenant(type("C", (), {"tenant": tenant})())
            messages = history + [{"role": "user", "content": clean_text}]
            response = await provider_ai.chat(active_model, system, messages, tools)
            if response.has_tool_use:
                tc = response.tool_calls[0]
                request_id = f"tg-ai-{conversation_id}"
                await bus.publish_skill_invocation(
                    tenant_id,
                    tc.tool_name,
                    tc.tool_params,
                    conversation_id,
                    request_id,
                    "telegram",
                    message.channel_user_id,
                )
                skill_result = bus.get_result(request_id) or {"error": "No result"}
                _fire_and_forget(
                    _log_training(
                        tenant_id,
                        clean_text,
                        tc.tool_name,
                        tc.tool_params.get("action"),
                    )
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
                    active_model, system, messages_with_tool, tools, tc.tool_use_id, skill_result
                )
                assistant_text = final.text or "Got data but couldn't format."
                total_tokens = (
                    response.input_tokens
                    + response.output_tokens
                    + final.input_tokens
                    + final.output_tokens
                )
            else:
                _fire_and_forget(_log_training(tenant_id, clean_text, None, None))
                assistant_text = response.text or "Not sure how to help."
                total_tokens = response.input_tokens + response.output_tokens

    stats["total_tokens"] += total_tokens
    await memory.save_turn(
        tenant_id,
        conversation_id,
        clean_text,
        assistant_text,
        metadata={"route": "telegram", "model": model_short_name, "tokens": total_tokens},
    )
    outbound = OutboundMessage(
        channel=ChannelType.TELEGRAM,
        conversation_id=message.conversation_id,
        recipient_id=message.channel_user_id,
        text=assistant_text,
    )
    await adapter.send_response(outbound)


# ---------------------------------------------------------------------------
# Clinical API handlers
# ---------------------------------------------------------------------------


async def handle_skill_invoke(request: Request) -> Response:
    """POST /api/skill/{name} — invoke a skill synchronously from practice pages."""
    skill_name = request.path_params["name"]
    try:
        body = await request.json()
        worker_fn = skills.get_worker(skill_name)

        # Get secrets if the skill requires an integration
        skill = skills.get_skill(skill_name)
        skill_secrets: dict[str, Any] = {}
        if skill and skill.requires_integration:
            try:
                skill_secrets = await secrets.get(DEFAULT_TENANT, skill.requires_integration)
            except Exception:
                pass

        # Build context with tenant and blob store
        ctx = {"blob_store": blobs, "tenant_id": DEFAULT_TENANT}
        sig = inspect.signature(worker_fn)
        if len(sig.parameters) >= 3:
            result = worker_fn(body, skill_secrets, ctx)
        else:
            result = worker_fn(body, skill_secrets)

        # Support async workers
        if asyncio.iscoroutine(result):
            result = await result

        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Skill invoke failed ({skill_name}): {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_practices_list(request: Request) -> Response:
    """GET /api/practices — list all installed practices."""
    result = []
    for p in practices.list_all():
        result.append(
            {
                "name": p.name,
                "display_name": p.display_name,
                "description": p.description,
                "version": p.version,
                "icon": p.icon,
                "built_in": p.built_in,
                "skills": p.skills,
                "pages": [
                    {"slug": pg.slug, "title": pg.title, "nav_label": pg.nav_label}
                    for pg in p.pages
                ],
            }
        )
    return JSONResponse(result)


async def handle_practices_pages(request: Request) -> Response:
    """GET /api/practices/pages — pages available to current tenant."""
    try:
        tenant = await tenants.get_tenant(DEFAULT_TENANT)
        pages = practices.get_pages_for_tenant(tenant)
        return JSONResponse(pages)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_practices_upload(request: Request) -> Response:
    """POST /api/practices/upload — upload and install a practice ZIP."""
    try:
        body = await request.body()
        data_dir = Path("data")
        practice = practices.install_zip(body, data_dir)
        # Register the new practice's skills
        practices.register_skills(skills)
        return JSONResponse(
            {
                "ok": True,
                "name": practice.name,
                "version": practice.version,
                "skills": practice.skills,
            }
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_blob_upload(request: Request) -> Response:
    """POST /api/blobs/{key:path} — upload a binary blob to BlobStore."""
    key = request.path_params["key"]
    try:
        body = await request.body()
        await blobs.put(DEFAULT_TENANT, key, body)
        return JSONResponse({"ok": True, "key": key, "size": len(body)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_blob_read(request: Request) -> Response:
    """GET /api/blobs/{key:path} — read a binary blob from BlobStore."""
    key = request.path_params["key"]
    try:
        data = await blobs.get(DEFAULT_TENANT, key)
        # Guess content type from key extension
        ct = "application/octet-stream"
        if key.endswith(".json"):
            ct = "application/json"
        elif key.endswith(".webm"):
            ct = "audio/webm"
        elif key.endswith(".html"):
            ct = "text/html"
        return Response(data, media_type=ct)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=404)


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
        started_at

    started_at = time.time()

    # Load .env
    secrets = EnvSecretsProvider(".env")

    # Initialize AI providers — both can run simultaneously
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

    # Load skills — base skills (ping) from agent/skills/
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

    # Direct bus — with context for practice skills (BlobStore access)
    bus = DirectBus(skills, secrets, context={"blob_store": blobs})

    # Register channels
    channels = ChannelRegistry()
    channels.register(DashboardAdapter())

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
            logger.info(f"No rules cached for '{t.tenant_id}' — generating via AI...")
            await _rebuild_rules(t.tenant_id)


def main() -> None:
    asyncio.run(init())

    port = int(os.getenv("PORT", "8080"))
    logger.info("")
    logger.info("  ╔══════════════════════════════════════╗")
    logger.info("  ║  T3nets Dev Server                   ║")
    logger.info("  ║                                      ║")
    logger.info(f"  ║  Chat:   http://localhost:{port}       ║")
    logger.info(f"  ║  Health: http://localhost:{port}/health ║")
    logger.info("  ║                                      ║")
    logger.info("  ║  Routing: Rules → Claude (hybrid)    ║")
    logger.info("  ║  Debug:   append --raw to messages   ║")
    logger.info("  ║  SSE:     /api/events (async push)   ║")
    logger.info("  ╚══════════════════════════════════════╝")
    logger.info("")

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning", timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
