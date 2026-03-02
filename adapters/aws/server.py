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
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from adapters.shared.base_handler import BaseHandler
from adapters.shared.server_utils import (
    INTEGRATION_SCHEMAS,
    _format_raw_json,
    _strip_metadata,
    _uptime_human,
)

from agent.skills.registry import SkillRegistry
from agent.channels.base import ChannelRegistry
from agent.channels.dashboard import DashboardAdapter
from agent.models.message import ChannelType
from agent.router.rule_router import RuleBasedRouter, strip_raw_flag
from adapters.aws.bedrock_provider import BedrockProvider
from adapters.aws.dynamodb_conversation_store import DynamoDBConversationStore
from adapters.aws.dynamodb_tenant_store import DynamoDBTenantStore
from adapters.aws.secrets_manager import SecretsManagerProvider
from adapters.aws.auth_middleware import extract_auth, AuthError
from adapters.aws.admin_api import AdminAPI
from adapters.aws.platform_api import PlatformAPI
from adapters.local.direct_bus import DirectBus
from adapters.aws.event_bridge_bus import EventBridgeBus
from adapters.aws.pending_requests import PendingRequestsStore, PendingRequest
from adapters.aws.sqs_poller import SQSResultPoller
from adapters.aws.result_router import AsyncResultRouter
from agent.models.tenant import Invitation
from agent.channels.teams import TeamsAdapter
from agent.channels.telegram import TelegramAdapter
from agent.errors.handler import ErrorHandler
from agent.sse import SSEConnectionManager, start_keepalive_thread
from adapters.aws.ws_connections import WebSocketConnectionManager
from agent.models.ai_models import (
    DEFAULT_MODEL_ID,
    get_model,
    get_model_for_provider,
    get_models_for_provider,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("t3nets.aws")

# --- Global state ---
ai: BedrockProvider
memory: DynamoDBConversationStore
tenants: DynamoDBTenantStore
secrets: SecretsManagerProvider
skills: SkillRegistry
bus: DirectBus  # Sync fallback — replaced by EventBridgeBus when async is enabled
event_bus: EventBridgeBus | None = None  # Async bus (Phase 3b)
pending_store: PendingRequestsStore | None = None
sqs_poller: SQSResultPoller | None = None
result_router: AsyncResultRouter | None = None
rule_router: RuleBasedRouter
admin_api: AdminAPI
platform_api: PlatformAPI
error_handler: ErrorHandler
started_at: float = 0.0
USE_ASYNC_SKILLS = os.environ.get("USE_ASYNC_SKILLS", "false").lower() == "true"

DEFAULT_TENANT = "default"
BEDROCK_MODEL_ID = os.environ["BEDROCK_MODEL_ID"]  # full inference profile ID from Terraform
PROVIDER = "bedrock"
PLATFORM = os.environ.get("T3NETS_PLATFORM", "aws")  # aws, gcp, azure
STAGE = os.environ.get("T3NETS_STAGE", "dev")  # dev, staging, prod — set by Terraform
WS_API_ENDPOINT = os.environ.get("WS_API_ENDPOINT", "")
# Derive management endpoint from WS API endpoint: wss://xxx → https://xxx
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
    sse_manager: SSEConnectionManager | None = None  # type: ignore[assignment]
    logger.info(f"Push transport: WebSocket (endpoint={WS_MANAGEMENT_ENDPOINT[:40]}...)")
else:
    push_client = SSEConnectionManager()
    ws_manager = None
    sse_manager = push_client  # type: ignore[assignment]
    logger.info("Push transport: SSE (no WS_MANAGEMENT_ENDPOINT configured)")


def _run_async(coro):
    return asyncio.run(coro)


def _bedrock_geo_prefix() -> str:
    """Map AWS region to Bedrock geographic inference profile prefix.

    Newer models (Sonnet 4.5+, Nova) require geographic prefixes (us., eu., apac.),
    NOT region-specific ones (us-east-1.).
    """
    region = os.environ.get("AWS_REGION", AWS_REGION)
    if region.startswith("us-") or region.startswith("ca-") or region.startswith("sa-"):
        return "us"
    elif region.startswith("eu-"):
        return "eu"
    elif region.startswith("ap-"):
        return "apac"
    return "us"


def _resolve_model(tenant):
    """Resolve tenant's ai_model to a Bedrock inference profile ID and short name."""
    model_id = tenant.settings.ai_model or DEFAULT_MODEL_ID
    model = get_model(model_id)
    if not model:
        # Unknown model in tenant settings — fall back to registry default
        logger.warning(f"Unknown model '{model_id}', falling back to {DEFAULT_MODEL_ID}")
        model_id = DEFAULT_MODEL_ID
        model = get_model(model_id)
    bedrock_id = get_model_for_provider(model_id, PROVIDER)
    if bedrock_id:
        geo = _bedrock_geo_prefix()
        full_id = f"{geo}.{bedrock_id}"
        logger.info(f"Resolved model: {model_id} → {full_id}")
        return full_id, model.short_name
    # Fallback: use the env var model ID directly
    return BEDROCK_MODEL_ID, model.short_name


def _get_auth_info(headers) -> tuple[str, str]:
    """Extract (tenant_id, user_email) from JWT in Authorization header.

    Resolution: DynamoDB lookup by IdP sub → DEFAULT_TENANT.
    The email is always extracted from the JWT payload (never lost).
    """
    if not COGNITO_USER_POOL_ID:
        return DEFAULT_TENANT, ""
    try:
        auth = extract_auth(headers)
        email = auth.email

        # Look up user in DynamoDB by IdP sub
        try:
            user = _run_async(tenants.get_user_by_cognito_sub(auth.user_id))
            if user:
                logger.info(
                    f"Resolved tenant '{user.tenant_id}' from DynamoDB "
                    f"for sub {auth.user_id[:8]}..."
                )
                return user.tenant_id, email
        except Exception as e:
            logger.warning(f"DynamoDB sub lookup failed: {e}")

        # User not found — new user, needs onboarding
        return DEFAULT_TENANT, email
    except AuthError:
        return DEFAULT_TENANT, ""


class AWSHandler(BaseHandler):
    """HTTP request handler for AWS deployment."""

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        self._dispatch(
            {
                "/": lambda: self._serve_file("chat.html", "adapters/local"),
                "/chat": lambda: self._serve_file("chat.html", "adapters/local"),
                "/health": lambda: self._serve_file("health.html", "adapters/local"),
                "/settings": lambda: self._serve_file("settings.html", "adapters/local"),
                "/login": lambda: self._serve_file("login.html", "adapters/local"),
                "/callback": lambda: self._serve_file("callback.html", "adapters/local"),
                "/onboard": lambda: self._serve_file("onboard.html", "adapters/local"),
                "/platform": lambda: self._serve_file("platform.html", "adapters/local"),
                "/api/events": self._handle_sse,
                "/api/health": self._handle_health_api,
                "/api/settings": self._handle_settings_get,
                "/api/history": self._handle_history,
                "/api/auth/config": self._handle_auth_config,
                "/api/auth/me": self._handle_auth_me,
                "/api/integrations": self._handle_integrations_list,
                "/api/integrations/*": lambda: self._handle_integration_get(path),
                "/api/invitations/validate": self._handle_invitation_validate,
                "/api/platform/*": lambda: self._handle_platform("GET", path),
                "/api/admin/*": lambda: self._handle_admin("GET", path),
            },
            path,
        )

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        # WebSocket API Gateway routes — identified by X-WS-Route header
        ws_route = self.headers.get("X-WS-Route", "")
        if ws_route:
            if ws_route == "$connect":
                self._handle_ws_connect()
            elif ws_route == "$disconnect":
                self._handle_ws_disconnect()
            else:
                self._handle_ws_default()
            return

        self._dispatch(
            {
                "/api/channels/teams/webhook": self._handle_teams_webhook,
                "/api/channels/telegram/webhook*": self._handle_telegram_webhook,
                "/api/chat": self._handle_chat,
                "/api/clear": self._handle_clear,
                "/api/settings": self._handle_settings_post,
                "/api/integrations/*": lambda: self._handle_integrations_post(path),
                "/api/auth/login": self._handle_auth_login,
                "/api/auth/signup": self._handle_auth_signup,
                "/api/auth/confirm": self._handle_auth_confirm,
                "/api/auth/refresh": self._handle_auth_refresh,
                "/api/auth/forgot-password": self._handle_auth_forgot_password,
                "/api/auth/confirm-reset": self._handle_auth_confirm_reset,
                "/api/invitations/accept": self._handle_invitation_accept,
                "/api/platform/*": lambda: self._handle_platform("POST", path, self._read_json()),
                "/api/admin/*": lambda: self._handle_admin("POST", path, self._read_json()),
            },
            path,
        )

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        self._dispatch(
            {
                "/api/admin/*": lambda: self._handle_admin("PUT", path, self._read_json()),
            },
            path,
        )

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        self._dispatch(
            {
                "/api/platform/*": lambda: self._handle_platform("DELETE", path),
                "/api/admin/*": lambda: self._handle_admin("DELETE", path),
            },
            path,
        )

    def do_PATCH(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        self._dispatch(
            {
                "/api/platform/*": lambda: self._handle_platform("PATCH", path, self._read_json()),
                "/api/admin/*": lambda: self._handle_admin("PATCH", path, self._read_json()),
            },
            path,
        )

    def _handle_admin(self, method: str, path: str, body: dict | None = None):
        """Delegate to admin API."""
        data, status = admin_api.handle_request(method, path, self.headers, body)
        self._json_response(data, status)

    def _handle_platform(self, method: str, path: str, body: dict | None = None):
        """Delegate to platform API."""
        data, status = platform_api.handle_request(method, path, self.headers, body)
        self._json_response(data, status)

    def _handle_health_api(self):
        try:
            uptime_secs = time.time() - started_at
            tenant = _run_async(tenants.get_tenant(DEFAULT_TENANT))
            connected = _run_async(secrets.list_integrations(DEFAULT_TENANT))

            health = {
                "status": "ok",
                "platform": PLATFORM,
                "stage": STAGE,
                "started_at": datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat(),
                "uptime_seconds": round(uptime_secs, 1),
                "uptime_human": _uptime_human(uptime_secs),
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "tenant": {
                    "tenant_id": tenant.tenant_id,
                    "name": tenant.name,
                    "status": tenant.status,
                    "enabled_skills": tenant.settings.enabled_skills,
                    "ai_model": tenant.settings.ai_model,
                },
                "ai": {
                    "provider": "bedrock",
                    "model": _resolve_model(tenant)[0],
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
            self._json_response(health)
        except Exception as e:
            logger.exception("Health check error")
            self._json_response({"status": "error", "error": str(e)}, 500)

    def _handle_integrations_post(self, path: str):
        """Handle POST /api/integrations/{name} and /api/integrations/{name}/test."""
        try:
            parts = path.rstrip("/").split("/")
            # /api/integrations/{name}/test or /api/integrations/{name}
            is_test = parts[-1] == "test"
            integration_name = parts[-2] if is_test else parts[-1]

            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))

            # During onboarding the JWT may not yet have custom:tenant_id,
            # so accept tenant_id from the request body as a fallback.
            tenant_id, _ = _get_auth_info(self.headers)
            if tenant_id == DEFAULT_TENANT and body.get("tenant_id"):
                tenant_id = body["tenant_id"]

            if is_test:
                result = self._test_integration(integration_name, body)
                self._json_response(result, 200 if result.get("ok") else 400)
            else:
                _run_async(secrets.put(tenant_id, integration_name, body))
                logger.info(f"Stored {integration_name} credentials for tenant {tenant_id}")

                # Auto-register Telegram webhook and channel mapping after saving
                if integration_name == "telegram":
                    self._register_telegram_webhook(body)
                    # Save channel mapping for fast GSI lookup on incoming webhooks
                    import hashlib

                    bot_token = body.get("bot_token", "")
                    if bot_token:
                        t_hash = hashlib.sha256(bot_token.encode()).hexdigest()[:16]
                        _run_async(tenants.set_channel_mapping(tenant_id, "telegram", t_hash))

                self._json_response({"ok": True})
        except Exception as e:
            logger.exception("Integration endpoint error")
            self._json_response({"error": str(e)}, 500)

    def _test_integration(self, name: str, creds: dict) -> dict:
        """Test integration credentials by making a real API call."""
        if name == "jira":
            return self._test_jira(creds)
        elif name == "telegram":
            return self._test_telegram(creds)
        return {"ok": False, "error": f"Testing not supported for '{name}'"}

    def _test_telegram(self, creds: dict) -> dict:
        """Validate Telegram bot token by calling getMe."""
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

    def _register_telegram_webhook(self, creds: dict):
        """Register the Telegram webhook URL after saving credentials."""
        import hashlib

        bot_token = creds.get("bot_token", "")
        if not bot_token:
            return
        try:
            token_hash = hashlib.sha256(bot_token.encode()).hexdigest()[:16]
            # Build webhook URL from Host header or API Gateway URL
            host = self.headers.get("Host", "")
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

    def _test_jira(self, creds: dict) -> dict:
        """Validate Jira credentials by calling /rest/api/3/myself."""
        import urllib.request
        import base64

        url = creds.get("url", "").rstrip("/")
        email = creds.get("email", "")
        api_token = creds.get("api_token", "")

        if not all([url, email, api_token]):
            return {"ok": False, "error": "url, email, and api_token are required"}

        try:
            auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
            req = urllib.request.Request(
                f"{url}/rest/api/3/myself",
                headers={
                    "Authorization": f"Basic {auth}",
                    "Accept": "application/json",
                },
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

    def _handle_auth_login(self):
        """Authenticate user via Cognito InitiateAuth (USER_PASSWORD_AUTH).

        Replaces the Cognito Hosted UI redirect — credentials are submitted
        directly from the login form and exchanged for tokens server-side.
        """
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            email = body.get("email", "").strip()
            password = body.get("password", "")

            if not email or not password:
                self._json_response({"error": "Email and password are required"}, 400)
                return

            if not COGNITO_USER_POOL_ID or not COGNITO_APP_CLIENT_ID:
                self._json_response({"error": "Auth not configured"}, 500)
                return

            import boto3

            client = boto3.client("cognito-idp", region_name=AWS_REGION)
            result = client.initiate_auth(
                ClientId=COGNITO_APP_CLIENT_ID,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={
                    "USERNAME": email,
                    "PASSWORD": password,
                },
            )

            # Handle Cognito challenges (e.g. FORCE_CHANGE_PASSWORD users)
            challenge = result.get("ChallengeName", "")
            if challenge:
                logger.warning(f"Auth login challenge: {challenge} for {email}")
                self._json_response(
                    {"error": f"Account requires action: {challenge}", "code": challenge},
                    403,
                )
                return

            auth_result = result.get("AuthenticationResult", {})
            self._json_response(
                {
                    "id_token": auth_result.get("IdToken", ""),
                    "access_token": auth_result.get("AccessToken", ""),
                    "refresh_token": auth_result.get("RefreshToken", ""),
                }
            )

        except Exception as e:
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if err_code == "NotAuthorizedException":
                self._json_response({"error": "Invalid email or password"}, 401)
            elif err_code == "UserNotConfirmedException":
                self._json_response(
                    {"error": "Email not verified", "code": "USER_NOT_CONFIRMED"}, 403
                )
            elif err_code == "UserNotFoundException":
                self._json_response({"error": "Invalid email or password"}, 401)
            elif err_code == "PasswordResetRequiredException":
                self._json_response(
                    {"error": "Password reset required", "code": "PASSWORD_RESET_REQUIRED"},
                    403,
                )
            else:
                logger.exception("Auth login error")
                self._json_response({"error": str(e)}, 500)

    def _handle_auth_forgot_password(self):
        """Initiate password reset — sends a verification code to the user's email."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            email = body.get("email", "").strip()

            if not email:
                self._json_response({"error": "Email is required"}, 400)
                return

            if not COGNITO_APP_CLIENT_ID:
                self._json_response({"error": "Auth not configured"}, 500)
                return

            import boto3

            client = boto3.client("cognito-idp", region_name=AWS_REGION)
            client.forgot_password(
                ClientId=COGNITO_APP_CLIENT_ID,
                Username=email,
            )

            # Always return success — don't leak whether the email exists
            self._json_response({"message": "Reset code sent"})

        except Exception as e:
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if err_code in ("UserNotFoundException", "InvalidParameterException"):
                # Don't leak email existence
                self._json_response({"message": "Reset code sent"})
            elif err_code == "LimitExceededException":
                self._json_response({"error": "Too many attempts. Please try again later."}, 429)
            else:
                logger.exception("Auth forgot-password error")
                self._json_response({"error": str(e)}, 500)

    def _handle_auth_confirm_reset(self):
        """Complete password reset with verification code and new password."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            email = body.get("email", "").strip()
            code = body.get("code", "").strip()
            new_password = body.get("new_password", "")

            if not email or not code or not new_password:
                self._json_response({"error": "Email, code, and new password are required"}, 400)
                return

            if not COGNITO_APP_CLIENT_ID:
                self._json_response({"error": "Auth not configured"}, 500)
                return

            import boto3

            client = boto3.client("cognito-idp", region_name=AWS_REGION)
            client.confirm_forgot_password(
                ClientId=COGNITO_APP_CLIENT_ID,
                Username=email,
                ConfirmationCode=code,
                Password=new_password,
            )

            self._json_response({"message": "Password reset successful"})

        except Exception as e:
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if err_code == "CodeMismatchException":
                self._json_response({"error": "Invalid verification code"}, 400)
            elif err_code == "ExpiredCodeException":
                self._json_response({"error": "Verification code has expired"}, 400)
            elif err_code == "InvalidPasswordException":
                msg = getattr(e, "response", {}).get("Error", {}).get("Message", str(e))
                self._json_response({"error": msg}, 400)
            else:
                logger.exception("Auth confirm-reset error")
                self._json_response({"error": str(e)}, 500)

    def _handle_auth_signup(self):
        """Register a new user via Cognito SignUp."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            email = body.get("email", "").strip()
            password = body.get("password", "")
            name = body.get("name", "").strip()

            if not email or not password:
                self._json_response({"error": "Email and password are required"}, 400)
                return

            if not COGNITO_USER_POOL_ID or not COGNITO_APP_CLIENT_ID:
                self._json_response({"error": "Auth not configured"}, 500)
                return

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

            self._json_response(
                {
                    "user_sub": result.get("UserSub", ""),
                    "confirmed": result.get("UserConfirmed", False),
                },
                201,
            )

        except Exception as e:
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if err_code == "UsernameExistsException":
                self._json_response({"error": "An account with this email already exists"}, 409)
            elif err_code == "InvalidPasswordException":
                msg = getattr(e, "response", {}).get("Error", {}).get("Message", str(e))
                self._json_response({"error": msg}, 400)
            else:
                logger.exception("Auth signup error")
                self._json_response({"error": str(e)}, 500)

    def _handle_auth_confirm(self):
        """Confirm a user's email with the verification code."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            email = body.get("email", "").strip()
            code = body.get("code", "").strip()

            if not email or not code:
                self._json_response({"error": "Email and code are required"}, 400)
                return

            if not COGNITO_APP_CLIENT_ID:
                self._json_response({"error": "Auth not configured"}, 500)
                return

            import boto3

            client = boto3.client("cognito-idp", region_name=AWS_REGION)
            client.confirm_sign_up(
                ClientId=COGNITO_APP_CLIENT_ID,
                Username=email,
                ConfirmationCode=code,
            )

            self._json_response({"ok": True})

        except Exception as e:
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if err_code == "CodeMismatchException":
                self._json_response({"error": "Invalid verification code"}, 400)
            elif err_code == "ExpiredCodeException":
                self._json_response({"error": "Verification code has expired"}, 400)
            else:
                logger.exception("Auth confirm error")
                self._json_response({"error": str(e)}, 500)

    def _handle_auth_refresh(self):
        """Refresh tokens using a refresh token."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            refresh_token = body.get("refresh_token", "")

            if not refresh_token:
                self._json_response({"error": "refresh_token is required"}, 400)
                return

            if not COGNITO_APP_CLIENT_ID:
                self._json_response({"error": "Auth not configured"}, 500)
                return

            import boto3

            client = boto3.client("cognito-idp", region_name=AWS_REGION)
            result = client.initiate_auth(
                ClientId=COGNITO_APP_CLIENT_ID,
                AuthFlow="REFRESH_TOKEN_AUTH",
                AuthParameters={
                    "REFRESH_TOKEN": refresh_token,
                },
            )

            auth_result = result.get("AuthenticationResult", {})
            self._json_response(
                {
                    "id_token": auth_result.get("IdToken", ""),
                    "access_token": auth_result.get("AccessToken", ""),
                }
            )

        except Exception as e:
            logger.exception("Auth refresh error")
            self._json_response({"error": str(e)}, 500)

    def _handle_auth_config(self):
        """Return Cognito config for the frontend login flow.

        Also includes ws_endpoint so the frontend can connect to WebSocket
        transport without requiring server-side HTML injection.
        """
        response: dict = {
            "enabled": bool(COGNITO_USER_POOL_ID),
            "client_id": COGNITO_APP_CLIENT_ID,
            "auth_domain": COGNITO_AUTH_DOMAIN,
            "user_pool_id": COGNITO_USER_POOL_ID,
        }
        if WS_API_ENDPOINT:
            response["ws_endpoint"] = WS_API_ENDPOINT
        self._json_response(response)

    def _handle_auth_me(self):
        """Return current authenticated user info, including tenant status.

        Resolves tenant from DynamoDB by IdP sub. If no user found,
        returns empty tenant_id so the frontend redirects to onboarding.
        """
        if not COGNITO_USER_POOL_ID:
            self._json_response(
                {
                    "authenticated": False,
                    "tenant_id": DEFAULT_TENANT,
                    "tenant_status": "active",
                }
            )
            return
        try:
            auth = extract_auth(self.headers)
            email = auth.email
            tenant_id = ""
            display_name = ""
            avatar_url = ""
            role = "member"
            tenant_status = "onboarding"

            # Look up user in DynamoDB by IdP sub
            try:
                user = _run_async(tenants.get_user_by_cognito_sub(auth.user_id))
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

            # Determine tenant status and name
            tenant_name = ""
            if tenant_id:
                try:
                    tenant = _run_async(tenants.get_tenant(tenant_id))
                    tenant_status = tenant.status
                    tenant_name = tenant.name
                except Exception:
                    tenant_status = "active"

            self._json_response(
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
            self._json_response({"error": e.message}, e.status)

    def _handle_integrations_list(self):
        """GET /api/integrations — list all integrations with status and field schemas."""
        try:
            tenant_id, _ = _get_auth_info(self.headers)
            connected = _run_async(secrets.list_integrations(tenant_id))
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
            self._json_response(result)
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_integration_get(self, path: str):
        """GET /api/integrations/{name} — return current config with sensitive fields masked."""
        try:
            tenant_id, _ = _get_auth_info(self.headers)
            integration_name = path.rstrip("/").split("/")[-1]
            if integration_name not in INTEGRATION_SCHEMAS:
                self._json_response({"error": f"Unknown integration: {integration_name}"}, 404)
                return

            schema = INTEGRATION_SCHEMAS[integration_name]
            connected = False
            config = {}
            try:
                stored = _run_async(secrets.get(tenant_id, integration_name))
                connected = True
                password_keys = {f["key"] for f in schema["fields"] if f["type"] == "password"}
                for key, value in stored.items():
                    if key in password_keys and value:
                        config[key] = "\u2022" * 8
                    else:
                        config[key] = value
            except Exception:
                pass

            self._json_response(
                {
                    "name": integration_name,
                    "label": schema["label"],
                    "connected": connected,
                    "config": config,
                    "fields": schema["fields"],
                }
            )
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_settings_get(self):
        """Return current settings and available models."""
        try:
            tenant_id, _ = _get_auth_info(self.headers)
            tenant = _run_async(tenants.get_tenant(tenant_id))
            s = tenant.settings

            # Build available skills list from registry
            available_skills = [
                {
                    "name": sk.name,
                    "description": sk.description.strip(),
                    "requires_integration": sk.requires_integration,
                }
                for sk in skills.list_skills()
            ]

            # Connected integrations for the tenant
            connected_integrations = _run_async(secrets.list_integrations(tenant_id))

            self._json_response(
                {
                    "ai_model": s.ai_model or DEFAULT_MODEL_ID,
                    "provider": PROVIDER,
                    "models": get_models_for_provider(PROVIDER),
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
                }
            )
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_history(self):
        """Return conversation history for the authenticated tenant."""
        try:
            tenant_id, _ = _get_auth_info(self.headers)
            history = _run_async(memory.get_conversation(tenant_id, "dashboard-default"))
            self._json_response(
                {
                    "messages": history,
                    "platform": PLATFORM,
                    "stage": STAGE,
                }
            )
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_settings_post(self):
        """Update tenant settings."""
        try:
            tenant_id, _ = _get_auth_info(self.headers)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            tenant = _run_async(tenants.get_tenant(tenant_id))
            changed = False

            if "ai_model" in body:
                model_id = body["ai_model"]
                model = get_model(model_id)
                if not model:
                    self._json_response({"error": f"Unknown model: {model_id}"}, 400)
                    return
                if PROVIDER not in model.providers:
                    self._json_response(
                        {"error": f"Model '{model_id}' not available for {PROVIDER}"},
                        400,
                    )
                    return
                tenant.settings.ai_model = model_id
                changed = True
                logger.info(f"Model changed to: {model.display_name} ({model_id})")

            if "enabled_skills" in body:
                skill_list = body["enabled_skills"]
                if not isinstance(skill_list, list):
                    self._json_response({"error": "enabled_skills must be a list"}, 400)
                    return
                known = set(skills.list_skill_names())
                unknown = [s for s in skill_list if s not in known]
                if unknown:
                    self._json_response({"error": f"Unknown skills: {', '.join(unknown)}"}, 400)
                    return
                tenant.settings.enabled_skills = skill_list
                changed = True
                logger.info(f"Enabled skills updated: {skill_list}")

            if "system_prompt_override" in body:
                tenant.settings.system_prompt_override = body["system_prompt_override"]
                changed = True

            if "max_tokens_per_message" in body:
                val = body["max_tokens_per_message"]
                if not isinstance(val, int) or val < 256 or val > 16384:
                    self._json_response({"error": "max_tokens_per_message must be 256-16384"}, 400)
                    return
                tenant.settings.max_tokens_per_message = val
                changed = True

            if "messages_per_day" in body:
                val = body["messages_per_day"]
                if not isinstance(val, int) or val < 1:
                    self._json_response(
                        {"error": "messages_per_day must be a positive integer"}, 400
                    )
                    return
                tenant.settings.messages_per_day = val
                changed = True

            if "max_conversation_history" in body:
                val = body["max_conversation_history"]
                if not isinstance(val, int) or val < 1 or val > 100:
                    self._json_response({"error": "max_conversation_history must be 1-100"}, 400)
                    return
                tenant.settings.max_conversation_history = val
                changed = True

            if changed:
                _run_async(tenants.update_tenant(tenant))

            self._json_response({"ok": True})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    # --- WebSocket route handlers (API Gateway → HTTP POST → ECS) ---

    def _extract_ws_context(self) -> tuple[str, str]:
        """Extract connection ID and route key from API Gateway WebSocket request.

        API Gateway sends these as headers when using HTTP_PROXY integration
        with request parameter mappings.
        """
        connection_id = self.headers.get("X-WS-Connection-Id", "")
        route_key = self.headers.get("X-WS-Route", "")
        return connection_id, route_key

    def _extract_user_key_from_token(self) -> str:
        """Extract user identity from JWT query param (same as SSE)."""
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        token = params.get("token", [None])[0]

        if not token:
            # Also check body for token
            content_length = int(self.headers.get("Content-Length", 0) or 0)
            if content_length > 0:
                body = json.loads(self.rfile.read(content_length))
                token = body.get("token", "")

        if token:
            try:
                import base64

                payload_b64 = token.split(".")[1]
                padding = 4 - len(payload_b64) % 4
                if padding != 4:
                    payload_b64 += "=" * padding
                claims = json.loads(base64.urlsafe_b64decode(payload_b64))
                return claims.get("email", "") or claims.get("sub", "") or DEFAULT_TENANT
            except Exception:
                pass
        return DEFAULT_TENANT

    def _handle_ws_connect(self):
        """Handle WebSocket $connect — register connection in DynamoDB."""
        connection_id, _ = self._extract_ws_context()
        if not connection_id or not ws_manager:
            self._json_response({"error": "WebSocket not configured"}, 400)
            return

        user_key = self._extract_user_key_from_token()
        ws_manager.register(user_key, connection_id)
        logger.info(f"WS $connect: {connection_id[:12]} user={user_key}")
        self._json_response({"status": "connected"})

    def _handle_ws_disconnect(self):
        """Handle WebSocket $disconnect — remove connection from DynamoDB."""
        connection_id, _ = self._extract_ws_context()
        if not connection_id or not ws_manager:
            self._json_response({"status": "ok"})
            return

        ws_manager.unregister_by_connection_id(connection_id)
        logger.info(f"WS $disconnect: {connection_id[:12]}")
        self._json_response({"status": "disconnected"})

    def _handle_ws_default(self):
        """Handle WebSocket $default — no-op, just acknowledge."""
        self._json_response({"status": "ok"})

    def _handle_sse(self):
        """Server-Sent Events endpoint for async skill results.

        The dashboard opens GET /api/events to receive push notifications
        when async skill execution completes. Connection stays open until
        the client disconnects.

        Auth: JWT passed via query param (SSE doesn't support custom headers).
        AWS: Cognito JWT validated server-side.
        Keepalive comments sent every 15s to prevent API Gateway 30s timeout.
        """
        try:
            # Extract user identity from query param
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            user_key = DEFAULT_TENANT  # fallback

            token = params.get("token", [None])[0]
            if not token:
                auth_header = self.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    token = auth_header[7:]

            if token:
                try:
                    import base64

                    payload_b64 = token.split(".")[1]
                    padding = 4 - len(payload_b64) % 4
                    if padding != 4:
                        payload_b64 += "=" * padding
                    claims = json.loads(base64.urlsafe_b64decode(payload_b64))
                    user_key = claims.get("email", "") or claims.get("sub", "") or user_key
                except Exception:
                    pass

            # Open SSE stream
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            # Send initial connection confirmation
            self.wfile.write(b'event: connected\ndata: {"status": "ok"}\n\n')
            self.wfile.flush()

            # Register this connection (SSE manager must be active)
            if sse_manager is None:
                logger.warning("SSE: endpoint called but WebSocket transport is active")
                return
            sse_manager.register(user_key, self.wfile)
            logger.info(f"SSE: client connected (user={user_key})")

            try:
                while True:
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                sse_manager.unregister(user_key, self.wfile)
                logger.info(f"SSE: client disconnected (user={user_key})")

        except Exception as e:
            logger.exception("SSE connection error")

    def _handle_chat(self):
        """Handle chat — supports both sync (DirectBus) and async (EventBridge) paths."""
        try:
            tenant_id, user_email = _get_auth_info(self.headers)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = body.get("text", "").strip()
            if not text:
                self._json_response({"error": "Empty message"}, 400)
                return

            conversation_id = body.get("conversation_id", "default")
            clean_text, is_raw = strip_raw_flag(text)
            is_raw_response = False
            request_start = time.time()

            logger.info(f"Chat [{tenant_id}]: {text[:100]}" + (" [RAW]" if is_raw else ""))

            history = _strip_metadata(
                _run_async(memory.get_conversation(tenant_id, conversation_id))
            )
            tenant = _run_async(tenants.get_tenant(tenant_id))
            active_model, model_short_name = _resolve_model(tenant)

            system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
When you have data to present, format it clearly with structure."""

            # --- Conversational messages: always sync (no skill needed) ---
            if not is_raw and rule_router.is_conversational(clean_text):
                stats["conversational"] += 1
                messages = history + [{"role": "user", "content": clean_text}]
                response = _run_async(ai.chat(active_model, system, messages, []))
                assistant_text = response.text or "Hey! How can I help?"
                total_tokens = response.input_tokens + response.output_tokens
                route_type = "conversational"
            else:
                match = rule_router.match(clean_text, tenant.settings.enabled_skills)

                if match:
                    # --- Async path: publish to EventBridge, return immediately ---
                    if USE_ASYNC_SKILLS and event_bus and pending_store:
                        return self._handle_async_skill(
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

                    # --- Sync fallback: DirectBus ---
                    request_id = f"rule-{conversation_id}"
                    _run_async(
                        bus.publish_skill_invocation(
                            tenant_id,
                            match.skill_name,
                            match.params,
                            conversation_id,
                            request_id,
                            "dashboard",
                            "user",
                        )
                    )
                    skill_result = bus.get_result(request_id) or {"error": "No result"}

                    if is_raw and rule_router.supports_raw(match.skill_name):
                        stats["raw"] += 1
                        stats["rule_routed"] += 1
                        assistant_text = _format_raw_json(skill_result)
                        total_tokens = 0
                        route_type = "rule"
                        is_raw_response = True
                    else:
                        stats["rule_routed"] += 1
                        prompt = f'{system}\n\nThe user asked: "{clean_text}"\n\nTool data:\n{json.dumps(skill_result, indent=2)}\n\nFormat this clearly.'
                        messages = history + [{"role": "user", "content": prompt}]
                        response = _run_async(ai.chat(active_model, system, messages, []))
                        assistant_text = response.text or "Got data but couldn't format."
                        total_tokens = response.input_tokens + response.output_tokens
                        route_type = "rule"
                else:
                    stats["ai_routed"] += 1
                    tools = skills.get_tools_for_tenant(type("C", (), {"tenant": tenant})())
                    messages = history + [{"role": "user", "content": clean_text}]
                    response = _run_async(ai.chat(active_model, system, messages, tools))

                    if response.has_tool_use:
                        tc = response.tool_calls[0]

                        # --- Async path for AI-routed skills ---
                        if USE_ASYNC_SKILLS and event_bus and pending_store:
                            return self._handle_async_skill(
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

                        # --- Sync fallback ---
                        request_id = f"ai-{conversation_id}"
                        _run_async(
                            bus.publish_skill_invocation(
                                tenant_id,
                                tc.tool_name,
                                tc.tool_params,
                                conversation_id,
                                request_id,
                                "dashboard",
                                "user",
                            )
                        )
                        skill_result = bus.get_result(request_id) or {"error": "No result"}

                        if is_raw and rule_router.supports_raw(tc.tool_name):
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
                            final = _run_async(
                                ai.chat_with_tool_result(
                                    active_model,
                                    system,
                                    messages_with_tool,
                                    tools,
                                    tc.tool_use_id,
                                    skill_result,
                                )
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

            stats["total_tokens"] += total_tokens
            roundtrip_sec = round(time.time() - request_start, 1)
            chat_metadata: dict = {
                "route": route_type,
                "model": model_short_name,
                "tokens": total_tokens,
                "timestamp": int(request_start * 1000),
                "roundtrip_sec": roundtrip_sec,
            }
            if user_email:
                chat_metadata["user_email"] = user_email
            if not is_raw_response:
                _run_async(
                    memory.save_turn(
                        tenant_id,
                        conversation_id,
                        clean_text,
                        assistant_text,
                        metadata=chat_metadata,
                    )
                )

            self._json_response(
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
            self._json_response(
                {
                    "error": friendly.message,
                    **friendly.to_dict(),
                },
                500,
            )

    def _handle_async_skill(
        self,
        tenant_id,
        user_email,
        skill_name,
        params,
        conversation_id,
        user_message,
        is_raw,
        route_type,
        model_id="",
        model_short_name="",
    ):
        """
        Publish a skill invocation to EventBridge and return immediately.
        The result will arrive later via WebSocket (or SSE).
        """
        import uuid

        request_id = f"async-{uuid.uuid4().hex[:12]}"
        user_key = user_email or "anonymous"

        # Create pending request in DynamoDB
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
        pending_store.create(pending_req)

        # Publish to EventBridge (returns immediately)
        _run_async(
            event_bus.publish_skill_invocation(
                tenant_id,
                skill_name,
                params,
                conversation_id,
                request_id,
                "dashboard",
                user_key,
            )
        )

        stats[f"{route_type}_routed"] += 1
        logger.info(
            f"Chat: async skill '{skill_name}' dispatched, "
            f"request={request_id[:8]}, user={user_key}"
        )

        # Return immediately — client will receive result via WebSocket/SSE
        self._json_response(
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

    def _handle_async_channel_skill(
        self,
        tenant_id,
        channel,
        skill_name,
        params,
        conversation_id,
        reply_target,
        user_key,
        user_message,
        is_raw,
        route_type,
        model_id="",
        model_short_name="",
        service_url="",
    ):
        """
        Publish a skill invocation to EventBridge for Teams/Telegram channels.
        Returns immediately — the result will be delivered by AsyncResultRouter
        when SQS picks it up.
        """
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
        pending_store.create(pending_req)

        _run_async(
            event_bus.publish_skill_invocation(
                tenant_id,
                skill_name,
                params,
                conversation_id,
                request_id,
                channel,
                user_key,
            )
        )

        stats[f"{route_type}_routed"] += 1
        logger.info(
            f"{channel.capitalize()}: async skill '{skill_name}' dispatched, "
            f"request={request_id[:8]}"
        )

    # --- Invitation endpoints (public) ---

    def _handle_invitation_validate(self):
        """Public: validate an invite code, return tenant name + email."""
        try:
            from urllib.parse import parse_qs

            query = parse_qs(urlparse(self.path).query)
            code = query.get("code", [""])[0]
            if not code:
                self._json_response({"error": "Missing code parameter"}, 400)
                return

            invitation = _run_async(tenants.get_invitation(code))
            if not invitation or not invitation.is_valid():
                self._json_response({"error": "Invalid or expired invitation"}, 404)
                return

            try:
                tenant = _run_async(tenants.get_tenant(invitation.tenant_id))
                tenant_name = tenant.name
            except Exception:
                tenant_name = invitation.tenant_id

            self._json_response(
                {
                    "valid": True,
                    "tenant_name": tenant_name,
                    "tenant_id": invitation.tenant_id,
                    "email": invitation.email,
                    "role": invitation.role,
                }
            )
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_invitation_accept(self):
        """Accept an invitation — requires JWT, links user to tenant."""
        try:
            auth = extract_auth(self.headers)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            invite_code = body.get("invite_code", "")

            if not invite_code:
                self._json_response({"error": "invite_code is required"}, 400)
                return

            invitation = _run_async(tenants.get_invitation(invite_code))
            if not invitation or not invitation.is_valid():
                self._json_response({"error": "Invalid or expired invitation"}, 404)
                return

            # Email from JWT must match invitation email
            if auth.email.lower() != invitation.email.lower():
                self._json_response({"error": "Email does not match invitation"}, 403)
                return

            # Check if user is already a member
            existing = _run_async(tenants.get_user_by_email(invitation.tenant_id, invitation.email))
            if existing:
                invitation.status = "accepted"
                invitation.accepted_at = datetime.now(timezone.utc).isoformat()
                _run_async(tenants.update_invitation(invitation))
                self._json_response(
                    {
                        "accepted": True,
                        "tenant_id": invitation.tenant_id,
                        "already_member": True,
                    }
                )
                return

            # Create TenantUser
            from agent.models.tenant import TenantUser

            user = TenantUser(
                user_id=auth.user_id,  # Cognito sub
                tenant_id=invitation.tenant_id,
                email=invitation.email,
                display_name=invitation.email.split("@")[0],
                role=invitation.role,
                cognito_sub=auth.user_id,
            )
            _run_async(tenants.create_user(user))

            # Mark invitation as accepted
            invitation.status = "accepted"
            invitation.accepted_at = datetime.now(timezone.utc).isoformat()
            _run_async(tenants.update_invitation(invitation))

            self._json_response(
                {
                    "accepted": True,
                    "tenant_id": invitation.tenant_id,
                    "user_id": auth.user_id,
                    "role": invitation.role,
                }
            )
        except AuthError as e:
            self._json_response({"error": e.message}, e.status)
        except Exception as e:
            logger.exception("Invitation accept error")
            self._json_response({"error": str(e)}, 500)

    def _handle_teams_webhook(self):
        """Handle incoming Microsoft Teams webhook (Bot Framework Activity).

        This is the main entry point for Teams messages. Microsoft sends
        HTTP POST with a Bot Framework Activity JSON payload and a JWT
        in the Authorization header.
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0) or 0)
            body_bytes = self.rfile.read(content_length)
            activity = json.loads(body_bytes) if body_bytes else {}

            activity_type = activity.get("type", "")
            logger.info(f"Teams webhook: type={activity_type}")

            # Get Teams integration credentials to validate + respond
            # Resolve tenant from the bot's app ID (recipient.id)
            recipient_id = activity.get("recipient", {}).get("id", "")
            teams_adapter = self._get_teams_adapter(recipient_id)

            if not teams_adapter:
                logger.warning(f"No Teams adapter for recipient {recipient_id}")
                self._json_response({"error": "Bot not configured"}, 401)
                return

            # Validate webhook authenticity
            auth_header = self.headers.get("Authorization", "")
            if auth_header and not teams_adapter.validate_webhook(dict(self.headers), body_bytes):
                logger.warning("Teams webhook JWT validation failed")
                self._json_response({"error": "Unauthorized"}, 401)
                return

            # Handle different activity types
            if activity_type == "message" and TeamsAdapter.is_message_activity(activity):
                self._handle_teams_message(teams_adapter, activity)
            elif TeamsAdapter.is_bot_added(activity):
                # Bot was added to a team/chat — send welcome message
                self._handle_teams_bot_added(teams_adapter, activity)
            else:
                logger.debug(f"Ignoring Teams activity type: {activity_type}")

            # Always return 200 OK quickly — Teams expects fast responses
            self._json_response({"ok": True})

        except Exception as e:
            logger.exception("Teams webhook error")
            self._json_response({"error": str(e)}, 500)

    def _handle_teams_message(self, teams_adapter: TeamsAdapter, activity: dict):
        """Process a Teams message through the T3nets router."""
        from agent.models.message import OutboundMessage

        message = teams_adapter.parse_inbound(activity)
        text = message.text
        if not text:
            return

        # Resolve tenant from channel mapping
        recipient_id = activity.get("recipient", {}).get("id", "")
        try:
            tenant = _run_async(tenants.get_by_channel_id("teams", recipient_id))
        except Exception:
            logger.warning(f"No tenant mapped for Teams bot {recipient_id}")
            return

        tenant_id = tenant.tenant_id
        conversation_id = f"teams-{message.conversation_id}"

        logger.info(f"Teams [{tenant_id}]: {text[:100]}")

        # Send typing indicator while processing
        _run_async(teams_adapter.send_typing_indicator(message.conversation_id))

        # Process through the same routing pipeline as dashboard
        clean_text, is_raw = strip_raw_flag(text)
        active_model, model_short_name = _resolve_model(tenant)

        system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
You are communicating via Microsoft Teams. Keep responses clear and well-formatted.
When you have data to present, format it clearly with structure."""

        history = _strip_metadata(_run_async(memory.get_conversation(tenant_id, conversation_id)))

        if not is_raw and rule_router.is_conversational(clean_text):
            stats["conversational"] += 1
            messages = history + [{"role": "user", "content": clean_text}]
            response = _run_async(ai.chat(active_model, system, messages, []))
            assistant_text = response.text or "Hey! How can I help?"
            total_tokens = response.input_tokens + response.output_tokens
        else:
            match = rule_router.match(clean_text, tenant.settings.enabled_skills)

            if match:
                # --- Async path: publish to EventBridge, return immediately ---
                if USE_ASYNC_SKILLS and event_bus and pending_store:
                    service_url = teams_adapter._service_urls.get(message.conversation_id, "")
                    self._handle_async_channel_skill(
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

                # --- Sync fallback: DirectBus ---
                request_id = f"teams-rule-{conversation_id}"
                _run_async(
                    bus.publish_skill_invocation(
                        tenant_id,
                        match.skill_name,
                        match.params,
                        conversation_id,
                        request_id,
                        "teams",
                        message.channel_user_id,
                    )
                )
                skill_result = bus.get_result(request_id) or {"error": "No result"}

                if is_raw and rule_router.supports_raw(match.skill_name):
                    stats["raw"] += 1
                    stats["rule_routed"] += 1
                    assistant_text = _format_raw_json(skill_result)
                    total_tokens = 0
                else:
                    stats["rule_routed"] += 1
                    prompt = (
                        f'{system}\n\nThe user asked: "{clean_text}"\n\n'
                        f"Tool data:\n{json.dumps(skill_result, indent=2)}\n\n"
                        f"Format this clearly."
                    )
                    messages = history + [{"role": "user", "content": prompt}]
                    response = _run_async(ai.chat(active_model, system, messages, []))
                    assistant_text = response.text or "Got data but couldn't format."
                    total_tokens = response.input_tokens + response.output_tokens
            else:
                stats["ai_routed"] += 1
                tools = skills.get_tools_for_tenant(type("C", (), {"tenant": tenant})())
                messages = history + [{"role": "user", "content": clean_text}]
                response = _run_async(ai.chat(active_model, system, messages, tools))

                if response.has_tool_use:
                    tc = response.tool_calls[0]

                    # --- Async path for AI-routed skills ---
                    if USE_ASYNC_SKILLS and event_bus and pending_store:
                        service_url = teams_adapter._service_urls.get(message.conversation_id, "")
                        self._handle_async_channel_skill(
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

                    # --- Sync fallback ---
                    request_id = f"teams-ai-{conversation_id}"
                    _run_async(
                        bus.publish_skill_invocation(
                            tenant_id,
                            tc.tool_name,
                            tc.tool_params,
                            conversation_id,
                            request_id,
                            "teams",
                            message.channel_user_id,
                        )
                    )
                    skill_result = bus.get_result(request_id) or {"error": "No result"}

                    if is_raw and rule_router.supports_raw(tc.tool_name):
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
                        final = _run_async(
                            ai.chat_with_tool_result(
                                active_model,
                                system,
                                messages_with_tool,
                                tools,
                                tc.tool_use_id,
                                skill_result,
                            )
                        )
                        assistant_text = final.text or "Got data but couldn't format."
                        total_tokens = (
                            response.input_tokens
                            + response.output_tokens
                            + final.input_tokens
                            + final.output_tokens
                        )
                else:
                    assistant_text = response.text or "Not sure how to help."
                    total_tokens = response.input_tokens + response.output_tokens

        stats["total_tokens"] += total_tokens

        # Save conversation turn
        _run_async(
            memory.save_turn(
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
        )

        # Send response back to Teams
        outbound = OutboundMessage(
            channel=ChannelType.TEAMS,
            conversation_id=message.conversation_id,
            recipient_id=message.channel_user_id,
            text=assistant_text,
        )
        _run_async(teams_adapter.send_response(outbound))

    def _handle_teams_bot_added(self, teams_adapter: TeamsAdapter, activity: dict):
        """Handle bot being added to a Teams channel/chat."""
        from agent.models.message import OutboundMessage

        conversation_id = activity.get("conversation", {}).get("id", "")
        if not conversation_id:
            return

        # Cache the serviceUrl
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
        _run_async(teams_adapter.send_response(welcome))

    def _get_teams_adapter(self, bot_app_id: str) -> TeamsAdapter | None:
        """Get or create a TeamsAdapter for the given bot app ID.

        Looks up Teams integration credentials from Secrets Manager.
        """
        # Try to resolve tenant from channel mapping
        try:
            tenant = _run_async(tenants.get_by_channel_id("teams", bot_app_id))
        except Exception:
            # No channel mapping — try all tenants for Teams integration
            try:
                all_tenants = _run_async(tenants.list_tenants())
                for t in all_tenants:
                    try:
                        creds = _run_async(secrets.get(t.tenant_id, "teams"))
                        if creds.get("app_id") == bot_app_id:
                            tenant = t
                            # Create channel mapping for faster lookup next time
                            _run_async(
                                tenants.set_channel_mapping(t.tenant_id, "teams", bot_app_id)
                            )
                            break
                    except Exception:
                        continue
                else:
                    return None
            except Exception:
                return None

        # Load credentials and create adapter
        try:
            creds = _run_async(secrets.get(tenant.tenant_id, "teams"))
            app_id = creds.get("app_id", "")
            app_secret = creds.get("app_secret", "")
            if not app_id or not app_secret:
                logger.error(f"Incomplete Teams credentials for tenant {tenant.tenant_id}")
                return None
            return TeamsAdapter(app_id, app_secret)
        except Exception as e:
            logger.error(f"Failed to load Teams credentials: {e}")
            return None

    # --- Telegram webhook handlers ---

    def _handle_telegram_webhook(self):
        """Handle incoming Telegram webhook (Bot API Update)."""
        try:
            content_length = int(self.headers.get("Content-Length", 0) or 0)
            body_bytes = self.rfile.read(content_length)
            update = json.loads(body_bytes) if body_bytes else {}

            # Extract bot token from URL path: /api/channels/telegram/webhook/{token_hash}
            path = urlparse(self.path).path
            path_parts = path.rstrip("/").split("/")
            token_hash = path_parts[-1] if len(path_parts) > 5 else ""

            telegram_adapter = self._get_telegram_adapter(token_hash)
            if not telegram_adapter:
                logger.warning(f"No Telegram adapter for token hash {token_hash[:8]}...")
                self._json_response({"error": "Bot not configured"}, 401)
                return

            # Validate webhook secret
            if not telegram_adapter.validate_webhook(dict(self.headers), body_bytes):
                logger.warning("Telegram webhook secret validation failed")
                self._json_response({"error": "Unauthorized"}, 401)
                return

            if TelegramAdapter.is_message_update(update):
                self._handle_telegram_message(telegram_adapter, update)

            self._json_response({"ok": True})

        except Exception as e:
            logger.exception("Telegram webhook error")
            self._json_response({"error": str(e)}, 500)

    def _handle_telegram_message(self, adapter: TelegramAdapter, update: dict):
        """Process a Telegram message through the T3nets router."""
        from agent.models.message import OutboundMessage

        message = adapter.parse_inbound(update)
        text = message.text
        if not text:
            return

        # Resolve tenant — look up by token hash in channel mapping GSI
        import hashlib

        token_hash = hashlib.sha256(adapter.bot_token.encode()).hexdigest()[:16]
        try:
            tenant = _run_async(tenants.get_by_channel_id("telegram", token_hash))
        except Exception:
            logger.warning(f"No tenant mapped for Telegram bot {bot_id}")
            return

        tenant_id = tenant.tenant_id
        conversation_id = f"tg-{message.conversation_id}"

        logger.info(f"Telegram [{tenant_id}]: {text[:100]}")

        # Typing indicator
        _run_async(adapter.send_typing_indicator(message.conversation_id))

        clean_text, is_raw = strip_raw_flag(text)
        active_model, model_short_name = _resolve_model(tenant)

        system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
You are communicating via Telegram. Keep responses concise and well-formatted.
Use Markdown sparingly — Telegram supports *bold*, _italic_, and `code`."""

        history = _strip_metadata(_run_async(memory.get_conversation(tenant_id, conversation_id)))

        # Route through the same pipeline
        if not is_raw and rule_router.is_conversational(clean_text):
            stats["conversational"] += 1
            messages = history + [{"role": "user", "content": clean_text}]
            response = _run_async(ai.chat(active_model, system, messages, []))
            assistant_text = response.text or "Hey! How can I help?"
            total_tokens = response.input_tokens + response.output_tokens
        else:
            match = rule_router.match(clean_text, tenant.settings.enabled_skills)
            if match:
                # --- Async path: publish to EventBridge, return immediately ---
                if USE_ASYNC_SKILLS and event_bus and pending_store:
                    self._handle_async_channel_skill(
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

                # --- Sync fallback: DirectBus ---
                request_id = f"tg-rule-{conversation_id}"
                _run_async(
                    bus.publish_skill_invocation(
                        tenant_id,
                        match.skill_name,
                        match.params,
                        conversation_id,
                        request_id,
                        "telegram",
                        message.channel_user_id,
                    )
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
                        f"Tool data:\n{json.dumps(skill_result, indent=2)}\n\n"
                        f"Format this clearly and concisely for Telegram."
                    )
                    messages = history + [{"role": "user", "content": prompt}]
                    response = _run_async(ai.chat(active_model, system, messages, []))
                    assistant_text = response.text or "Got data but couldn't format."
                    total_tokens = response.input_tokens + response.output_tokens
            else:
                stats["ai_routed"] += 1
                tools = skills.get_tools_for_tenant(type("C", (), {"tenant": tenant})())
                messages = history + [{"role": "user", "content": clean_text}]
                response = _run_async(ai.chat(active_model, system, messages, tools))
                if response.has_tool_use:
                    tc = response.tool_calls[0]

                    # --- Async path for AI-routed skills ---
                    if USE_ASYNC_SKILLS and event_bus and pending_store:
                        self._handle_async_channel_skill(
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

                    # --- Sync fallback ---
                    request_id = f"tg-ai-{conversation_id}"
                    _run_async(
                        bus.publish_skill_invocation(
                            tenant_id,
                            tc.tool_name,
                            tc.tool_params,
                            conversation_id,
                            request_id,
                            "telegram",
                            message.channel_user_id,
                        )
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
                    final = _run_async(
                        ai.chat_with_tool_result(
                            active_model,
                            system,
                            messages_with_tool,
                            tools,
                            tc.tool_use_id,
                            skill_result,
                        )
                    )
                    assistant_text = final.text or "Got data but couldn't format."
                    total_tokens = (
                        response.input_tokens
                        + response.output_tokens
                        + final.input_tokens
                        + final.output_tokens
                    )
                else:
                    assistant_text = response.text or "Not sure how to help."
                    total_tokens = response.input_tokens + response.output_tokens

        stats["total_tokens"] += total_tokens
        _run_async(
            memory.save_turn(
                tenant_id,
                conversation_id,
                clean_text,
                assistant_text,
                metadata={"route": "telegram", "model": model_short_name, "tokens": total_tokens},
            )
        )

        outbound = OutboundMessage(
            channel=ChannelType.TELEGRAM,
            conversation_id=message.conversation_id,
            recipient_id=message.channel_user_id,
            text=assistant_text,
        )
        _run_async(adapter.send_response(outbound))

    def _get_telegram_adapter(self, token_hash: str) -> TelegramAdapter | None:
        """Get TelegramAdapter by looking up the channel mapping GSI."""
        if not token_hash or token_hash == "webhook":
            logger.warning("No token hash in Telegram webhook URL")
            return None

        # Fast path: GSI lookup by channel mapping (CHANNEL#telegram#{token_hash})
        try:
            tenant = _run_async(tenants.get_by_channel_id("telegram", token_hash))
            creds = _run_async(secrets.get(tenant.tenant_id, "telegram"))
            bot_token = creds.get("bot_token", "")
            if bot_token:
                webhook_secret = creds.get("webhook_secret", "")
                return TelegramAdapter(bot_token, webhook_secret)
        except Exception as e:
            logger.warning(f"Telegram channel mapping lookup failed: {e}")
        return None

    def _handle_clear(self):
        try:
            tenant_id, _ = _get_auth_info(self.headers)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            cid = body.get("conversation_id", "default")
            _run_async(memory.clear_conversation(tenant_id, cid))
            self._json_response({"cleared": True})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    # _serve_file, _json_response, do_OPTIONS, log_message inherited from BaseHandler


def init():
    global ai, memory, tenants, secrets, skills, bus, event_bus, pending_store
    global sqs_poller, result_router, rule_router, admin_api, platform_api, error_handler, started_at

    started_at = time.time()

    region = AWS_REGION
    conversations_table = os.getenv("DYNAMODB_CONVERSATIONS_TABLE")
    tenants_table = os.getenv("DYNAMODB_TENANTS_TABLE")
    secrets_prefix = os.getenv("SECRETS_PREFIX")

    if not all([conversations_table, tenants_table, secrets_prefix]):
        logger.error(
            "Missing required env vars: DYNAMODB_CONVERSATIONS_TABLE, DYNAMODB_TENANTS_TABLE, SECRETS_PREFIX"
        )
        sys.exit(1)

    ai = BedrockProvider(region=region, model_id=BEDROCK_MODEL_ID)
    memory = DynamoDBConversationStore(conversations_table, region=region)
    tenants = DynamoDBTenantStore(tenants_table, region=region)
    secrets = SecretsManagerProvider(secrets_prefix, region=region)

    skills_obj = SkillRegistry()
    skills_dir = Path(__file__).parent.parent.parent / "agent" / "skills"
    skills_obj.load_from_directory(skills_dir)
    skills = skills_obj
    logger.info(f"Loaded skills: {skills.list_skill_names()}")

    rule_router = RuleBasedRouter(skills, confidence_threshold=0.5)
    bus = DirectBus(skills, secrets)  # Sync fallback, always initialized
    admin_api = AdminAPI(tenants, secrets, skills)
    platform_api = PlatformAPI(tenants, secrets, skills)
    error_handler = ErrorHandler()

    # --- Phase 3b: Async skill execution ---
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

    # Seed default tenant if it doesn't exist
    try:
        _run_async(tenants.get_tenant(DEFAULT_TENANT))
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
        _run_async(tenants.create_tenant(tenant))
        logger.info(f"Seeded tenant '{DEFAULT_TENANT}'")

    connected = _run_async(secrets.list_integrations(DEFAULT_TENANT))
    logger.info(f"Connected integrations: {connected}")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in a new thread.

    Required for SSE: long-lived SSE connections would block a single-threaded
    server from handling other requests.
    """

    daemon_threads = True


def main():
    init()

    # Start SSE keepalive only when using SSE transport (not WebSocket)
    if sse_manager is not None:
        start_keepalive_thread(sse_manager)

    port = int(os.getenv("PORT", "8080"))
    server = ThreadedHTTPServer(("0.0.0.0", port), AWSHandler)

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

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info(f"Stats: {json.dumps(stats)}")
        server.server_close()


if __name__ == "__main__":
    main()
