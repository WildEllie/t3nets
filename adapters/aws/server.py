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

import inspect

from adapters.aws.admin_api import AdminAPI
from adapters.aws.auth_middleware import AuthError, extract_auth
from adapters.aws.bedrock_provider import BedrockProvider
from adapters.aws.dynamo_rule_store import DynamoDBRuleStore
from adapters.aws.dynamo_training_store import DynamoDBTrainingStore
from adapters.aws.dynamodb_conversation_store import DynamoDBConversationStore
from adapters.aws.dynamodb_tenant_store import DynamoDBTenantStore
from adapters.aws.event_bridge_bus import EventBridgeBus
from adapters.aws.pending_requests import PendingRequest, PendingRequestsStore
from adapters.aws.platform_api import PlatformAPI
from adapters.aws.result_router import AsyncResultRouter
from adapters.aws.secrets_manager import SecretsManagerProvider
from adapters.aws.sqs_poller import SQSResultPoller
from adapters.aws.ws_connections import WebSocketConnectionManager
from adapters.local.direct_bus import DirectBus
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
from agent.router.compiled_engine import CompiledRuleEngine, is_conversational, strip_raw_flag
from agent.router.models import TrainingExample
from agent.router.rule_engine_builder import RuleEngineBuilder
from agent.router.rule_router import RuleBasedRouter
from agent.practices.registry import PracticeRegistry
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
# Health
# ---------------------------------------------------------------------------


async def handle_health_api(request: Request) -> Response:
    try:
        uptime_secs = time.time() - started_at
        tenant = await tenants.get_tenant(DEFAULT_TENANT)
        connected = await secrets.list_integrations(DEFAULT_TENANT)
        health = {
            "status": "ok",
            "platform": PLATFORM,
            "stage": STAGE,
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
                "api_key_preview": "IAM role (no key)",
                "total_tokens": stats["total_tokens"],
            },
            "routing": stats,
            "integrations": {
                name: {"connected": name in connected}
                for name in ["jira", "github", "teams", "telegram", "twilio"]
            },
            "skills": [
                {
                    "name": s.name,
                    "description": s.description.strip()[:120],
                    "requires_integration": s.requires_integration,
                    "supports_raw": s.supports_raw,
                    "triggers": s.triggers[:8],
                }
                for s in skills.list_skills()
            ],
            "push_connections": push_client.connection_count,
        }
        return JSONResponse(health)
    except Exception as e:
        logger.exception("Health check error")
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


async def handle_auth_config(request: Request) -> Response:
    response: dict[str, Any] = {
        "enabled": bool(COGNITO_USER_POOL_ID),
        "client_id": COGNITO_APP_CLIENT_ID,
        "auth_domain": COGNITO_AUTH_DOMAIN,
        "user_pool_id": COGNITO_USER_POOL_ID,
    }
    if WS_API_ENDPOINT:
        response["ws_endpoint"] = WS_API_ENDPOINT
    return JSONResponse(response)


async def handle_auth_me(request: Request) -> Response:
    if not COGNITO_USER_POOL_ID:
        return JSONResponse(
            {
                "authenticated": False,
                "tenant_id": DEFAULT_TENANT,
                "tenant_status": "active",
            }
        )
    try:
        auth = extract_auth(request.headers)
        email = auth.email
        tenant_id = ""
        display_name = ""
        avatar_url = ""
        role = "member"
        tenant_status = "onboarding"

        try:
            user = await tenants.get_user_by_cognito_sub(auth.user_id)
            if user:
                tenant_id = user.tenant_id
                email = email or user.email
                display_name = user.display_name
                avatar_url = user.avatar_url
                role = user.role
                logger.info(
                    f"auth/me: resolved tenant '{tenant_id}' from DynamoDB "
                    f"for sub {auth.user_id[:8]}..."
                )
        except Exception as e:
            logger.warning(f"auth/me DynamoDB lookup failed: {e}")

        tenant_name = ""
        if tenant_id:
            try:
                tenant = await tenants.get_tenant(tenant_id)
                tenant_status = tenant.status
                tenant_name = tenant.name
            except Exception:
                tenant_status = "active"

        return JSONResponse(
            {
                "authenticated": True,
                "user_id": auth.user_id,
                "tenant_id": tenant_id,
                "email": email,
                "role": role,
                "display_name": display_name,
                "avatar_url": avatar_url,
                "tenant_status": tenant_status,
                "tenant_name": tenant_name,
            }
        )
    except AuthError as e:
        return JSONResponse({"error": e.message}, status_code=e.status)


