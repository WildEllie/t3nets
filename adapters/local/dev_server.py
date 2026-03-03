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
from adapters.local.sqlite_store import SQLiteConversationStore
from adapters.local.sqlite_tenant_store import SQLiteTenantStore
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
    get_models_for_provider,
)
from agent.models.message import ChannelType
from agent.models.tenant import Invitation
from agent.router.rule_router import RuleBasedRouter, strip_raw_flag
from agent.skills.registry import SkillRegistry
from agent.sse import SSEConnectionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("t3nets.dev")

# --- Global state (initialized in main) ---
ai: AnthropicProvider
memory: SQLiteConversationStore
tenants: SQLiteTenantStore
secrets: EnvSecretsProvider
skills: SkillRegistry
bus: DirectBus
rule_router: RuleBasedRouter
error_handler: ErrorHandler
started_at: float = 0.0

DEFAULT_TENANT = "local"
DEFAULT_CONVERSATION = "dashboard-default"
PROVIDER = "anthropic"

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


def _resolve_model(tenant: Any) -> tuple[str, str]:
    """Resolve the tenant's ai_model setting to an Anthropic API model ID and short name."""
    model_id = tenant.settings.ai_model or DEFAULT_MODEL_ID
    model = get_model(model_id)
    if not model:
        logger.warning(f"Unknown model '{model_id}', falling back to {DEFAULT_MODEL_ID}")
        model_id = DEFAULT_MODEL_ID
        model = get_model(model_id)
    assert model is not None, f"Default model {DEFAULT_MODEL_ID} not found in registry"
    api_id = get_model_for_provider(model_id, PROVIDER)
    return api_id or model.anthropic_id, model.short_name


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


async def serve_logo(request: Request) -> Response:
    base = Path(__file__).parent.parent.parent
    path = base / "adapters/local/logo.png"
    if path.exists():
        return FileResponse(str(path), media_type="image/png")
    return Response(status_code=404, content="logo not found")


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
            skills_info.append({
                "name": skill.name,
                "description": skill.description.strip()[:120],
                "requires_integration": skill.requires_integration,
                "supports_raw": skill.supports_raw,
                "triggers": skill.triggers[:8],
            })

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
                "provider": "anthropic (direct)",
                "model": _resolve_model(tenant)[0],
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
    return JSONResponse({
        "enabled": False,
        "client_id": "",
        "auth_domain": "",
        "user_pool_id": "",
    })


