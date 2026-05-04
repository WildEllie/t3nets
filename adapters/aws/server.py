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
from starlette.types import ASGIApp, Receive, Scope, Send

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from adapters.aws.admin_api import AdminAPI
from adapters.aws.async_dispatch import AsyncSkillDispatcher
from adapters.aws.auth_api import AuthAPI
from adapters.aws.auth_middleware import AuthError, extract_auth
from adapters.aws.bedrock_provider import BedrockProvider
from adapters.aws.dynamo_rule_store import DynamoDBRuleStore
from adapters.aws.dynamo_training_store import DynamoDBTrainingStore
from adapters.aws.dynamodb_conversation_store import DynamoDBConversationStore
from adapters.aws.dynamodb_tenant_store import DynamoDBTenantStore
from adapters.aws.event_bridge_bus import EventBridgeBus
from adapters.aws.pending_requests import PendingRequestsStore
from adapters.aws.platform_api import PlatformAPI
from adapters.aws.result_router import AsyncResultRouter
from adapters.aws.secrets_manager import SecretsManagerProvider
from adapters.aws.sqs_poller import SQSResultPoller
from adapters.aws.ws_connections import WebSocketConnectionManager
from adapters.local.direct_bus import DirectBus
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
from agent.practices.registry import PracticeRegistry
from agent.router.compiled_engine import CompiledRuleEngine
from agent.router.rule_router import RuleBasedRouter
from agent.skills.registry import SkillRegistry
from agent.sse import SSEConnectionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("t3nets.aws")

# --- Global state ---
ai: MultiAIProvider
memory: DynamoDBConversationStore
tenants: DynamoDBTenantStore
secrets: SecretsManagerProvider
skills: SkillRegistry
bus: DirectBus
event_bus: EventBridgeBus | None = None
pending_store: PendingRequestsStore | None = None
sqs_poller: SQSResultPoller | None = None
result_router: AsyncResultRouter | None = None
practices: PracticeRegistry
blobs: Any  # S3BlobStore or None
rule_store: DynamoDBRuleStore
training_store: DynamoDBTrainingStore
admin_api: AdminAPI
auth_api: AuthAPI
async_dispatch: AsyncSkillDispatcher | None = None

# Shared handler instances (initialised in init())
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


# Fallback trigger-based router used when no compiled engine exists for a tenant
_fallback_router: RuleBasedRouter | None = None
platform_api: PlatformAPI
error_handler: ErrorHandler
started_at: float = 0.0
USE_ASYNC_SKILLS = os.environ.get("USE_ASYNC_SKILLS", "false").lower() == "true"

DEFAULT_TENANT = "default"