async def handle_auth_login(request: Request) -> Response:
    try:
        body = await request.json()
        email = body.get("email", "").strip()
        password = body.get("password", "")
        if not email or not password:
            return JSONResponse({"error": "Email and password are required"}, status_code=400)
        if not COGNITO_USER_POOL_ID or not COGNITO_APP_CLIENT_ID:
            return JSONResponse({"error": "Auth not configured"}, status_code=500)

        import boto3  # type: ignore[import-untyped]

        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        result = client.initiate_auth(
            ClientId=COGNITO_APP_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )
        challenge = result.get("ChallengeName", "")
        if challenge:
            logger.warning(f"Auth login challenge: {challenge} for {email}")
            return JSONResponse(
                {"error": f"Account requires action: {challenge}", "code": challenge},
                status_code=403,
            )
        auth_result = result.get("AuthenticationResult", {})
        return JSONResponse(
            {
                "id_token": auth_result.get("IdToken", ""),
                "access_token": auth_result.get("AccessToken", ""),
                "refresh_token": auth_result.get("RefreshToken", ""),
            }
        )
    except Exception as e:
        err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if err_code == "NotAuthorizedException":
            return JSONResponse({"error": "Invalid email or password"}, status_code=401)
        elif err_code == "UserNotConfirmedException":
            return JSONResponse(
                {"error": "Email not verified", "code": "USER_NOT_CONFIRMED"}, status_code=403
            )
        elif err_code == "UserNotFoundException":
            return JSONResponse({"error": "Invalid email or password"}, status_code=401)
        elif err_code == "PasswordResetRequiredException":
            return JSONResponse(
                {"error": "Password reset required", "code": "PASSWORD_RESET_REQUIRED"},
                status_code=403,
            )
        else:
            logger.exception("Auth login error")
            return JSONResponse({"error": str(e)}, status_code=500)


async def handle_auth_signup(request: Request) -> Response:
    try:
        body = await request.json()
        email = body.get("email", "").strip()
        password = body.get("password", "")
        name = body.get("name", "").strip()
        if not email or not password:
            return JSONResponse({"error": "Email and password are required"}, status_code=400)
        if not COGNITO_USER_POOL_ID or not COGNITO_APP_CLIENT_ID:
            return JSONResponse({"error": "Auth not configured"}, status_code=500)

        import boto3

        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        user_attrs = [
            {"Name": "email", "Value": email},
            {"Name": "name", "Value": name or email.split("@")[0]},
        ]
        result = client.sign_up(
            ClientId=COGNITO_APP_CLIENT_ID,
            Username=email,
            Password=password,
            UserAttributes=user_attrs,
        )
        return JSONResponse(
            {
                "user_sub": result.get("UserSub", ""),
                "confirmed": result.get("UserConfirmed", False),
            },
            status_code=201,
        )
    except Exception as e:
        err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if err_code == "UsernameExistsException":
            return JSONResponse(
                {"error": "An account with this email already exists"}, status_code=409
            )
        elif err_code == "InvalidPasswordException":
            msg = getattr(e, "response", {}).get("Error", {}).get("Message", str(e))
            return JSONResponse({"error": msg}, status_code=400)
        else:
            logger.exception("Auth signup error")
            return JSONResponse({"error": str(e)}, status_code=500)


async def handle_auth_confirm(request: Request) -> Response:
    try:
        body = await request.json()
        email = body.get("email", "").strip()
        code = body.get("code", "").strip()
        if not email or not code:
            return JSONResponse({"error": "Email and code are required"}, status_code=400)
        if not COGNITO_APP_CLIENT_ID:
            return JSONResponse({"error": "Auth not configured"}, status_code=500)

        import boto3

        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        client.confirm_sign_up(
            ClientId=COGNITO_APP_CLIENT_ID, Username=email, ConfirmationCode=code
        )
        return JSONResponse({"ok": True})
    except Exception as e:
        err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if err_code == "CodeMismatchException":
            return JSONResponse({"error": "Invalid verification code"}, status_code=400)
        elif err_code == "ExpiredCodeException":
            return JSONResponse({"error": "Verification code has expired"}, status_code=400)
        else:
            logger.exception("Auth confirm error")
            return JSONResponse({"error": str(e)}, status_code=500)


async def handle_auth_refresh(request: Request) -> Response:
    try:
        body = await request.json()
        refresh_token = body.get("refresh_token", "")
        if not refresh_token:
            return JSONResponse({"error": "refresh_token is required"}, status_code=400)
        if not COGNITO_APP_CLIENT_ID:
            return JSONResponse({"error": "Auth not configured"}, status_code=500)

        import boto3

        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        result = client.initiate_auth(
            ClientId=COGNITO_APP_CLIENT_ID,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": refresh_token},
        )
        auth_result = result.get("AuthenticationResult", {})
        return JSONResponse(
            {
                "id_token": auth_result.get("IdToken", ""),
                "access_token": auth_result.get("AccessToken", ""),
            }
        )
    except Exception as e:
        logger.exception("Auth refresh error")
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_auth_forgot_password(request: Request) -> Response:
    try:
        body = await request.json()
        email = body.get("email", "").strip()
        if not email:
            return JSONResponse({"error": "Email is required"}, status_code=400)
        if not COGNITO_APP_CLIENT_ID:
            return JSONResponse({"error": "Auth not configured"}, status_code=500)

        import boto3

        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        client.forgot_password(ClientId=COGNITO_APP_CLIENT_ID, Username=email)
        return JSONResponse({"message": "Reset code sent"})
    except Exception as e:
        err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if err_code in ("UserNotFoundException", "InvalidParameterException"):
            return JSONResponse({"message": "Reset code sent"})
        elif err_code == "LimitExceededException":
            return JSONResponse(
                {"error": "Too many attempts. Please try again later."}, status_code=429
            )
        else:
            logger.exception("Auth forgot-password error")
            return JSONResponse({"error": str(e)}, status_code=500)