async def handle_auth_me(request: Request) -> Response:
    tenant = await tenants.get_tenant(DEFAULT_TENANT)
    return JSONResponse({
        "authenticated": True,
        "user_id": "local-admin",
        "tenant_id": DEFAULT_TENANT,
        "email": "admin@local.dev",
        "role": "admin",
        "tenant_status": tenant.status,
        "tenant_name": tenant.name,
    })


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
        return JSONResponse({
            "ai_model": s.ai_model or DEFAULT_MODEL_ID,
            "provider": PROVIDER,
            "models": get_models_for_provider(PROVIDER),
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
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_settings_post(request: Request) -> Response:
    """Update tenant settings."""
    try:
        body = await request.json()
        tenant = await tenants.get_tenant(DEFAULT_TENANT)
        changed = False

        if "ai_model" in body:
            model_id = body["ai_model"]
            model = get_model(model_id)
            if not model:
                return JSONResponse({"error": f"Unknown model: {model_id}"}, status_code=400)
            if PROVIDER not in model.providers:
                return JSONResponse(
                    {"error": f"Model '{model_id}' not available for {PROVIDER}"}, status_code=400
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
        return JSONResponse({"ok": True})

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_history(request: Request) -> Response:
    """Return conversation history for the default conversation."""
    try:
        history = await memory.get_conversation(DEFAULT_TENANT, DEFAULT_CONVERSATION)
        return JSONResponse({
            "messages": history,
            "platform": os.getenv("T3NETS_PLATFORM", "local"),
            "stage": os.getenv("T3NETS_STAGE", "dev"),
        })
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
            result.append({
                "name": name,
                "label": schema["label"],
                "connected": name in connected,
                "fields": schema["fields"],
            })
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
        return JSONResponse({
            "name": integration_name,
            "label": schema["label"],
            "connected": connected,
            "config": config,
            "fields": schema["fields"],
        })
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
        active_model, model_short_name = _resolve_model(tenant)

        system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
When you have data to present, format it clearly with structure."""

        # === TIER 0: Conversational ===
        if not is_raw and rule_router.is_conversational(clean_text):
            logger.info("Route: CONVERSATIONAL (no tools)")
            stats["conversational"] += 1
            messages = history + [{"role": "user", "content": clean_text}]
            response = await ai.chat(
                model=active_model, system=system, messages=messages, tools=[]
            )
            assistant_text = response.text or "Hey! How can I help?"
            total_tokens = response.input_tokens + response.output_tokens
            route_type = "conversational"

        else:
            # === TIER 1: Rule-based routing ===
            match = rule_router.match(clean_text, tenant.settings.enabled_skills)

            if match:
                logger.info(
                    f"Route: RULE-BASED → {match.skill_name}.{match.action} "
                    f"(confidence={match.confidence:.2f})"
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

                if is_raw and rule_router.supports_raw(match.skill_name):
                    logger.info(f"Returning raw output for {match.skill_name}")
                    stats["raw"] += 1
                    stats["rule_routed"] += 1
                    assistant_text = _format_raw_json(skill_result)
                    total_tokens = 0
                    route_type = "rule"
                    is_raw_response = True
                else:
                    if is_raw and not rule_router.supports_raw(match.skill_name):
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
                    response = await ai.chat(
                        model=active_model, system=system, messages=messages, tools=[]
                    )
                    assistant_text = response.text or "Got the data but couldn't format it."
                    total_tokens = response.input_tokens + response.output_tokens
                    route_type = "rule"

            # === TIER 2: Full Claude routing ===
            else:
                if is_raw:
                    logger.info("--raw flag ignored: no rule match, using AI routing")
                logger.info("Route: AI (full Claude with tools)")
                stats["ai_routed"] += 1

                tools = skills.get_tools_for_tenant(type("Ctx", (), {"tenant": tenant})())
                messages = history + [{"role": "user", "content": clean_text}]
                response = await ai.chat(
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

                    if is_raw and rule_router.supports_raw(tool_call.tool_name):
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
                        final_response = await ai.chat_with_tool_result(
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
                DEFAULT_TENANT, conversation_id, clean_text, assistant_text,
                metadata=chat_metadata,
            )

        return JSONResponse({
            "text": assistant_text,
            "conversation_id": conversation_id,
            "tokens": total_tokens,
            "route": route_type,
            "raw": is_raw_response,
            "model": model_short_name,
            "user_email": user_email,
        })

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
        return JSONResponse({
            "valid": True,
            "tenant_name": tenant_name,
            "tenant_id": invitation.tenant_id,
            "email": invitation.email,
            "role": invitation.role,
        })
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
            return JSONResponse({
                "accepted": True,
                "tenant_id": invitation.tenant_id,
                "already_member": True,
            })

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

        return JSONResponse({
            "accepted": True,
            "tenant_id": invitation.tenant_id,
            "user_id": user_id,
            "role": invitation.role,
        })
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
    return JSONResponse({
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
    })


async def _admin_list_users(path: str) -> Response:
    parts = path.rstrip("/").split("/")
    tenant_id = parts[4]
    users = await tenants.list_users(tenant_id)
    return JSONResponse({
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
    })


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

    return JSONResponse({
        "invite_code": invitation.invite_code,
        "invite_url": invite_url,
        "email": email,
        "role": role,
        "expires_at": invitation.expires_at,
    }, status_code=201)


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
            result.append({
                "tenant_id": t.tenant_id,
                "name": t.name,
                "status": t.status,
                "created_at": t.created_at,
                "user_count": user_count,
            })
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

        return JSONResponse({
            "tenant_id": tenant_id,
            "tenant_name": tenant_name,
            "invite_code": invitation.invite_code,
            "invite_url": invite_url,
            "admin_name": admin_name,
            "admin_email": admin_email,
        }, status_code=201)
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
                return JSONResponse(
                    {"error": "Cannot delete the default tenant"}, status_code=400
                )
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


async def _handle_teams_message_local(teams_adapter: TeamsAdapter, activity: dict[str, Any]) -> None:
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
    active_model, model_short_name = _resolve_model(tenant)
    system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
You are communicating via Microsoft Teams. Keep responses clear and well-formatted.
When you have data to present, format it clearly with structure."""
    history = _strip_metadata(await memory.get_conversation(tenant_id, conversation_id))

    if not is_raw and rule_router.is_conversational(clean_text):
        stats["conversational"] += 1
        messages = history + [{"role": "user", "content": clean_text}]
        response = await ai.chat(active_model, system, messages, [])
        assistant_text = response.text or "Hey! How can I help?"
        total_tokens = response.input_tokens + response.output_tokens
    else:
        match = rule_router.match(clean_text, tenant.settings.enabled_skills)
        if match:
            request_id = f"teams-rule-{conversation_id}"
            await bus.publish_skill_invocation(
                tenant_id, match.skill_name, match.params,
                conversation_id, request_id, "teams", message.channel_user_id,
            )
            skill_result = bus.get_result(request_id) or {"error": "No result"}
            stats["rule_routed"] += 1
            if is_raw and rule_router.supports_raw(match.skill_name):
                stats["raw"] += 1
                assistant_text = _format_raw_json(skill_result)
                total_tokens = 0
            else:
                prompt = (
                    f'{system}\n\nThe user asked: "{clean_text}"\n\n'
                    f'Tool data:\n{json.dumps(skill_result, indent=2)}\n\nFormat this clearly.'
                )
                messages = history + [{"role": "user", "content": prompt}]
                response = await ai.chat(active_model, system, messages, [])
                assistant_text = response.text or "Got data but couldn't format."
                total_tokens = response.input_tokens + response.output_tokens
        else:
            stats["ai_routed"] += 1
            tools = skills.get_tools_for_tenant(type("C", (), {"tenant": tenant})())
            messages = history + [{"role": "user", "content": clean_text}]
            response = await ai.chat(active_model, system, messages, tools)
            if response.has_tool_use:
                tc = response.tool_calls[0]
                request_id = f"teams-ai-{conversation_id}"
                await bus.publish_skill_invocation(
                    tenant_id, tc.tool_name, tc.tool_params,
                    conversation_id, request_id, "teams", message.channel_user_id,
                )
                skill_result = bus.get_result(request_id) or {"error": "No result"}
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
                final = await ai.chat_with_tool_result(
                    active_model, system, messages_with_tool, tools, tc.tool_use_id, skill_result
                )
                assistant_text = final.text or "Got data but couldn't format."
                total_tokens = (
                    response.input_tokens + response.output_tokens
                    + final.input_tokens + final.output_tokens
                )
            else:
                assistant_text = response.text or "Not sure how to help."
                total_tokens = response.input_tokens + response.output_tokens

    stats["total_tokens"] += total_tokens
    await memory.save_turn(
        tenant_id, conversation_id, clean_text, assistant_text,
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
    active_model, model_short_name = _resolve_model(tenant)
    system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
You are communicating via Telegram. Keep responses concise and well-formatted.
Use Markdown sparingly — Telegram supports *bold*, _italic_, and `code`."""
    history = _strip_metadata(await memory.get_conversation(tenant_id, conversation_id))

    if not is_raw and rule_router.is_conversational(clean_text):
        stats["conversational"] += 1
        messages = history + [{"role": "user", "content": clean_text}]
        response = await ai.chat(active_model, system, messages, [])
        assistant_text = response.text or "Hey! How can I help?"
        total_tokens = response.input_tokens + response.output_tokens
    else:
        match = rule_router.match(clean_text, tenant.settings.enabled_skills)
        if match:
            request_id = f"tg-rule-{conversation_id}"
            await bus.publish_skill_invocation(
                tenant_id, match.skill_name, match.params,
                conversation_id, request_id, "telegram", message.channel_user_id,
            )
            skill_result = bus.get_result(request_id) or {"error": "No result"}
            stats["rule_routed"] += 1
            if is_raw and rule_router.supports_raw(match.skill_name):
                stats["raw"] += 1
                assistant_text = _format_raw_json(skill_result)
                total_tokens = 0
            else:
                prompt = (
                    f'{system}\n\nThe user asked: "{clean_text}"\n\n'
                    f'Tool data:\n{json.dumps(skill_result, indent=2)}\n\n'
                    f'Format this clearly and concisely for Telegram.'
                )
                messages = history + [{"role": "user", "content": prompt}]
                response = await ai.chat(active_model, system, messages, [])
                assistant_text = response.text or "Got data but couldn't format."
                total_tokens = response.input_tokens + response.output_tokens
        else:
            stats["ai_routed"] += 1
            tools = skills.get_tools_for_tenant(type("C", (), {"tenant": tenant})())
            messages = history + [{"role": "user", "content": clean_text}]
            response = await ai.chat(active_model, system, messages, tools)
            if response.has_tool_use:
                tc = response.tool_calls[0]
                request_id = f"tg-ai-{conversation_id}"
                await bus.publish_skill_invocation(
                    tenant_id, tc.tool_name, tc.tool_params,
                    conversation_id, request_id, "telegram", message.channel_user_id,
                )
                skill_result = bus.get_result(request_id) or {"error": "No result"}
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
                final = await ai.chat_with_tool_result(
                    active_model, system, messages_with_tool, tools, tc.tool_use_id, skill_result
                )
                assistant_text = final.text or "Got data but couldn't format."
                total_tokens = (
                    response.input_tokens + response.output_tokens
                    + final.input_tokens + final.output_tokens
                )
            else:
                assistant_text = response.text or "Not sure how to help."
                total_tokens = response.input_tokens + response.output_tokens

    stats["total_tokens"] += total_tokens
    await memory.save_turn(
        tenant_id, conversation_id, clean_text, assistant_text,
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
# Starlette app
# ---------------------------------------------------------------------------

routes = [
    # Static pages
    Route("/", homepage),
    Route("/chat", homepage),
    Route("/logo.png", serve_logo),
    Route("/health", health_page),
    Route("/settings", settings_page),
    Route("/onboard", onboard_page),
    Route("/platform", platform_page),
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
    # Admin routes
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
    global ai, memory, tenants, secrets, skills, bus, rule_router, error_handler, started_at

    started_at = time.time()

    # Load .env
    secrets = EnvSecretsProvider(".env")

    # Check for API key
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set. Copy .env.example to .env and add your key.")
        sys.exit(1)

    # Initialize adapters
    ai = AnthropicProvider(api_key)
    memory = SQLiteConversationStore("data/t3nets.db")
    tenants = SQLiteTenantStore("data/t3nets.db")

    # Load skills
    skills = SkillRegistry()
    skills_dir = Path(__file__).parent.parent.parent / "agent" / "skills"
    skills.load_from_directory(skills_dir)
    logger.info(f"Loaded skills: {skills.list_skill_names()}")

    # Rule-based router and error handler
    rule_router = RuleBasedRouter(skills, confidence_threshold=0.5)
    error_handler = ErrorHandler()

    # Direct bus
    bus = DirectBus(skills, secrets)

    # Register channels
    channels = ChannelRegistry()
    channels.register(DashboardAdapter())

    # Seed default tenant
    tenant = tenants.seed_default_tenant(
        tenant_id="local",
        name="Dev",
        enabled_skills=skills.list_skill_names(),
    )
    if not tenant.settings.ai_model:
        tenant.settings.ai_model = DEFAULT_MODEL_ID
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

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