def _get_lambda_deploy_config() -> dict[str, Any]:
    """Build Lambda deployment config from environment variables."""
    project = os.environ.get("T3NETS_STAGE", "dev")
    name_prefix = f"t3nets-{project}"
    subnet_str = os.environ.get("LAMBDA_SUBNET_IDS", "")
    return {
        "region": AWS_REGION,
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


BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "")
OLLAMA_API_URL = os.environ.get("OLLAMA_API_URL", "")
PLATFORM = os.environ.get("T3NETS_PLATFORM", "aws")
STAGE = os.environ.get("T3NETS_STAGE", "dev")
WS_API_ENDPOINT = os.environ.get("WS_API_ENDPOINT", "")
WS_MANAGEMENT_ENDPOINT = os.environ.get(
    "WS_MANAGEMENT_ENDPOINT",
    WS_API_ENDPOINT.replace("wss://", "https://") if WS_API_ENDPOINT else "",
)
WS_CONNECTIONS_TABLE = os.environ.get("WS_CONNECTIONS_TABLE", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Build number — read from version.txt at startup
_version_path = Path(__file__).resolve().parent.parent.parent / "version.txt"
BUILD_NUMBER = _version_path.read_text().strip() if _version_path.exists() else "0"

# Cognito (set by Terraform → ECS env vars)
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_APP_CLIENT_ID = os.environ.get("COGNITO_APP_CLIENT_ID", "")
COGNITO_AUTH_DOMAIN = os.environ.get("COGNITO_AUTH_DOMAIN", "")

stats = {
    "rule_routed": 0,
    "ai_routed": 0,
    "conversational": 0,
    "raw": 0,
    "errors": 0,
    "total_tokens": 0,
}

# --- Push client: WebSocket (API Gateway) or SSE fallback ---
if WS_MANAGEMENT_ENDPOINT:
    push_client: SSEConnectionManager | WebSocketConnectionManager = WebSocketConnectionManager(
        management_endpoint=WS_MANAGEMENT_ENDPOINT,
        table_name=WS_CONNECTIONS_TABLE,
        region=AWS_REGION,
    )
    ws_manager: WebSocketConnectionManager | None = push_client  # type: ignore[assignment]
    sse_manager: SSEConnectionManager | None = None
    logger.info(f"Push transport: WebSocket (endpoint={WS_MANAGEMENT_ENDPOINT[:40]}...)")
else:
    push_client = SSEConnectionManager()
    ws_manager = None
    sse_manager = push_client
    logger.info("Push transport: SSE (no WS_MANAGEMENT_ENDPOINT configured)")


def _bedrock_geo_prefix() -> str:
    """Map AWS region to Bedrock geographic inference profile prefix."""
    region = os.environ.get("AWS_REGION", AWS_REGION)
    if region.startswith("us-") or region.startswith("ca-") or region.startswith("sa-"):
        return "us"
    elif region.startswith("eu-"):
        return "eu"
    elif region.startswith("ap-"):
        return "apac"
    return "us"


def _resolve_model(tenant: Any) -> tuple[str, str, str]:
    """Resolve tenant's ai_model to (provider_name, api_model_id, short_name).

    Picks the first active provider that supports the requested model.
    Falls back gracefully when the selected model isn't available.
    """
    model_id = tenant.settings.ai_model or DEFAULT_MODEL_ID
    model = get_model(model_id)
    active = ai.active_providers  # e.g. ["bedrock", "ollama"] or ["ollama"]

    # Find the first active provider that supports this model
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
        geo = _bedrock_geo_prefix()
        full_id = f"{geo}.{bedrock_id}"
        logger.info(f"Resolved model: {model_id} → {full_id}")
        return "bedrock", full_id, model.short_name
    return "bedrock", BEDROCK_MODEL_ID, model.short_name


async def _get_auth_info(request: Request) -> tuple[str, str]:
    """Extract (tenant_id, user_email) from JWT in Authorization header."""
    if not COGNITO_USER_POOL_ID:
        return DEFAULT_TENANT, ""
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
        return DEFAULT_TENANT, email
    except AuthError:
        return DEFAULT_TENANT, ""


def _file_response(filename: str, search_dir: str | None = None) -> Response:
    base = Path(__file__).parent.parent.parent
    path = base / search_dir / filename if search_dir else base / filename
    if path.exists():
        return FileResponse(str(path), media_type="text/html")
    return Response(status_code=404, content=f"{filename} not found")


def _extract_user_key(request: Request, body_token: str = "") -> str:
    """Extract user identity from JWT query param, Authorization header, or body token."""
    user_key = DEFAULT_TENANT
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


# ---------------------------------------------------------------------------
# Rule engine helpers
# ---------------------------------------------------------------------------


def _get_engine(tenant_id: str) -> CompiledRuleEngine | None:
    """Return the compiled rule engine for a tenant, or None if not yet built."""
    return _compiled_engines.get(tenant_id)


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
# WebSocket middleware
# ---------------------------------------------------------------------------


class WebSocketEventMiddleware:
    """Intercept API Gateway WebSocket events (POST with X-WS-Route header)."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") == "POST":
            headers = dict(scope["headers"])
            ws_route = headers.get(b"x-ws-route", b"").decode()
            if ws_route:
                request = Request(scope, receive)
                response = await _dispatch_ws_event(request, ws_route)
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


async def _dispatch_ws_event(request: Request, ws_route: str) -> Response:
    connection_id = request.headers.get("x-ws-connection-id", "")
    if ws_route == "$connect":
        return await _handle_ws_connect(request, connection_id)
    elif ws_route == "$disconnect":
        return await _handle_ws_disconnect(request, connection_id)
    else:
        return JSONResponse({"status": "ok"})


async def _handle_ws_connect(request: Request, connection_id: str) -> Response:
    if not connection_id or not ws_manager:
        return JSONResponse({"error": "WebSocket not configured"}, status_code=400)
    # WS Lambda proxy forwards the token in the POST body: {"token": "..."}
    body_token = ""
    try:
        body = await request.json()
        body_token = body.get("token", "")
    except Exception:
        pass
    user_key = _extract_user_key(request, body_token)
    ws_manager.register(user_key, connection_id)
    logger.info(f"WS $connect: {connection_id[:12]} user={user_key}")
    return JSONResponse({"status": "connected"})


async def _handle_ws_disconnect(request: Request, connection_id: str) -> Response:
    if not connection_id or not ws_manager:
        return JSONResponse({"status": "ok"})
    ws_manager.unregister_by_connection_id(connection_id)
    logger.info(f"WS $disconnect: {connection_id[:12]}")
    return JSONResponse({"status": "disconnected"})


# ---------------------------------------------------------------------------
# SSE bridge
# ---------------------------------------------------------------------------


class _QueueBridge:
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
# Static pages
# ---------------------------------------------------------------------------


async def homepage(request: Request) -> Response:
    return _file_response("chat.html", "adapters/local")


async def health_page(request: Request) -> Response:
    return _file_response("health.html", "adapters/local")


async def settings_page(request: Request) -> Response:
    return _file_response("settings.html", "adapters/local")


async def login_page(request: Request) -> Response:
    return _file_response("login.html", "adapters/local")


async def callback_page(request: Request) -> Response:
    return _file_response("callback.html", "adapters/local")


async def onboard_page(request: Request) -> Response:
    return _file_response("onboard.html", "adapters/local")


async def platform_page(request: Request) -> Response:
    return _file_response("platform.html", "adapters/local")


async def training_page(request: Request) -> Response:
    return _file_response("training.html", "adapters/local")


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------


async def sse_endpoint(request: Request) -> Response:
    if sse_manager is None:
        return JSONResponse(
            {"error": "SSE not available — WebSocket transport is active"}, status_code=400
        )
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
# Thin wrappers — delegate to shared handler instances
# ---------------------------------------------------------------------------


async def handle_health_api(request: Request) -> Response:
    return await health_handlers.handle_health_api(request)


async def handle_settings_get(request: Request) -> Response:
    tenant_id, _ = await _get_auth_info(request)
    return await settings_handlers.get_settings(request, tenant_id)


async def handle_settings_post(request: Request) -> Response:
    tenant_id, _ = await _get_auth_info(request)
    return await settings_handlers.post_settings(request, tenant_id)


async def handle_history(request: Request) -> Response:
    tenant_id, _ = await _get_auth_info(request)
    return await history_handlers.get_history(request, tenant_id, "dashboard-default")


async def handle_integrations_list(request: Request) -> Response:
    tenant_id, _ = await _get_auth_info(request)
    return await integration_handlers.list_integrations(request, tenant_id)


async def handle_integration_get(request: Request) -> Response:
    tenant_id, _ = await _get_auth_info(request)
    return await integration_handlers.get_integration(request, tenant_id)


async def handle_integrations_post(request: Request) -> Response:
    tenant_id, _ = await _get_auth_info(request)
    return await integration_handlers.post_integration(request, tenant_id)


async def handle_integrations_test(request: Request) -> Response:
    tenant_id, _ = await _get_auth_info(request)
    return await integration_handlers.test_integration(request, tenant_id)


async def handle_chat(request: Request) -> Response:
    return await chat_handlers.handle_chat(request)


async def handle_clear(request: Request) -> Response:
    return await chat_handlers.handle_clear(request)


async def handle_teams_webhook(request: Request) -> Response:
    return await webhook_handlers.handle_teams_webhook(request)


async def handle_telegram_webhook(request: Request) -> Response:
    return await webhook_handlers.handle_telegram_webhook(request)


async def handle_skill_invoke(request: Request) -> Response:
    tenant_id, _ = await _get_auth_info(request)
    return await practice_handlers.invoke_skill(request, tenant_id)


async def handle_practices_list(request: Request) -> Response:
    tenant_id, _ = await _get_auth_info(request)
    return await practice_handlers.list_practices(request, tenant_id)


async def handle_practices_upload(request: Request) -> Response:
    tenant_id, _ = await _get_auth_info(request)
    return await practice_handlers.upload_practice(request, tenant_id)


async def handle_practices_pages(request: Request) -> Response:
    tenant_id, _ = await _get_auth_info(request)
    return await practice_handlers.list_practice_pages(request, tenant_id)


async def handle_callback(request: Request) -> Response:
    tenant_id, _ = await _get_auth_info(request)
    return await practice_handlers.handle_callback(request, tenant_id)


async def handle_rules_admin(request: Request) -> Response:
    """POST /api/admin/rules/rebuild and GET /api/admin/rules/status."""
    method = request.method
    path = str(request.url.path)
    tenant_id, _ = await _get_auth_info(request)

    if method == "POST" and path.endswith("/rebuild"):
        _fire_and_forget(chat_handlers.rebuild_rules(tenant_id))
        data, status = await training_handlers.rebuild_rules(tenant_id)
        return JSONResponse(data, status_code=status)

    if method == "GET" and path.endswith("/status"):
        data, status = await training_handlers.rules_status(tenant_id)
        return JSONResponse(data, status_code=status)

    return JSONResponse({"error": "Not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Auth endpoints (AWS-specific — Cognito)
# ---------------------------------------------------------------------------


async def handle_auth_config(request: Request) -> Response:
    return await auth_api.config(request)


async def handle_auth_me(request: Request) -> Response:
    return await auth_api.me(request)


async def handle_auth_login(request: Request) -> Response:
    return await auth_api.login(request)


async def handle_auth_signup(request: Request) -> Response:
    return await auth_api.signup(request)


async def handle_auth_confirm(request: Request) -> Response:
    return await auth_api.confirm(request)


async def handle_auth_refresh(request: Request) -> Response:
    return await auth_api.refresh(request)


async def handle_auth_forgot_password(request: Request) -> Response:
    return await auth_api.forgot_password(request)


async def handle_auth_confirm_reset(request: Request) -> Response:
    return await auth_api.confirm_reset(request)


# ---------------------------------------------------------------------------
# Invitations (AWS-specific — DynamoDB tenant store)
# ---------------------------------------------------------------------------


async def handle_invitation_validate(request: Request) -> Response:
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
    try:
        auth = extract_auth(request.headers)
        body = await request.json()
        invite_code = body.get("invite_code", "")
        if not invite_code:
            return JSONResponse({"error": "invite_code is required"}, status_code=400)
        invitation = await tenants.get_invitation(invite_code)
        if not invitation or not invitation.is_valid():
            return JSONResponse({"error": "Invalid or expired invitation"}, status_code=404)
        if auth.email.lower() != invitation.email.lower():
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

        user = TenantUser(
            user_id=auth.user_id,
            tenant_id=invitation.tenant_id,
            email=invitation.email,
            display_name=invitation.email.split("@")[0],
            role=invitation.role,
            cognito_sub=auth.user_id,
        )
        await tenants.create_user(user)
        invitation.status = "accepted"
        invitation.accepted_at = datetime.now(timezone.utc).isoformat()
        await tenants.update_invitation(invitation)
        return JSONResponse(
            {
                "accepted": True,
                "tenant_id": invitation.tenant_id,
                "user_id": auth.user_id,
                "role": invitation.role,
            }
        )
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status)
    except Exception as e:
        logger.exception("Invitation accept error")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Admin and Platform API delegation (run in thread pool)
# ---------------------------------------------------------------------------


async def handle_admin(request: Request) -> Response:
    """Delegate all /api/admin/* routes to AdminAPI."""
    method = request.method
    path = str(request.url.path)
    body = None
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        try:
            body = await request.json()
        except Exception:
            body = None
    headers = dict(request.headers)
    # Resolve tenant_id server-side (avoids a second DynamoDB lookup inside AdminAPI)
    tenant_id, _ = await _get_auth_info(request)
    headers["x-tenant-id"] = tenant_id
    data, status = await admin_api.handle_request(method, path, headers, body)
    return JSONResponse(data, status_code=status)


async def handle_platform(request: Request) -> Response:
    """Delegate all /api/platform/* routes to PlatformAPI (run in thread pool)."""
    method = request.method
    path = str(request.url.path)
    body = None
    if method in ("POST", "PUT", "PATCH"):
        body = await request.json()
    headers = dict(request.headers)
    data, status = await asyncio.to_thread(platform_api.handle_request, method, path, headers, body)
    return JSONResponse(data, status_code=status)


# ---------------------------------------------------------------------------
# AWS-specific: channel adapter resolvers
# ---------------------------------------------------------------------------


async def _get_teams_adapter(bot_app_id: str) -> TeamsAdapter | None:
    try:
        tenant = await tenants.get_by_channel_id("teams", bot_app_id)
    except Exception:
        try:
            all_tenants = await tenants.list_tenants()
            tenant = None
            for t in all_tenants:
                try:
                    creds = await secrets.get(t.tenant_id, "teams")
                    if creds.get("app_id") == bot_app_id:
                        tenant = t
                        await tenants.set_channel_mapping(t.tenant_id, "teams", bot_app_id)
                        break
                except Exception:
                    continue
            if tenant is None:
                return None
        except Exception:
            return None

    if tenant is None:
        return None

    try:
        creds = await secrets.get(tenant.tenant_id, "teams")
        app_id = creds.get("app_id", "")
        app_secret = creds.get("app_secret", "")
        if not app_id or not app_secret:
            logger.error(f"Incomplete Teams credentials for tenant {tenant.tenant_id}")
            return None
        return TeamsAdapter(app_id, app_secret)
    except Exception as e:
        logger.error(f"Failed to load Teams credentials: {e}")
        return None


async def _get_telegram_adapter(token_hash: str) -> TelegramAdapter | None:
    if not token_hash or token_hash == "webhook":
        logger.warning("No token hash in Telegram webhook URL")
        return None
    try:
        tenant = await tenants.get_by_channel_id("telegram", token_hash)
        creds = await secrets.get(tenant.tenant_id, "telegram")
        bot_token = creds.get("bot_token", "")
        if bot_token:
            return TelegramAdapter(bot_token, creds.get("webhook_secret", ""))
    except Exception as e:
        logger.warning(f"Telegram channel mapping lookup failed: {e}")
    return None


async def _get_whatsapp_adapter(token_hash: str) -> Any:
    from agent.channels.whatsapp import WhatsAppAdapter

    if not token_hash or token_hash == "webhook":
        logger.warning("No token hash in WhatsApp webhook URL")
        return None
    try:
        tenant = await tenants.get_by_channel_id("whatsapp", token_hash)
        creds = await secrets.get(tenant.tenant_id, "whatsapp")
        api_token = creds.get("api_token", "")
        if api_token:
            return WhatsAppAdapter(api_token, creds.get("webhook_secret", ""))
    except Exception as e:
        logger.warning(f"WhatsApp channel mapping lookup failed: {e}")
    return None


# ---------------------------------------------------------------------------
# WhatsApp webhook (delegates to shared WebhookHandlers)
# ---------------------------------------------------------------------------


async def handle_whatsapp_webhook(request: Request) -> Response:
    return await webhook_handlers.handle_whatsapp_webhook(request)


# ---------------------------------------------------------------------------
# Practice page (static file serving)
# ---------------------------------------------------------------------------


async def practice_page(request: Request) -> Response:
    """Serve a practice page at /p/{practice}/{page}."""
    practice_name = request.path_params["practice"]
    page_slug = request.path_params["page"]
    page_path = practices.get_page_path(practice_name, page_slug)
    if page_path and page_path.exists():
        return FileResponse(str(page_path), media_type="text/html")
    return Response(status_code=404, content="Practice page not found")


# ---------------------------------------------------------------------------
# Integration webhook registration helpers (used by _on_credentials_saved)
# ---------------------------------------------------------------------------


def _register_telegram_webhook(request_headers: dict[str, str], creds: dict[str, Any]) -> None:
    bot_token = creds.get("bot_token", "")
    if not bot_token:
        return
    try:
        token_hash = hashlib.sha256(bot_token.encode()).hexdigest()[:16]
        host = request_headers.get("host", "")
        scheme = "https"
        if host:
            base_url = f"{scheme}://{host}"
        else:
            base_url = os.environ.get("API_BASE_URL", "")
        if not base_url:
            logger.warning("Cannot register Telegram webhook: no Host header or API_BASE_URL")
            return
        webhook_url = f"{base_url}/api/channels/telegram/webhook/{token_hash}"
        webhook_secret = creds.get("webhook_secret", "")
        adapter = TelegramAdapter(bot_token, webhook_secret)
        result = adapter.register_webhook(webhook_url)
        logger.info(f"Telegram webhook registration: {result}")
    except Exception as e:
        logger.error(f"Failed to register Telegram webhook: {e}")


def _register_whatsapp_webhook(request_headers: dict[str, str], creds: dict[str, Any]) -> None:
    from agent.channels.whatsapp import WhatsAppAdapter

    api_token = creds.get("api_token", "")
    if not api_token:
        return
    try:
        token_hash = hashlib.sha256(api_token.encode()).hexdigest()[:16]
        host = request_headers.get("host", "")
        scheme = "https"
        if host:
            base_url = f"{scheme}://{host}"
        else:
            base_url = os.environ.get("API_BASE_URL", "")
        if not base_url:
            logger.warning("Cannot register WhatsApp webhook: no Host header or API_BASE_URL")
            return
        webhook_url = f"{base_url}/api/channels/whatsapp/webhook/{token_hash}"
        webhook_secret = creds.get("webhook_secret", "")
        if not webhook_secret:
            # Auto-generate a secret
            import secrets as _secrets

            webhook_secret = _secrets.token_urlsafe(24)
            creds["webhook_secret"] = webhook_secret
        adapter = WhatsAppAdapter(api_token, webhook_secret)
        result = adapter.register_webhook(webhook_url)
        logger.info(f"WhatsApp webhook registration: {result}")
    except Exception as e:
        logger.error(f"Failed to register WhatsApp webhook: {e}")


# ---------------------------------------------------------------------------
# Starlette app
# ---------------------------------------------------------------------------

routes = [
    # Static pages
    Route("/", homepage),
    Route("/chat", homepage),
    Route("/health", health_page),
    Route("/settings", settings_page),
    Route("/login", login_page),
    Route("/callback", callback_page),
    Route("/onboard", onboard_page),
    Route("/platform", platform_page),
    Route("/training", training_page),
    # API
    Route("/api/events", sse_endpoint),
    Route("/api/health", handle_health_api),
    Route("/api/settings", handle_settings_get, methods=["GET"]),
    Route("/api/settings", handle_settings_post, methods=["POST"]),
    Route("/api/history", handle_history),
    Route("/api/auth/config", handle_auth_config),
    Route("/api/auth/me", handle_auth_me),
    Route("/api/auth/login", handle_auth_login, methods=["POST"]),
    Route("/api/auth/signup", handle_auth_signup, methods=["POST"]),
    Route("/api/auth/confirm", handle_auth_confirm, methods=["POST"]),
    Route("/api/auth/refresh", handle_auth_refresh, methods=["POST"]),
    Route("/api/auth/forgot-password", handle_auth_forgot_password, methods=["POST"]),
    Route("/api/auth/confirm-reset", handle_auth_confirm_reset, methods=["POST"]),
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
    Route(
        "/api/channels/whatsapp/webhook/{token_hash}",
        handle_whatsapp_webhook,
        methods=["POST"],
    ),
    # Practices
    Route("/api/skill/{name}", handle_skill_invoke, methods=["POST"]),
    Route("/api/practices", handle_practices_list),
    Route("/api/practices/pages", handle_practices_pages),
    Route("/api/practices/upload", handle_practices_upload, methods=["POST"]),
    Route("/api/callback/{request_id}", handle_callback, methods=["POST"]),
    Route("/p/{practice}/{page}", practice_page),
    # Admin and Platform (delegated to API objects via thread pool)
    Route("/api/admin/rules/{rest:path}", handle_rules_admin, methods=["GET", "POST"]),
    Route(
        "/api/admin/{rest:path}", handle_admin, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
    ),
    Route(
        "/api/platform/{rest:path}",
        handle_platform,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    ),
]

middleware = [
    Middleware(WebSocketEventMiddleware),
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    ),
]

app = Starlette(routes=routes, middleware=middleware)


# ---------------------------------------------------------------------------
# Initialisation & entry point
# ---------------------------------------------------------------------------


async def _init_aws_adapters(region: str) -> None:
    """AI providers, conversation/tenant/secrets stores, skills, blobs."""
    global ai, memory, tenants, secrets, skills, blobs

    conversations_table = os.getenv("DYNAMODB_CONVERSATIONS_TABLE")
    tenants_table = os.getenv("DYNAMODB_TENANTS_TABLE")
    secrets_prefix = os.getenv("SECRETS_PREFIX")
    if not all([conversations_table, tenants_table, secrets_prefix]):
        logger.error(
            "Missing required env vars: DYNAMODB_CONVERSATIONS_TABLE, "
            "DYNAMODB_TENANTS_TABLE, SECRETS_PREFIX"
        )
        sys.exit(1)
    assert conversations_table and tenants_table and secrets_prefix  # narrowed above

    _providers: dict[str, BedrockProvider | OllamaProvider] = {}
    if BEDROCK_MODEL_ID:
        logger.info(f"Using Bedrock provider (model={BEDROCK_MODEL_ID})")
        _providers["bedrock"] = BedrockProvider(region=region, model_id=BEDROCK_MODEL_ID)
    if OLLAMA_API_URL:
        logger.info(f"Using Ollama provider at {OLLAMA_API_URL}")
        _providers["ollama"] = OllamaProvider(base_url=OLLAMA_API_URL)
    if not _providers:
        logger.error("No AI provider configured. Set BEDROCK_MODEL_ID and/or OLLAMA_API_URL.")
        sys.exit(1)
    ai = MultiAIProvider(_providers)
    memory = DynamoDBConversationStore(conversations_table, region=region)
    tenants = DynamoDBTenantStore(tenants_table, region=region)
    secrets = SecretsManagerProvider(secrets_prefix, region=region)

    skills_obj = SkillRegistry()
    skills_dir = Path(__file__).parent.parent.parent / "agent" / "skills"
    skills_obj.load_from_directory(skills_dir)
    skills = skills_obj

    try:
        from adapters.aws.s3_blob_store import S3BlobStore

        s3_bucket = os.getenv("S3_BUCKET_NAME", "t3nets-dev-static")
        blobs = S3BlobStore(bucket_name=s3_bucket, region=region)
        logger.info(f"S3 BlobStore: {s3_bucket}")
    except Exception as e:
        logger.warning(f"S3BlobStore init failed ({e}), blobs disabled")
        blobs = None


async def _init_aws_practices() -> None:
    """Practice registry, S3 restore, Lambda ensure."""
    global practices, _fallback_router

    practices_obj = PracticeRegistry()
    practices_dir = Path(__file__).parent.parent.parent / "agent" / "practices"
    practices_obj.load_builtin(practices_dir)

    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    if blobs:
        try:
            default_tenant = await tenants.get_tenant(DEFAULT_TENANT)
            installed_versions = default_tenant.settings.installed_practices
            restored = await practices_obj.restore_from_blob_store(
                blobs,
                DEFAULT_TENANT,
                data_dir,
                installed_versions=installed_versions,
            )
            if restored:
                logger.info(f"Restored {restored} practice(s) from S3")
        except Exception as e:
            logger.warning(f"Practice restore from S3 failed: {e}")

    practices_obj.load_uploaded(data_dir)
    practices_obj.register_skills(skills)
    practices = practices_obj
    logger.info(f"Loaded practices: {[p.name for p in practices.list_all()]}")
    logger.info(f"Loaded skills: {skills.list_skill_names()}")

    lambda_config = _get_lambda_deploy_config()
    if lambda_config["lambda_role_arn"]:
        try:
            fixed = await practices_obj.ensure_skill_lambdas(lambda_config)
            if fixed:
                logger.info(f"Deployed {fixed} missing practice skill Lambda(s)")
        except Exception as e:
            logger.warning(f"Lambda ensure check failed: {e}")

    _fallback_router = RuleBasedRouter(skills)


def _init_aws_dispatch(region: str) -> None:
    """rule_store, training_store, DirectBus, AdminAPI, PlatformAPI, AuthAPI, errors."""
    global rule_store, training_store, bus, admin_api, platform_api, auth_api, error_handler

    tenants_table = os.environ["DYNAMODB_TENANTS_TABLE"]
    rule_store = DynamoDBRuleStore(tenants_table, region=region)
    training_store = DynamoDBTrainingStore(tenants_table, region=region)
    bus = DirectBus(skills, secrets)
    admin_api = AdminAPI(tenants, secrets, skills, training_store)
    platform_api = PlatformAPI(tenants, secrets, skills)
    auth_api = AuthAPI(tenants)
    error_handler = ErrorHandler()


def _init_aws_async_dispatch(region: str) -> None:
    """EventBridge bus, pending requests store, async dispatcher, result router, SQS poller."""
    global event_bus, pending_store, async_dispatch, result_router, sqs_poller

    if not USE_ASYNC_SKILLS:
        logger.info("Async skills DISABLED (USE_ASYNC_SKILLS=false), using DirectBus")
        return

    eb_bus_name = os.environ.get("EVENTBRIDGE_BUS_NAME", "")
    sqs_queue_url = os.environ.get("SQS_RESULTS_QUEUE_URL", "")
    pending_table = os.environ.get("PENDING_REQUESTS_TABLE", "")
    if not all([eb_bus_name, sqs_queue_url, pending_table]):
        logger.error(
            "USE_ASYNC_SKILLS=true but missing required env vars: "
            "EVENTBRIDGE_BUS_NAME, SQS_RESULTS_QUEUE_URL, PENDING_REQUESTS_TABLE"
        )
        logger.warning("Falling back to synchronous DirectBus")
        return

    event_bus = EventBridgeBus(eb_bus_name, region=region)
    pending_store = PendingRequestsStore(pending_table, region=region)
    async_dispatch = AsyncSkillDispatcher(
        event_bus=event_bus,
        pending_store=pending_store,
        stats=stats,
        fire_and_forget=_fire_and_forget,
        log_training=lambda *a, **kw: chat_handlers.log_training(*a, **kw),
    )
    result_router = AsyncResultRouter(
        push_client=push_client,
        pending_store=pending_store,
        ai_provider=ai,
        conversation_store=memory,
        bedrock_model_id=BEDROCK_MODEL_ID,
        secrets_provider=secrets,
    )
    sqs_poller = SQSResultPoller(
        queue_url=sqs_queue_url,
        callback=result_router.handle_result,
        region=region,
    )
    sqs_poller.start()
    logger.info(
        f"Async skills ENABLED: EventBridge={eb_bus_name}, "
        f"SQS={sqs_queue_url[-30:]}, Pending={pending_table}"
    )


async def _init_aws_state() -> None:
    """Default tenant seeding, integrations log, compiled rule engines."""
    channels = ChannelRegistry()
    channels.register(DashboardAdapter())

    try:
        await tenants.get_tenant(DEFAULT_TENANT)
        logger.info(f"Tenant '{DEFAULT_TENANT}' exists")
    except Exception:
        from agent.models.tenant import Tenant, TenantSettings

        now = datetime.now(timezone.utc).isoformat()
        tenant = Tenant(
            tenant_id=DEFAULT_TENANT,
            name="T3nets Default",
            status="active",
            created_at=now,
            settings=TenantSettings(enabled_skills=skills.list_skill_names()),
        )
        await tenants.create_tenant(tenant)
        logger.info(f"Seeded tenant '{DEFAULT_TENANT}'")

    connected = await secrets.list_integrations(DEFAULT_TENANT)
    logger.info(f"Connected integrations: {connected}")

    try:
        all_tenants = await tenants.list_tenants()
        for t in all_tenants:
            cached = await rule_store.load_rule_set(t.tenant_id)
            if cached:
                _compiled_engines[t.tenant_id] = CompiledRuleEngine(cached, skills)
                logger.info(
                    f"Loaded rule engine for '{t.tenant_id}' "
                    f"(v{cached.version}, generated {cached.generated_at[:10]})"
                )
            else:
                logger.info(
                    f"No rule set found for tenant '{t.tenant_id}' — "
                    "AI routing will be used until rules are built via /api/admin/rules/rebuild"
                )
    except Exception:
        logger.exception("Failed to load rule engines at startup — AI routing will be used")


async def _chat_skill_invoker(
    tenant_id: str,
    skill_name: str,
    params: dict[str, Any],
    conversation_id: str,
    request_id: str,
    reply_channel: str,
    reply_target: str,
    is_raw: bool = False,
    user_message: str = "",
    model_id: str = "",
    model_short_name: str = "",
) -> dict[str, Any] | Response | None:
    """skill_invoker for ChatHandlers — async dispatch when enabled, else DirectBus."""
    if USE_ASYNC_SKILLS and async_dispatch is not None:
        user_email = reply_target  # reply_target is the user_email for dashboard
        route_type = "rule" if request_id.startswith("rule-") else "ai"
        return await async_dispatch.dispatch_chat(
            tenant_id,
            user_email,
            skill_name,
            params,
            conversation_id,
            user_message,
            is_raw,
            route_type,
            model_id,
            model_short_name,
        )
    await bus.publish_skill_invocation(
        tenant_id,
        skill_name,
        params,
        conversation_id,
        request_id,
        reply_channel,
        reply_target,
        is_raw=is_raw,
    )
    return bus.get_result(request_id)


async def _on_credentials_saved(
    tenant_id: str,
    integration_name: str,
    merged: dict[str, Any],
) -> None:
    """Webhook registration + channel mapping after credentials save."""
    if integration_name == "telegram":
        bot_token = merged.get("bot_token", "")
        if bot_token:
            _register_telegram_webhook({}, merged)
            t_hash = hashlib.sha256(bot_token.encode()).hexdigest()[:16]
            await tenants.set_channel_mapping(tenant_id, "telegram", t_hash)

    elif integration_name == "whatsapp":
        api_token = merged.get("api_token", "")
        if api_token:
            _register_whatsapp_webhook({}, merged)
            wa_hash = hashlib.sha256(api_token.encode()).hexdigest()[:16]
            await tenants.set_channel_mapping(tenant_id, "whatsapp", wa_hash)


async def _post_install_hook(practice_obj: Any, tenant_id: str) -> None:
    """After a practice is installed: deploy Lambdas, publish pages, rebuild rules."""
    lc = _get_lambda_deploy_config()
    if lc["lambda_role_arn"]:
        deployed = await practices.deploy_skill_lambdas(practice_obj, lc)
        logger.info(f"Background: deployed Lambdas for {deployed}")
    # Publish pages to S3 + invalidate CloudFront so /p/{name}/* serves the
    # newly-uploaded version immediately. CDN+S3 paradigm — see
    # docs/aws-infrastructure.md "Static vs dynamic content split".
    from adapters.aws.practice_publish import publish_practice_pages

    try:
        uploaded = publish_practice_pages(
            practice_obj,
            s3_bucket=os.environ.get("S3_BUCKET_NAME", ""),
            cloudfront_distribution_id=os.environ.get("CLOUDFRONT_DISTRIBUTION_ID", ""),
            region=AWS_REGION,
        )
        logger.info(f"Background: published {uploaded} practice page(s) to S3")
    except Exception as e:
        logger.error(f"Background: practice page publish failed: {e}")
    await chat_handlers.rebuild_rules(tenant_id)
    logger.info(f"Background: rules rebuilt for tenant {tenant_id}")


def _init_aws_handlers() -> None:
    """Construct all shared handler classes."""
    global settings_handlers, integration_handlers, chat_handlers, history_handlers
    global training_handlers, health_handlers, practice_handlers, webhook_handlers

    settings_handlers = SettingsHandlers(
        tenant_store=tenants,
        secrets_provider=secrets,
        skill_registry=skills,
        practice_registry=practices,
        active_providers=lambda: ai.active_providers,
        platform=PLATFORM,
        stage=STAGE,
        build_number=BUILD_NUMBER,
        rebuild_callback=lambda tid: _fire_and_forget(chat_handlers.rebuild_rules(tid)),
    )

    integration_handlers = IntegrationHandlers(
        secrets=secrets,
        on_credentials_saved=_on_credentials_saved,
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
        resolve_auth=_get_auth_info,
        resolve_model=_resolve_model,
        fire_and_forget=_fire_and_forget,
        skill_invoker=_chat_skill_invoker,
        enrich_match=_enrich_match_params,
        fallback_router=_fallback_router,
    )

    history_handlers = HistoryHandlers(conversation_store=memory)

    training_handlers = TrainingHandlers(
        training_store=training_store,
        rule_store=rule_store,
        compiled_engines=_compiled_engines,
        rebuild_rules_fn=chat_handlers.rebuild_rules,
    )

    health_handlers = HealthHandlers(
        tenants=tenants,
        secrets=secrets,
        skill_registry=skills,
        started_at=started_at,
        connection_count=lambda: push_client.connection_count,
        get_stats=lambda: stats,
        get_ai_info=lambda: {
            "providers": ai.active_providers,
            "model": _resolve_model(
                type("T", (), {"settings": type("S", (), {"ai_model": DEFAULT_MODEL_ID})()})()
            )[1],
            "api_key_preview": "IAM role (no key)",
            "total_tokens": stats["total_tokens"],
        },
        platform=PLATFORM,
        stage=STAGE,
        default_tenant=DEFAULT_TENANT,
        connection_label="push_connections",
    )

    practice_handlers = PracticeHandlers(
        practices=practices,
        skills=skills,
        blobs=blobs,
        tenants=tenants,
        secrets=secrets,
        pending_store=pending_store,
        post_install_hook=_post_install_hook,
    )

    webhook_handlers = WebhookHandlers(
        ai=ai,
        memory=memory,
        bus=bus,
        skills=skills,
        stats=stats,
        compiled_engines=_compiled_engines,
        fallback_router=_fallback_router,
        resolve_model=_resolve_model,
        resolve_teams_adapter=_get_teams_adapter,
        resolve_telegram_adapter=_get_telegram_adapter,
        resolve_whatsapp_adapter=_get_whatsapp_adapter,
        resolve_tenant_by_channel=lambda ch, key: tenants.get_by_channel_id(ch, key),
        log_training=chat_handlers.log_training,
        enrich_match_params=_enrich_match_params,
        async_skill_handler=async_dispatch.dispatch_channel if async_dispatch else None,
        use_async_skills=USE_ASYNC_SKILLS,
        event_bus=event_bus,
        pending_store=pending_store,
    )


async def init() -> None:
    global ai, memory, tenants, secrets, skills, bus, event_bus, pending_store
    global sqs_poller, result_router, rule_store, training_store, admin_api, platform_api
    global auth_api, async_dispatch, error_handler, started_at, _fallback_router, practices, blobs
    global settings_handlers, integration_handlers, chat_handlers, history_handlers
    global training_handlers, health_handlers, practice_handlers, webhook_handlers

    started_at = time.time()
    region = AWS_REGION

    await _init_aws_adapters(region)
    await _init_aws_practices()
    _init_aws_dispatch(region)
    # _init_aws_async_dispatch references chat_handlers; that closure is evaluated
    # at call time, not now, so the order with _init_aws_handlers below is fine.
    _init_aws_async_dispatch(region)
    await _init_aws_state()
    _init_aws_handlers()


def main() -> None:
    asyncio.run(init())

    port = int(os.getenv("PORT", "8080"))
    async_status = "ON (EventBridge→Lambda→SQS)" if event_bus else "OFF (DirectBus)"
    push_transport = "WebSocket" if ws_manager else "SSE"
    logger.info("")
    logger.info("  ╔══════════════════════════════════════════════╗")
    logger.info("  ║  T3nets AWS Server                           ║")
    logger.info(f"  ║  http://0.0.0.0:{port}                       ║")
    logger.info(f"  ║  Model: {BEDROCK_MODEL_ID[:30]}      ║")
    logger.info(f"  ║  Push:    {push_transport:<35}║")
    logger.info(f"  ║  Async:   {async_status:<35}║")
    logger.info("  ╚══════════════════════════════════════════════╝")
    logger.info("")

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