async def handle_auth_confirm_reset(request: Request) -> Response:
    try:
        body = await request.json()
        email = body.get("email", "").strip()
        code = body.get("code", "").strip()
        new_password = body.get("new_password", "")
        if not email or not code or not new_password:
            return JSONResponse(
                {"error": "Email, code, and new password are required"}, status_code=400
            )
        if not COGNITO_APP_CLIENT_ID:
            return JSONResponse({"error": "Auth not configured"}, status_code=500)

        import boto3

        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        client.confirm_forgot_password(
            ClientId=COGNITO_APP_CLIENT_ID,
            Username=email,
            ConfirmationCode=code,
            Password=new_password,
        )
        return JSONResponse({"message": "Password reset successful"})
    except Exception as e:
        err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if err_code == "CodeMismatchException":
            return JSONResponse({"error": "Invalid verification code"}, status_code=400)
        elif err_code == "ExpiredCodeException":
            return JSONResponse({"error": "Verification code has expired"}, status_code=400)
        elif err_code == "InvalidPasswordException":
            msg = getattr(e, "response", {}).get("Error", {}).get("Message", str(e))
            return JSONResponse({"error": msg}, status_code=400)
        else:
            logger.exception("Auth confirm-reset error")
            return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


async def handle_settings_get(request: Request) -> Response:
    try:
        tenant_id, _ = await _get_auth_info(request)
        tenant = await tenants.get_tenant(tenant_id)
        s = tenant.settings
        available_skills = [
            {
                "name": sk.name,
                "description": sk.description.strip(),
                "requires_integration": sk.requires_integration,
            }
            for sk in skills.list_skills()
        ]
        connected_integrations = await secrets.list_integrations(tenant_id)
        return JSONResponse(
            {
                "ai_model": s.ai_model or DEFAULT_MODEL_ID,
                "providers": ai.active_providers,
                "models": get_models_for_providers(ai.active_providers),
                "platform": PLATFORM,
                "stage": STAGE,
                "build": BUILD_NUMBER,
                "enabled_skills": s.enabled_skills,
                "available_skills": available_skills,
                "connected_integrations": connected_integrations,
                "enabled_channels": s.enabled_channels,
                "system_prompt_override": s.system_prompt_override,
                "max_tokens_per_message": s.max_tokens_per_message,
                "messages_per_day": s.messages_per_day,
                "max_conversation_history": s.max_conversation_history,
                "primary_practice": s.primary_practice,
            }
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_settings_post(request: Request) -> Response:
    try:
        tenant_id, _ = await _get_auth_info(request)
        body = await request.json()
        tenant = await tenants.get_tenant(tenant_id)
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
            rebuild_skills = True
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

        if "primary_practice" in body:
            practice_name = body["primary_practice"]
            tenant.settings.primary_practice = practice_name
            # Auto-add the practice's skills to enabled_skills
            for p in practices.list_all():
                if p.name == practice_name:
                    for skill_name in p.skills:
                        if skill_name not in tenant.settings.enabled_skills:
                            tenant.settings.enabled_skills.append(skill_name)
                    break
            changed = True
            rebuild_skills = True
            logger.info(f"Primary practice set to: {practice_name}")

        if changed:
            await tenants.update_tenant(tenant)
        if rebuild_skills:
            _fire_and_forget(_rebuild_rules(tenant_id))
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_history(request: Request) -> Response:
    try:
        tenant_id, _ = await _get_auth_info(request)
        history = await memory.get_conversation(tenant_id, "dashboard-default")
        return JSONResponse({"messages": history, "platform": PLATFORM, "stage": STAGE})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------


async def handle_integrations_list(request: Request) -> Response:
    try:
        tenant_id, _ = await _get_auth_info(request)
        connected = await secrets.list_integrations(tenant_id)
        result = [
            {
                "name": name,
                "label": schema["label"],
                "connected": name in connected,
                "fields": schema["fields"],
            }
            for name, schema in INTEGRATION_SCHEMAS.items()
        ]
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_integration_get(request: Request) -> Response:
    try:
        tenant_id, _ = await _get_auth_info(request)
        integration_name = request.path_params["name"]
        if integration_name not in INTEGRATION_SCHEMAS:
            return JSONResponse(
                {"error": f"Unknown integration: {integration_name}"}, status_code=404
            )
        schema = INTEGRATION_SCHEMAS[integration_name]
        connected = False
        config = {}
        try:
            stored = await secrets.get(tenant_id, integration_name)
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
    try:
        integration_name = request.path_params["name"]
        body = await request.json()
        tenant_id, _ = await _get_auth_info(request)
        if tenant_id == DEFAULT_TENANT and body.get("tenant_id"):
            tenant_id = body["tenant_id"]

        await secrets.put(tenant_id, integration_name, body)
        logger.info(f"Stored {integration_name} credentials for tenant {tenant_id}")

        if integration_name == "telegram":
            _register_telegram_webhook(request, body)
            bot_token = body.get("bot_token", "")
            if bot_token:
                t_hash = hashlib.sha256(bot_token.encode()).hexdigest()[:16]
                await tenants.set_channel_mapping(tenant_id, "telegram", t_hash)

        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("Integration endpoint error")
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_integrations_test(request: Request) -> Response:
    try:
        integration_name = request.path_params["name"]
        body = await request.json()
        result = _test_integration(integration_name, body)
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _test_integration(name: str, creds: dict[str, Any]) -> dict[str, Any]:
    if name == "jira":
        return _test_jira(creds)
    elif name == "telegram":
        return _test_telegram(creds)
    return {"ok": False, "error": f"Testing not supported for '{name}'"}


def _register_telegram_webhook(request: Request, creds: dict[str, Any]) -> None:
    bot_token = creds.get("bot_token", "")
    if not bot_token:
        return
    try:
        token_hash = hashlib.sha256(bot_token.encode()).hexdigest()[:16]
        host = request.headers.get("host", "")
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


def _enrich_match_params(match: Any, clean_text: str) -> None:
    """Inject original user text into match params for skills that expect a 'text' field.

    Called after Tier 1/2 routing, before dispatch. The router identifies the skill,
    the skill processes the content. This ensures all channels (dashboard, Telegram,
    Teams) behave identically.
    """
    if not match:
        return
    skill_def = skills.get_skill(match.skill_name)
    if skill_def:
        schema_props = skill_def.parameters.get("properties", {})
        if "text" in schema_props and "text" not in match.params:
            match.params["text"] = clean_text


async def _rebuild_rules(tenant_id: str) -> None:
    """(Re)build AI-generated routing rules for a tenant and cache the engine."""
    try:
        tenant = await tenants.get_tenant(tenant_id)
        all_skills = skills.list_skills()
        enabled = [s for s in all_skills if s.name in tenant.settings.enabled_skills]
        disabled = [s for s in all_skills if s.name not in tenant.settings.enabled_skills]

        existing = await rule_store.load_rule_set(tenant_id)
        old_version = existing.version if existing else 0

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
        logger.info(
            f"Training: logging example for tenant={tenant_id} skill={matched_skill} "
            f"msg={message_text[:40]!r}"
        )
        await training_store.log_example(example)
        logger.info(f"Training: logged example {example.example_id}")
    except Exception:
        logger.exception("Failed to log training example")


# ---------------------------------------------------------------------------
# Chat & clear
# ---------------------------------------------------------------------------


async def handle_chat(request: Request) -> Response:
    try:
        tenant_id, user_email = await _get_auth_info(request)
        body = await request.json()
        text = body.get("text", "").strip()
        if not text:
            return JSONResponse({"error": "Empty message"}, status_code=400)

        conversation_id = body.get("conversation_id", "default")
        clean_text, is_raw = strip_raw_flag(text)
        is_raw_response = False
        request_start = time.time()

        logger.info(f"Chat [{tenant_id}]: {text[:100]}" + (" [RAW]" if is_raw else ""))

        history = _strip_metadata(await memory.get_conversation(tenant_id, conversation_id))
        tenant = await tenants.get_tenant(tenant_id)
        active_provider, active_model, model_short_name = _resolve_model(tenant)
        provider_ai = ai.for_provider(active_provider)

        system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
When you have data to present, format it clearly with structure."""

        engine = _get_engine(tenant_id)
        if not is_raw and is_conversational(clean_text):
            stats["conversational"] += 1
            messages = history + [{"role": "user", "content": clean_text}]
            response = await provider_ai.chat(active_model, system, messages, [])
            assistant_text = response.text or "Hey! How can I help?"
            total_tokens = response.input_tokens + response.output_tokens
            route_type = "conversational"
        else:
            router = engine or _fallback_router
            match = router.match(clean_text, tenant.settings.enabled_skills) if router else None

            if match:
                _enrich_match_params(match, clean_text)

                if USE_ASYNC_SKILLS and event_bus and pending_store:
                    return await _handle_async_skill(
                        tenant_id,
                        user_email,
                        match.skill_name,
                        match.params,
                        conversation_id,
                        clean_text,
                        is_raw,
                        "rule",
                        active_model,
                        model_short_name,
                    )

                request_id = f"rule-{conversation_id}"
                await bus.publish_skill_invocation(
                    tenant_id,
                    match.skill_name,
                    match.params,
                    conversation_id,
                    request_id,
                    "dashboard",
                    "user",
                )
                skill_result = bus.get_result(request_id) or {"error": "No result"}

                if is_raw and engine and engine.supports_raw(match.skill_name):
                    stats["raw"] += 1
                    stats["rule_routed"] += 1
                    assistant_text = _format_raw_json(skill_result)
                    total_tokens = 0
                    route_type = "rule"
                    is_raw_response = True
                else:
                    stats["rule_routed"] += 1
                    prompt = (
                        f'{system}\n\nThe user asked: "{clean_text}"\n\n'
                        f"Tool data:\n{json.dumps(skill_result, indent=2)}\n\nFormat this clearly."
                    )
                    messages = history + [{"role": "user", "content": prompt}]
                    response = await provider_ai.chat(active_model, system, messages, [])
                    assistant_text = response.text or "Got data but couldn't format."
                    total_tokens = response.input_tokens + response.output_tokens
                    route_type = "rule"
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
                    _log_training(tenant_id, clean_text, None, None, was_disabled_skill=True)
                )
            else:
                stats["ai_routed"] += 1
                tools = skills.get_tools_for_tenant(type("C", (), {"tenant": tenant})())
                messages = history + [{"role": "user", "content": clean_text}]
                response = await provider_ai.chat(active_model, system, messages, tools)

                if response.has_tool_use:
                    tc = response.tool_calls[0]

                    if USE_ASYNC_SKILLS and event_bus and pending_store:
                        return await _handle_async_skill(
                            tenant_id,
                            user_email,
                            tc.tool_name,
                            tc.tool_params,
                            conversation_id,
                            clean_text,
                            is_raw,
                            "ai",
                            active_model,
                            model_short_name,
                        )

                    request_id = f"ai-{conversation_id}"
                    await bus.publish_skill_invocation(
                        tenant_id,
                        tc.tool_name,
                        tc.tool_params,
                        conversation_id,
                        request_id,
                        "dashboard",
                        "user",
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

                    if is_raw and engine and engine.supports_raw(tc.tool_name):
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
                                        "id": tc.tool_use_id,
                                        "name": tc.tool_name,
                                        "input": tc.tool_params,
                                    }
                                ],
                            }
                        ]
                        final = await provider_ai.chat_with_tool_result(
                            active_model,
                            system,
                            messages_with_tool,
                            tools,
                            tc.tool_use_id,
                            skill_result,
                        )
                        assistant_text = final.text or "Got data but couldn't format."
                        total_tokens = (
                            response.input_tokens
                            + response.output_tokens
                            + final.input_tokens
                            + final.output_tokens
                        )
                        route_type = "ai"
                else:
                    assistant_text = response.text or "Not sure how to help."
                    total_tokens = response.input_tokens + response.output_tokens
                    route_type = "ai"
                    _fire_and_forget(_log_training(tenant_id, clean_text, None, None))

        stats["total_tokens"] += total_tokens
        roundtrip_sec = round(time.time() - request_start, 1)
        chat_metadata: dict[str, Any] = {
            "route": route_type,
            "model": model_short_name,
            "tokens": total_tokens,
            "timestamp": int(request_start * 1000),
            "roundtrip_sec": roundtrip_sec,
        }
        if user_email:
            chat_metadata["user_email"] = user_email
        if not is_raw_response:
            await memory.save_turn(
                tenant_id, conversation_id, clean_text, assistant_text, metadata=chat_metadata
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


async def _handle_async_skill(
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
    import uuid

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
    pending_store.create(pending_req)  # type: ignore[union-attr]
    await event_bus.publish_skill_invocation(  # type: ignore[union-attr]
        tenant_id, skill_name, params, conversation_id, request_id, "dashboard", user_key
    )
    stats[f"{route_type}_routed"] += 1
    logger.info(
        f"Chat: async skill '{skill_name}' dispatched, request={request_id[:8]}, user={user_key}"
    )
    if route_type == "ai":
        _fire_and_forget(_log_training(tenant_id, user_message, skill_name, params.get("action")))
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


async def handle_clear(request: Request) -> Response:
    try:
        tenant_id, _ = await _get_auth_info(request)
        body = await request.json()
        cid = body.get("conversation_id", "default")
        await memory.clear_conversation(tenant_id, cid)
        return JSONResponse({"cleared": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Invitations
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
# Admin and Platform API delegation (run in thread pool — they use asyncio.run internally)
# ---------------------------------------------------------------------------


async def handle_rules_admin(request: Request) -> Response:
    """POST /api/admin/rules/rebuild and GET /api/admin/rules/status."""
    method = request.method
    path = str(request.url.path)
    tenant_id, _ = await _get_auth_info(request)

    if method == "POST" and path.endswith("/rebuild"):
        _fire_and_forget(_rebuild_rules(tenant_id))
        return JSONResponse({"rebuilding": True, "tenant_id": tenant_id})

    if method == "GET" and path.endswith("/status"):
        rule_set = await rule_store.load_rule_set(tenant_id)
        engine = _compiled_engines.get(tenant_id)
        return JSONResponse(
            {
                "tenant_id": tenant_id,
                "version": rule_set.version if rule_set else 0,
                "generated_at": rule_set.generated_at if rule_set else None,
                "skill_count": len(rule_set.rules) if rule_set else 0,
                "engine_loaded": engine is not None,
            }
        )

    return JSONResponse({"error": "Not found"}, status_code=404)


async def handle_admin(request: Request) -> Response:
    """Delegate all /api/admin/* routes to AdminAPI (run in thread pool)."""
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
    data, status = await asyncio.to_thread(admin_api.handle_request, method, path, headers, body)
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
# Teams webhook
# ---------------------------------------------------------------------------


async def handle_teams_webhook(request: Request) -> Response:
    try:
        body_bytes = await request.body()
        activity = json.loads(body_bytes) if body_bytes else {}
        activity_type = activity.get("type", "")
        logger.info(f"Teams webhook: type={activity_type}")

        recipient_id = activity.get("recipient", {}).get("id", "")
        teams_adapter = await _get_teams_adapter(recipient_id)

        if not teams_adapter:
            logger.warning(f"No Teams adapter for recipient {recipient_id}")
            return JSONResponse({"error": "Bot not configured"}, status_code=401)

        auth_header = request.headers.get("authorization", "")
        if auth_header and not teams_adapter.validate_webhook(dict(request.headers), body_bytes):
            logger.warning("Teams webhook JWT validation failed")
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        if activity_type == "message" and TeamsAdapter.is_message_activity(activity):
            await _handle_teams_message(teams_adapter, activity)
        elif TeamsAdapter.is_bot_added(activity):
            await _handle_teams_bot_added(teams_adapter, activity)
        else:
            logger.debug(f"Ignoring Teams activity type: {activity_type}")

        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("Teams webhook error")
        return JSONResponse({"error": str(e)}, status_code=500)


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


async def _handle_teams_message(teams_adapter: TeamsAdapter, activity: dict[str, Any]) -> None:
    from agent.models.message import OutboundMessage

    message = teams_adapter.parse_inbound(activity)
    text = message.text
    if not text:
        return

    recipient_id = activity.get("recipient", {}).get("id", "")
    try:
        tenant = await tenants.get_by_channel_id("teams", recipient_id)
    except Exception:
        logger.warning(f"No tenant mapped for Teams bot {recipient_id}")
        return

    tenant_id = tenant.tenant_id
    conversation_id = f"teams-{message.conversation_id}"
    logger.info(f"Teams [{tenant_id}]: {text[:100]}")

    await teams_adapter.send_typing_indicator(message.conversation_id)

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
        teams_router = teams_engine or _fallback_router
        match = (
            teams_router.match(clean_text, tenant.settings.enabled_skills) if teams_router else None
        )
        if match:
            _enrich_match_params(match, clean_text)

            if USE_ASYNC_SKILLS and event_bus and pending_store:
                service_url = teams_adapter._service_urls.get(message.conversation_id, "")
                _handle_async_channel_skill(
                    tenant_id=tenant_id,
                    channel="teams",
                    skill_name=match.skill_name,
                    params=match.params,
                    conversation_id=conversation_id,
                    reply_target=message.conversation_id,
                    user_key=message.channel_user_id,
                    user_message=clean_text,
                    is_raw=is_raw,
                    route_type="rule",
                    model_id=active_model,
                    model_short_name=model_short_name,
                    service_url=service_url,
                )
                return

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
            if is_raw and teams_engine and teams_engine.supports_raw(match.skill_name):
                stats["raw"] += 1
                stats["rule_routed"] += 1
                assistant_text = _format_raw_json(skill_result)
                total_tokens = 0
            else:
                stats["rule_routed"] += 1
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
                if USE_ASYNC_SKILLS and event_bus and pending_store:
                    service_url = teams_adapter._service_urls.get(message.conversation_id, "")
                    _handle_async_channel_skill(
                        tenant_id=tenant_id,
                        channel="teams",
                        skill_name=tc.tool_name,
                        params=tc.tool_params,
                        conversation_id=conversation_id,
                        reply_target=message.conversation_id,
                        user_key=message.channel_user_id,
                        user_message=clean_text,
                        is_raw=is_raw,
                        route_type="ai",
                        model_id=active_model,
                        model_short_name=model_short_name,
                        service_url=service_url,
                    )
                    return

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
                if is_raw and teams_engine and teams_engine.supports_raw(tc.tool_name):
                    stats["raw"] += 1
                    assistant_text = _format_raw_json(skill_result)
                    total_tokens = response.input_tokens + response.output_tokens
                else:
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
                        active_model,
                        system,
                        messages_with_tool,
                        tools,
                        tc.tool_use_id,
                        skill_result,
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
        metadata={
            "route": "teams",
            "model": model_short_name,
            "tokens": total_tokens,
            "channel": "teams",
        },
    )
    outbound = OutboundMessage(
        channel=ChannelType.TEAMS,
        conversation_id=message.conversation_id,
        recipient_id=message.channel_user_id,
        text=assistant_text,
    )
    await teams_adapter.send_response(outbound)


async def _handle_teams_bot_added(teams_adapter: TeamsAdapter, activity: dict[str, Any]) -> None:
    from agent.models.message import OutboundMessage

    conversation_id = activity.get("conversation", {}).get("id", "")
    if not conversation_id:
        return
    service_url = activity.get("serviceUrl", "")
    if service_url:
        teams_adapter._service_urls[conversation_id] = service_url.rstrip("/")

    welcome = OutboundMessage(
        channel=ChannelType.TEAMS,
        conversation_id=conversation_id,
        recipient_id="",
        text=(
            "Hi! I'm your T3nets assistant. "
            "Ask me about sprint status, release notes, and more. "
            "Type **help** to see what I can do."
        ),
    )
    await teams_adapter.send_response(welcome)


def _handle_async_channel_skill(
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
    import uuid

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
    pending_store.create(pending_req)  # type: ignore[union-attr]
    asyncio.ensure_future(
        event_bus.publish_skill_invocation(  # type: ignore[union-attr]
            tenant_id, skill_name, params, conversation_id, request_id, channel, user_key
        )
    )
    stats[f"{route_type}_routed"] += 1
    logger.info(
        f"{channel.capitalize()}: async skill '{skill_name}' dispatched, request={request_id[:8]}"
    )


# ---------------------------------------------------------------------------
# Telegram webhook
# ---------------------------------------------------------------------------


async def handle_telegram_webhook(request: Request) -> Response:
    try:
        body_bytes = await request.body()
        update = json.loads(body_bytes) if body_bytes else {}

        # Extract token hash from URL path
        token_hash = request.path_params.get("token_hash", "")
        telegram_adapter = await _get_telegram_adapter(token_hash)
        if not telegram_adapter:
            logger.warning(f"No Telegram adapter for token hash {token_hash[:8]}...")
            return JSONResponse({"error": "Bot not configured"}, status_code=401)

        if not telegram_adapter.validate_webhook(dict(request.headers), body_bytes):
            logger.warning("Telegram webhook secret validation failed")
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        if TelegramAdapter.is_message_update(update):
            await _handle_telegram_message(telegram_adapter, update)

        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("Telegram webhook error")
        return JSONResponse({"error": str(e)}, status_code=500)


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


async def _handle_telegram_message(adapter: TelegramAdapter, update: dict[str, Any]) -> None:
    from agent.models.message import OutboundMessage

    message = adapter.parse_inbound(update)
    text = message.text
    if not text:
        return

    token_hash = hashlib.sha256(adapter.bot_token.encode()).hexdigest()[:16]
    try:
        tenant = await tenants.get_by_channel_id("telegram", token_hash)
    except Exception:
        logger.warning(f"No tenant mapped for Telegram bot {token_hash[:8]}")
        return

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
        tg_router = tg_engine or _fallback_router
        match = tg_router.match(clean_text, tenant.settings.enabled_skills) if tg_router else None
        if match:
            _enrich_match_params(match, clean_text)

            if USE_ASYNC_SKILLS and event_bus and pending_store:
                _handle_async_channel_skill(
                    tenant_id=tenant_id,
                    channel="telegram",
                    skill_name=match.skill_name,
                    params=match.params,
                    conversation_id=conversation_id,
                    reply_target=message.conversation_id,
                    user_key=message.channel_user_id,
                    user_message=clean_text,
                    is_raw=is_raw,
                    route_type="rule",
                    model_id=active_model,
                    model_short_name=model_short_name,
                )
                return

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
                if USE_ASYNC_SKILLS and event_bus and pending_store:
                    _handle_async_channel_skill(
                        tenant_id=tenant_id,
                        channel="telegram",
                        skill_name=tc.tool_name,
                        params=tc.tool_params,
                        conversation_id=conversation_id,
                        reply_target=message.conversation_id,
                        user_key=message.channel_user_id,
                        user_message=clean_text,
                        is_raw=is_raw,
                        route_type="ai",
                        model_id=active_model,
                        model_short_name=model_short_name,
                    )
                    return

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
                    active_model,
                    system,
                    messages_with_tool,
                    tools,
                    tc.tool_use_id,
                    skill_result,
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
# Practice endpoints
# ---------------------------------------------------------------------------


async def handle_skill_invoke(request: Request) -> Response:
    """POST /api/skill/{name} — invoke a skill synchronously from practice pages."""
    skill_name = request.path_params["name"]
    try:
        body = await request.json()
        worker_fn = skills.get_worker(skill_name)

        skill = skills.get_skill(skill_name)
        skill_secrets: dict[str, Any] = {}
        if skill and skill.requires_integration:
            tenant_id = getattr(request.state, "tenant_id", DEFAULT_TENANT)
            try:
                skill_secrets = await secrets.get(tenant_id, skill.requires_integration)
            except Exception:
                pass

        tenant_id = getattr(request.state, "tenant_id", DEFAULT_TENANT)
        ctx: dict[str, Any] = {"blob_store": blobs, "tenant_id": tenant_id}
        sig = inspect.signature(worker_fn)
        if len(sig.parameters) >= 3:
            result = worker_fn(body, skill_secrets, ctx)
        else:
            result = worker_fn(body, skill_secrets)

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


async def handle_practices_upload(request: Request) -> Response:
    """POST /api/practices/upload — upload and install a practice ZIP."""
    try:
        body = await request.body()
        data_dir = Path("data")
        tenant_id = getattr(request.state, "tenant_id", DEFAULT_TENANT)

        # Get installed versions from tenant settings for version check
        tenant = await tenants.get_tenant(tenant_id)
        installed_versions = tenant.settings.installed_practices

        practice = await practices.install_zip(
            body, data_dir, blob_store=blobs, tenant_id=tenant_id,
            installed_versions=installed_versions,
        )
        practices.register_skills(skills)

        # Persist version to DynamoDB
        tenant.settings.installed_practices[practice.name] = practice.version
        await tenants.update_tenant(tenant)

        # Deploy Lambdas + rebuild rules in background (takes 15-30s)
        async def _deploy_and_rebuild() -> None:
            lambda_config = _get_lambda_deploy_config()
            if lambda_config["lambda_role_arn"]:
                deployed = await practices.deploy_skill_lambdas(practice, lambda_config)
                logger.info(f"Background: deployed Lambdas for {deployed}")
            await _rebuild_rules(tenant_id)
            logger.info(f"Background: rules rebuilt for tenant {tenant_id}")

        _fire_and_forget(_deploy_and_rebuild())

        return JSONResponse(
            {
                "ok": True,
                "name": practice.name,
                "version": practice.version,
                "skills": practice.skills,
                "status": "Lambdas deploying in background...",
            }
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"Practice upload failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def handle_practices_pages(request: Request) -> Response:
    """GET /api/practices/pages — pages available to current tenant."""
    try:
        tenant_id = getattr(request.state, "tenant_id", DEFAULT_TENANT)
        tenant = await tenants.get_tenant(tenant_id)
        pages = practices.get_pages_for_tenant(tenant)
        return JSONResponse(pages)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def practice_page(request: Request) -> Response:
    """Serve a practice page at /p/{practice}/{page}."""
    practice_name = request.path_params["practice"]
    page_slug = request.path_params["page"]
    page_path = practices.get_page_path(practice_name, page_slug)
    if page_path and page_path.exists():
        return FileResponse(str(page_path), media_type="text/html")
    return Response(status_code=404, content="Practice page not found")


async def handle_callback(request: Request) -> Response:
    """POST /api/callback/{request_id} — external service delivers async result."""
    request_id = request.path_params["request_id"]
    if not pending_store:
        return JSONResponse({"error": "Async skills not enabled"}, status_code=501)

    pending = await pending_store.get(request_id)
    if not pending:
        return JSONResponse({"error": "Request not found or expired"}, status_code=404)

    try:
        body = await request.json()
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"Callback failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


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


async def init() -> None:
    global ai, memory, tenants, secrets, skills, bus, event_bus, pending_store
    global sqs_poller, result_router, rule_store, training_store, admin_api, platform_api
    global error_handler, started_at, _fallback_router, practices, blobs

    started_at = time.time()

    region = AWS_REGION
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

    # Initialize AI providers — both can run simultaneously
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

    # Practices + BlobStore
    try:
        from adapters.aws.s3_blob_store import S3BlobStore

        s3_bucket = os.getenv("S3_BUCKET_NAME", "t3nets-dev-static")
        blobs = S3BlobStore(bucket_name=s3_bucket, region=region)
        logger.info(f"S3 BlobStore: {s3_bucket}")
    except Exception as e:
        logger.warning(f"S3BlobStore init failed ({e}), blobs disabled")
        blobs = None

    practices_obj = PracticeRegistry()
    practices_dir = Path(__file__).parent.parent.parent / "agent" / "practices"
    practices_obj.load_builtin(practices_dir)

    # Restore uploaded practices from S3 (survive container restarts/deploys)
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    if blobs:
        try:
            # Get installed practice versions from DynamoDB
            default_tenant = await tenants.get_tenant(DEFAULT_TENANT)
            installed_versions = default_tenant.settings.installed_practices
            restored = await practices_obj.restore_from_blob_store(
                blobs, DEFAULT_TENANT, data_dir,
                installed_versions=installed_versions,
            )
            if restored:
                logger.info(f"Restored {restored} practice(s) from S3")
        except Exception as e:
            logger.warning(f"Practice restore from S3 failed: {e}")

    # Load uploaded practices (from restore or previous extractions)
    practices_obj.load_uploaded(data_dir)
    practices_obj.register_skills(skills)
    practices = practices_obj
    logger.info(f"Loaded practices: {[p.name for p in practices.list_all()]}")
    logger.info(f"Loaded skills: {skills.list_skill_names()}")

    # Ensure Lambdas exist for restored practice skills
    lambda_config = _get_lambda_deploy_config()
    if lambda_config["lambda_role_arn"]:
        try:
            fixed = await practices_obj.ensure_skill_lambdas(lambda_config)
            if fixed:
                logger.info(f"Deployed {fixed} missing practice skill Lambda(s)")
        except Exception as e:
            logger.warning(f"Lambda ensure check failed: {e}")

    _fallback_router = RuleBasedRouter(skills)

    rule_store = DynamoDBRuleStore(tenants_table, region=region)
    training_store = DynamoDBTrainingStore(tenants_table, region=region)
    bus = DirectBus(skills, secrets)
    admin_api = AdminAPI(tenants, secrets, skills, training_store)
    platform_api = PlatformAPI(tenants, secrets, skills)
    error_handler = ErrorHandler()

    if USE_ASYNC_SKILLS:
        eb_bus_name = os.environ.get("EVENTBRIDGE_BUS_NAME", "")
        sqs_queue_url = os.environ.get("SQS_RESULTS_QUEUE_URL", "")
        pending_table = os.environ.get("PENDING_REQUESTS_TABLE", "")

        if not all([eb_bus_name, sqs_queue_url, pending_table]):
            logger.error(
                "USE_ASYNC_SKILLS=true but missing required env vars: "
                "EVENTBRIDGE_BUS_NAME, SQS_RESULTS_QUEUE_URL, PENDING_REQUESTS_TABLE"
            )
            logger.warning("Falling back to synchronous DirectBus")
        else:
            event_bus = EventBridgeBus(eb_bus_name, region=region)
            pending_store = PendingRequestsStore(pending_table, region=region)
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
    else:
        logger.info("Async skills DISABLED (USE_ASYNC_SKILLS=false), using DirectBus")

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

    # Load compiled rule engines for all tenants from DynamoDB
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
