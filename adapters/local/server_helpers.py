"""Module-level helpers extracted from dev_server.py.

Pure utilities or thin wrappers that operate on injected dependencies, so
the dev server module stays small.
"""

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, Response

from agent.channels.teams import TeamsAdapter
from agent.channels.telegram import TelegramAdapter
from agent.models.ai_models import (
    DEFAULT_MODEL_ID,
    get_model,
    get_model_for_provider,
)

logger = logging.getLogger("t3nets.local.helpers")


def file_response(filename: str, search_dir: str | None = None) -> Response:
    base = Path(__file__).parent.parent.parent
    path = base / search_dir / filename if search_dir else base / filename
    if path.exists():
        return FileResponse(str(path), media_type="text/html")
    return Response(status_code=404, content=f"{filename} not found")


def serve_static(filename: str, media_type: str) -> Response:
    base = Path(__file__).parent.parent.parent / "adapters/local"
    path = base / filename
    if path.exists():
        return FileResponse(str(path), media_type=media_type)
    return Response(status_code=404, content=f"{filename} not found")


def extract_user_key(request: Request, default: str) -> str:
    user_key = default
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


def resolve_model(tenant: Any, *, ai: Any) -> tuple[str, str, str]:
    """Resolve the tenant's ai_model to (provider_name, api_model_id, short_name)."""
    model_id = tenant.settings.ai_model or DEFAULT_MODEL_ID
    model = get_model(model_id)
    active = ai.active_providers

    selected_provider: str | None = None
    if model:
        for p in active:
            if p in model.providers:
                selected_provider = p
                break

    if not selected_provider:
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


def enrich_match_params(match: Any, clean_text: str, *, skills: Any) -> None:
    if not match:
        return
    skill_def = skills.get_skill(match.skill_name)
    if skill_def:
        schema_props = skill_def.parameters.get("properties", {})
        if "text" in schema_props and "text" not in match.params:
            match.params["text"] = clean_text


async def resolve_auth_single_tenant(request: Request, default_tenant: str) -> tuple[str, str]:
    """Single-tenant auth resolver: always default_tenant + extract email from JWT."""
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
    return (default_tenant, user_email)


def api_key_preview() -> str:
    """Safe preview of the Anthropic API key for the health endpoint."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if len(api_key) > 12:
        return api_key[:8] + "..." + api_key[-4:]
    return "not set" if not api_key else "***"


# ---------------------------------------------------------------------------
# Local channel adapter resolvers
# ---------------------------------------------------------------------------


async def get_teams_adapter_local(default_tenant: str, secrets: Any) -> TeamsAdapter | None:
    try:
        creds = await secrets.get(default_tenant, "teams")
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
    return TeamsAdapter("local-test-app", "local-test-secret")


async def get_telegram_adapter_local(
    token_hash: str, default_tenant: str, secrets: Any
) -> TelegramAdapter | None:
    import hashlib

    try:
        creds = await secrets.get(default_tenant, "telegram")
        bot_token = creds.get("bot_token", "")
        if bot_token:
            computed = hashlib.sha256(bot_token.encode()).hexdigest()[:16]
            if computed == token_hash or not token_hash:
                return TelegramAdapter(bot_token, creds.get("webhook_secret", ""))
    except Exception:
        pass
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if bot_token:
        return TelegramAdapter(bot_token, os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""))
    return None


# ---------------------------------------------------------------------------
# SSE bridge
# ---------------------------------------------------------------------------


class QueueBridge:
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
