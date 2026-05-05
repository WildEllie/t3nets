"""Module-level helpers extracted from server.py.

These are utility functions and small classes that operate on injected
dependencies (tenants store, AI provider, etc.) rather than on module
globals. The server passes the relevant pieces in at call time.
"""

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from adapters.aws.auth_middleware import AuthError, extract_auth
from agent.models.ai_models import (
    DEFAULT_MODEL_ID,
    get_model,
    get_model_for_provider,
)

logger = logging.getLogger("t3nets.aws.helpers")


def bedrock_geo_prefix(region: str) -> str:
    """Map AWS region to Bedrock geographic inference profile prefix."""
    if region.startswith("us-") or region.startswith("ca-") or region.startswith("sa-"):
        return "us"
    elif region.startswith("eu-"):
        return "eu"
    elif region.startswith("ap-"):
        return "apac"
    return "us"


def resolve_model(
    tenant: Any,
    *,
    ai: Any,
    aws_region: str,
    bedrock_model_id: str,
) -> tuple[str, str, str]:
    """Resolve tenant's ai_model to (provider_name, api_model_id, short_name).

    Picks the first active provider that supports the requested model.
    Falls back gracefully when the selected model isn't available.
    """
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

    if selected_provider == "ollama":
        ollama_id = get_model_for_provider(model_id, "ollama")
        return "ollama", ollama_id or model.ollama_id, model.short_name

    bedrock_id = get_model_for_provider(model_id, "bedrock")
    if bedrock_id:
        geo = bedrock_geo_prefix(aws_region)
        full_id = f"{geo}.{bedrock_id}"
        logger.info(f"Resolved model: {model_id} → {full_id}")
        return "bedrock", full_id, model.short_name
    return "bedrock", bedrock_model_id, model.short_name


async def get_auth_info(
    request: Request,
    *,
    tenants: Any,
    cognito_user_pool_id: str,
    default_tenant: str,
) -> tuple[str, str]:
    """Extract (tenant_id, user_email) from JWT in Authorization header."""
    if not cognito_user_pool_id:
        return default_tenant, ""
    try:
        auth = extract_auth(request.headers)
        email = auth.email
        try:
            user = await tenants.get_user_by_cognito_sub(auth.user_id)
            if user:
                logger.info(
                    f"Resolved tenant '{user.tenant_id}' from DynamoDB "
                    f"for sub {auth.user_id[:8]}..."
                )
                return user.tenant_id, email
        except Exception as e:
            logger.warning(f"DynamoDB sub lookup failed: {e}")
        return default_tenant, email
    except AuthError:
        return default_tenant, ""


def file_response(filename: str, search_dir: str | None = None) -> Response:
    base = Path(__file__).parent.parent.parent
    path = base / search_dir / filename if search_dir else base / filename
    if path.exists():
        return FileResponse(str(path), media_type="text/html")
    return Response(status_code=404, content=f"{filename} not found")


def extract_user_key(request: Request, default: str, body_token: str = "") -> str:
    """Extract user identity from JWT query param, Authorization header, or body token."""
    user_key = default
    token = request.query_params.get("token") or body_token
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


def enrich_match_params(match: Any, clean_text: str, *, skills: Any) -> None:
    """Inject original user text into match params for skills that expect a 'text' field."""
    if not match:
        return
    skill_def = skills.get_skill(match.skill_name)
    if skill_def:
        schema_props = skill_def.parameters.get("properties", {})
        if "text" in schema_props and "text" not in match.params:
            match.params["text"] = clean_text


# ---------------------------------------------------------------------------
# WebSocket bridge — API Gateway $connect/$disconnect events
# ---------------------------------------------------------------------------


class WebSocketEventMiddleware:
    """Intercept API Gateway WebSocket events (POST with X-WS-Route header)."""

    def __init__(self, app: ASGIApp, state_getter: Any) -> None:
        self._app = app
        self._state_getter = state_getter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") == "POST":
            headers = dict(scope["headers"])
            ws_route = headers.get(b"x-ws-route", b"").decode()
            if ws_route:
                request = Request(scope, receive)
                response = await _dispatch_ws_event(request, ws_route, self._state_getter())
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


async def _dispatch_ws_event(request: Request, ws_route: str, state: Any) -> Response:
    connection_id = request.headers.get("x-ws-connection-id", "")
    if ws_route == "$connect":
        return await _handle_ws_connect(request, connection_id, state)
    elif ws_route == "$disconnect":
        return await _handle_ws_disconnect(connection_id, state)
    return JSONResponse({"status": "ok"})


async def _handle_ws_connect(request: Request, connection_id: str, state: Any) -> Response:
    ws_manager = getattr(state, "ws_manager", None) if state else None
    if not connection_id or not ws_manager:
        return JSONResponse({"error": "WebSocket not configured"}, status_code=400)
    body_token = ""
    try:
        body = await request.json()
        body_token = body.get("token", "")
    except Exception:
        pass
    user_key = extract_user_key(request, state.default_tenant, body_token)
    ws_manager.register(user_key, connection_id)
    logger.info(f"WS $connect: {connection_id[:12]} user={user_key}")
    return JSONResponse({"status": "connected"})


async def _handle_ws_disconnect(connection_id: str, state: Any) -> Response:
    ws_manager = getattr(state, "ws_manager", None) if state else None
    if not connection_id or not ws_manager:
        return JSONResponse({"status": "ok"})
    ws_manager.unregister_by_connection_id(connection_id)
    logger.info(f"WS $disconnect: {connection_id[:12]}")
    return JSONResponse({"status": "disconnected"})


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


# ---------------------------------------------------------------------------
# Lambda deployment config
# ---------------------------------------------------------------------------


def get_lambda_deploy_config() -> dict[str, Any]:
    """Build Lambda deployment config from environment variables."""
    project = os.environ.get("T3NETS_STAGE", "dev")
    name_prefix = f"t3nets-{project}"
    subnet_str = os.environ.get("LAMBDA_SUBNET_IDS", "")
    return {
        "region": os.environ.get("AWS_REGION", "us-east-1"),
        "name_prefix": name_prefix,
        "stage": project,
        "lambda_role_arn": os.environ.get("LAMBDA_ROLE_ARN", ""),
        "eventbridge_bus_name": os.environ.get("EVENTBRIDGE_BUS_NAME", ""),
        "eventbridge_bus_arn": os.environ.get("EVENTBRIDGE_BUS_ARN", ""),
        "eventbridge_dlq_arn": os.environ.get("EVENTBRIDGE_DLQ_ARN", ""),
        "sqs_results_queue_url": os.environ.get("SQS_RESULTS_QUEUE_URL", ""),
        "secrets_prefix": os.environ.get("SECRETS_PREFIX", ""),
        "pending_requests_table": os.environ.get("PENDING_REQUESTS_TABLE", ""),
        "s3_bucket_name": os.environ.get("S3_BUCKET_NAME", ""),
        "dynamodb_tenants_table": os.environ.get("DYNAMODB_TENANTS_TABLE", ""),
        "subnet_ids": subnet_str.split(",") if subnet_str else [],
        "security_group_id": os.environ.get("LAMBDA_SECURITY_GROUP_ID", ""),
    }
