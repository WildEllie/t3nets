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
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.interfaces.ai_provider import ToolDefinition
from agent.skills.registry import SkillRegistry
from agent.channels.base import ChannelRegistry
from agent.channels.dashboard import DashboardAdapter
from agent.models.message import ChannelType
from agent.router.rule_router import RuleBasedRouter, strip_raw_flag
from adapters.local.anthropic_provider import AnthropicProvider
from adapters.local.sqlite_store import SQLiteConversationStore
from adapters.local.sqlite_tenant_store import SQLiteTenantStore
from adapters.local.env_secrets import EnvSecretsProvider
from adapters.local.direct_bus import DirectBus
from agent.errors.handler import ErrorHandler
from agent.models.tenant import Invitation
from agent.channels.teams import TeamsAdapter
from agent.models.message import ChannelType
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

# Integration field schemas — defines the config form per integration type.
# Used by GET /api/integrations to tell the frontend which fields to render.
INTEGRATION_SCHEMAS: dict = {
    "jira": {
        "label": "Jira",
        "fields": [
            {
                "key": "url",
                "label": "Jira URL",
                "type": "url",
                "required": True,
                "placeholder": "https://yourteam.atlassian.net",
            },
            {
                "key": "email",
                "label": "Email",
                "type": "email",
                "required": True,
                "placeholder": "admin@company.com",
            },
            {
                "key": "api_token",
                "label": "API Token",
                "type": "password",
                "required": True,
                "placeholder": "Your Jira API token",
            },
            {
                "key": "project_key",
                "label": "Project Key",
                "type": "text",
                "required": True,
                "placeholder": "PROJ",
            },
            {
                "key": "board_id",
                "label": "Board ID",
                "type": "text",
                "required": False,
                "placeholder": "Optional — for sprint queries",
            },
        ],
    },
    "github": {
        "label": "GitHub",
        "fields": [
            {
                "key": "token",
                "label": "Personal Access Token",
                "type": "password",
                "required": True,
                "placeholder": "ghp_...",
            },
            {
                "key": "org",
                "label": "Organization",
                "type": "text",
                "required": True,
                "placeholder": "your-org",
            },
        ],
    },
    "teams": {
        "label": "Microsoft Teams",
        "fields": [
            {
                "key": "app_id",
                "label": "Bot App ID",
                "type": "text",
                "required": True,
                "placeholder": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            },
            {
                "key": "app_secret",
                "label": "Bot App Secret",
                "type": "password",
                "required": True,
                "placeholder": "Your bot client secret",
            },
            {
                "key": "azure_tenant_id",
                "label": "Azure AD Tenant ID",
                "type": "text",
                "required": False,
                "placeholder": "Leave blank for multi-tenant bots",
            },
        ],
    },
}

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


def _run_async(coro):
    """Run async code from sync context."""
    return asyncio.run(coro)


def _format_raw_json(data: dict) -> str:
    """Format raw JSON for dashboard display."""
    return json.dumps(data, indent=2, default=str)


def _resolve_model(tenant):
    """Resolve the tenant's ai_model setting to an Anthropic API model ID and short name."""
    model_id = tenant.settings.ai_model or DEFAULT_MODEL_ID
    model = get_model(model_id)
    if not model:
        # Unknown model in tenant settings — fall back to registry default
        logger.warning(f"Unknown model '{model_id}', falling back to {DEFAULT_MODEL_ID}")
        model_id = DEFAULT_MODEL_ID
        model = get_model(model_id)
    api_id = get_model_for_provider(model_id, PROVIDER)
    return api_id or model.anthropic_id, model.short_name


def _strip_metadata(messages: list[dict]) -> list[dict]:
    """Strip metadata from conversation history before sending to the AI provider."""
    return [{"role": m["role"], "content": m["content"]} for m in messages]


def _uptime_human(seconds: float) -> str:
    """Convert seconds to human-readable uptime."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    elif s < 3600:
        return f"{s // 60}m {s % 60}s"
    elif s < 86400:
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h {m}m"
    else:
        d = s // 86400
        h = (s % 86400) // 3600
        return f"{d}d {h}h"


class DevHandler(BaseHTTPRequestHandler):
    """HTTP request handler for local development."""

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "/chat":
            self._serve_file("chat.html")
        elif path == "/health":
            self._serve_file("health.html")
        elif path == "/settings":
            self._serve_file("settings.html")
        elif path == "/onboard":
            self._serve_file("onboard.html")
        elif path == "/api/health":
            self._handle_health_api()
        elif path == "/api/settings":
            self._handle_settings_get()
        elif path == "/api/history":
            self._handle_history()
        elif path == "/api/auth/config":
            self._handle_auth_config()
        elif path == "/api/auth/me":
            self._handle_auth_me()
        elif path == "/api/integrations":
            self._handle_integrations_list()
        elif path.startswith("/api/integrations/"):
            self._handle_integration_get(path)
        elif path == "/api/invitations/validate":
            self._handle_invitation_validate()
        elif path.startswith("/api/admin/tenants/") and "/invitations" in path:
            self._handle_admin_invitations_list(path)
        elif path.startswith("/api/admin/tenants/") and "/users" in path:
            self._handle_admin_list_users(path)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/channels/teams/webhook":
            self._handle_teams_webhook()
        elif path == "/api/chat":
            self._handle_chat()
        elif path == "/api/clear":
            self._handle_clear()
        elif path == "/api/settings":
            self._handle_settings_post()
        elif path.startswith("/api/integrations/"):
            self._handle_integrations_post(path)
        elif path == "/api/admin/tenants":
            self._handle_create_tenant()
        elif path == "/api/invitations/accept":
            self._handle_invitation_accept()
        elif path.startswith("/api/admin/tenants/") and "/invitations" in path:
            self._handle_admin_create_invitation(path)
        else:
            self.send_error(404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/admin/tenants/") and "/invitations/" in path:
            self._handle_admin_revoke_invitation(path)
        else:
            self.send_error(404)

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith("/api/admin/tenants/"):
            self._handle_update_tenant(path)
        else:
            self.send_error(404)

    def do_PATCH(self):
        path = urlparse(self.path).path
        if path.startswith("/api/admin/tenants/") and path.endswith("/activate"):
            self._handle_activate_tenant(path)
        else:
            self.send_error(404)

    def _handle_health_api(self):
        """Rich health/status JSON endpoint."""
        try:
            uptime_secs = time.time() - started_at
            tenant = _run_async(tenants.get_tenant(DEFAULT_TENANT))
            connected_integrations = _run_async(secrets.list_integrations(DEFAULT_TENANT))

            # Build integration status
            all_integrations = {
                "jira": {"connected": "jira" in connected_integrations},
                "github": {"connected": "github" in connected_integrations},
                "teams": {"connected": "teams" in connected_integrations},
                "twilio": {"connected": "twilio" in connected_integrations},
            }

            # Build skills info
            skills_info = []
            for skill in skills.list_skills():
                skills_info.append({
                    "name": skill.name,
                    "description": skill.description.strip()[:120],
                    "requires_integration": skill.requires_integration,
                    "supports_raw": skill.supports_raw,
                    "triggers": skill.triggers[:8],
                })

            # API key preview (show first 8 + last 4 chars)
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
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
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
            }

            self._json_response(health)

        except Exception as e:
            logger.exception("Health check error")
            self._json_response({
                "status": "error",
                "error": str(e),
            }, 500)

    def _handle_auth_config(self):
        """Return auth config — always disabled for local dev."""
        self._json_response({
            "enabled": False,
            "client_id": "",
            "auth_domain": "",
            "user_pool_id": "",
        })

    def _handle_auth_me(self):
        """Return current user info — local dev always returns the default tenant."""
        tenant = _run_async(tenants.get_tenant(DEFAULT_TENANT))
        self._json_response({
            "authenticated": True,
            "user_id": "local-admin",
            "tenant_id": DEFAULT_TENANT,
            "email": "admin@local.dev",
            "tenant_status": tenant.status,
            "tenant_name": tenant.name,
        })

    def _handle_integrations_list(self):
        """GET /api/integrations — list all integrations with status and field schemas."""
        try:
            connected = _run_async(secrets.list_integrations(DEFAULT_TENANT))
            result = []
            for name, schema in INTEGRATION_SCHEMAS.items():
                result.append({
                    "name": name,
                    "label": schema["label"],
                    "connected": name in connected,
                    "fields": schema["fields"],
                })
            self._json_response(result)
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_integration_get(self, path: str):
        """GET /api/integrations/{name} — return current config with sensitive fields masked."""
        try:
            integration_name = path.rstrip("/").split("/")[-1]
            if integration_name not in INTEGRATION_SCHEMAS:
                self._json_response({"error": f"Unknown integration: {integration_name}"}, 404)
                return

            schema = INTEGRATION_SCHEMAS[integration_name]
            connected = False
            config = {}
            try:
                stored = _run_async(secrets.get(DEFAULT_TENANT, integration_name))
                connected = True
                # Mask password-type fields
                password_keys = {
                    f["key"] for f in schema["fields"] if f["type"] == "password"
                }
                for key, value in stored.items():
                    if key in password_keys and value:
                        config[key] = "\u2022" * 8  # ••••••••
                    else:
                        config[key] = value
            except Exception:
                pass  # Not connected — return empty config

            self._json_response({
                "name": integration_name,
                "label": schema["label"],
                "connected": connected,
                "config": config,
                "fields": schema["fields"],
            })
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_integrations_post(self, path: str):
        """Handle POST /api/integrations/{name} and /api/integrations/{name}/test."""
        try:
            parts = path.rstrip("/").split("/")
            is_test = parts[-1] == "test"
            integration_name = parts[-2] if is_test else parts[-1]

            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            tenant_id = body.get("tenant_id") or DEFAULT_TENANT

            if is_test:
                result = self._test_integration(integration_name, body)
                self._json_response(result, 200 if result.get("ok") else 400)
            else:
                _run_async(secrets.put(tenant_id, integration_name, body))
                logger.info(f"Stored {integration_name} credentials for tenant {tenant_id}")
                self._json_response({"ok": True})
        except Exception as e:
            logger.exception("Integration endpoint error")
            self._json_response({"error": str(e)}, 500)

    def _test_integration(self, name: str, creds: dict) -> dict:
        """Test integration credentials."""
        if name == "jira":
            return self._test_jira(creds)
        return {"ok": False, "error": f"Testing not supported for '{name}'"}

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

    def _handle_create_tenant(self):
        """Handle POST /api/admin/tenants for local dev."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            tenant_id = body.get("tenant_id", "").strip()
            name = body.get("name", "").strip()

            if not tenant_id or not name:
                self._json_response({"error": "tenant_id and name are required"}, 400)
                return

            from agent.models.tenant import Tenant, TenantSettings, TenantUser

            now = datetime.now(timezone.utc).isoformat()
            status = body.get("status", "active")
            tenant = Tenant(
                tenant_id=tenant_id,
                name=name,
                status=status,
                created_at=now,
                settings=TenantSettings(
                    enabled_skills=skills.list_skill_names(),
                ),
            )
            _run_async(tenants.create_tenant(tenant))
            logger.info(f"Created tenant: {tenant_id} ({name})")

            # Create admin user if provided
            admin_data = body.get("admin_user")
            if admin_data:
                user = TenantUser(
                    user_id=admin_data.get("cognito_sub", f"admin-{tenant_id}"),
                    tenant_id=tenant_id,
                    email=admin_data.get("email", "admin@local.dev"),
                    display_name=admin_data.get("display_name", "Admin"),
                    role="admin",
                )
                _run_async(tenants.create_user(user))

            self._json_response({"tenant_id": tenant_id, "created": True}, 201)
        except Exception as e:
            logger.exception("Create tenant error")
            self._json_response({"error": str(e)}, 500)

    def _handle_update_tenant(self, path: str):
        """Handle PUT /api/admin/tenants/{id} for local dev."""
        try:
            tenant_id = path.split("/")[-1]
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            tenant = _run_async(tenants.get_tenant(tenant_id))

            if "name" in body:
                tenant.name = body["name"]
            if "status" in body:
                tenant.status = body["status"]
            if "ai_model" in body:
                tenant.settings.ai_model = body["ai_model"]

            _run_async(tenants.update_tenant(tenant))
            self._json_response({"tenant_id": tenant_id, "updated": True})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_activate_tenant(self, path: str):
        """Handle PATCH /api/admin/tenants/{id}/activate for local dev."""
        try:
            parts = path.rstrip("/").split("/")
            tenant_id = parts[-2]
            tenant = _run_async(tenants.get_tenant(tenant_id))
            tenant.status = "active"
            _run_async(tenants.update_tenant(tenant))
            self._json_response({"tenant_id": tenant_id, "status": "active", "activated": True})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_settings_get(self):
        """Return current settings and available models."""
        try:
            tenant = _run_async(tenants.get_tenant(DEFAULT_TENANT))
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
            connected_integrations = _run_async(secrets.list_integrations(DEFAULT_TENANT))

            self._json_response({
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
            self._json_response({"error": str(e)}, 500)

    def _handle_history(self):
        """Return conversation history for the default conversation."""
        try:
            history = _run_async(
                memory.get_conversation(DEFAULT_TENANT, DEFAULT_CONVERSATION)
            )
            self._json_response({
                "messages": history,
                "platform": os.getenv("T3NETS_PLATFORM", "local"),
                "stage": os.getenv("T3NETS_STAGE", "dev"),
            })
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_settings_post(self):
        """Update tenant settings."""
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            tenant = _run_async(tenants.get_tenant(DEFAULT_TENANT))
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
                # Validate against registered skills
                known = set(skills.list_skill_names())
                unknown = [s for s in skill_list if s not in known]
                if unknown:
                    self._json_response(
                        {"error": f"Unknown skills: {', '.join(unknown)}"}, 400
                    )
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
                    self._json_response(
                        {"error": "max_tokens_per_message must be 256-16384"}, 400
                    )
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
                    self._json_response(
                        {"error": "max_conversation_history must be 1-100"}, 400
                    )
                    return
                tenant.settings.max_conversation_history = val
                changed = True

            if changed:
                _run_async(tenants.update_tenant(tenant))

            self._json_response({"ok": True})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_chat(self):
        """Handle a chat message with hybrid routing."""
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = body.get("text", "").strip()

            if not text:
                self._json_response({"error": "Empty message"}, 400)
                return

            conversation_id = body.get("conversation_id", DEFAULT_CONVERSATION)

            # Extract user email from JWT (if present) for message attribution
            user_email = ""
            auth_header = self.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                try:
                    import base64
                    payload_b64 = auth_header[7:].split(".")[1]
                    padding = 4 - len(payload_b64) % 4
                    if padding != 4:
                        payload_b64 += "=" * padding
                    claims = json.loads(base64.urlsafe_b64decode(payload_b64))
                    user_email = claims.get("email", "")
                except Exception:
                    pass

            # Check for --raw flag
            clean_text, is_raw = strip_raw_flag(text)

            logger.info(f"Chat: {text[:100]}" + (" [RAW]" if is_raw else ""))

            is_raw_response = False

            # Load conversation history (strip metadata before sending to AI)
            history = _strip_metadata(_run_async(
                memory.get_conversation(DEFAULT_TENANT, conversation_id)
            ))

            # Get tenant and resolve model
            tenant = _run_async(tenants.get_tenant(DEFAULT_TENANT))
            active_model, model_short_name = _resolve_model(tenant)

            system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
When you have data to present, format it clearly with structure."""

            # === TIER 0: Conversational (no tools, cheap) ===
            if not is_raw and rule_router.is_conversational(clean_text):
                logger.info("Route: CONVERSATIONAL (no tools)")
                stats["conversational"] += 1

                messages = history + [{"role": "user", "content": clean_text}]
                response = _run_async(ai.chat(
                    model=active_model,
                    system=system,
                    messages=messages,
                    tools=[],
                ))

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

                    # Execute skill
                    request_id = f"rule-{conversation_id}"
                    _run_async(bus.publish_skill_invocation(
                        tenant_id=DEFAULT_TENANT,
                        skill_name=match.skill_name,
                        params=match.params,
                        session_id=conversation_id,
                        request_id=request_id,
                        reply_channel="dashboard",
                        reply_target="dashboard-user",
                    ))

                    skill_result = bus.get_result(request_id)
                    if not skill_result:
                        skill_result = {"error": "Skill returned no result"}

                    # === RAW MODE ===
                    if is_raw and rule_router.supports_raw(match.skill_name):
                        logger.info(f"Returning raw output for {match.skill_name}")
                        stats["raw"] += 1
                        stats["rule_routed"] += 1

                        assistant_text = _format_raw_json(skill_result)
                        total_tokens = 0
                        route_type = "rule"
                        is_raw_response = True

                    # === NORMAL: Claude formats ===
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
                        response = _run_async(ai.chat(
                            model=active_model,
                            system=system,
                            messages=messages,
                            tools=[],
                        ))

                        assistant_text = response.text or "Got the data but couldn't format it."
                        total_tokens = response.input_tokens + response.output_tokens
                        route_type = "rule"

                # === TIER 2: Full Claude routing ===
                else:
                    if is_raw:
                        logger.info("--raw flag ignored: no rule match, using AI routing")

                    logger.info("Route: AI (full Claude with tools)")
                    stats["ai_routed"] += 1

                    tools = skills.get_tools_for_tenant(
                        type("Ctx", (), {"tenant": tenant})()
                    )

                    messages = history + [{"role": "user", "content": clean_text}]
                    response = _run_async(ai.chat(
                        model=active_model,
                        system=system,
                        messages=messages,
                        tools=tools,
                    ))

                    if response.has_tool_use:
                        tool_call = response.tool_calls[0]
                        logger.info(f"AI chose skill: {tool_call.tool_name}")

                        request_id = f"ai-{conversation_id}"
                        _run_async(bus.publish_skill_invocation(
                            tenant_id=DEFAULT_TENANT,
                            skill_name=tool_call.tool_name,
                            params=tool_call.tool_params,
                            session_id=conversation_id,
                            request_id=request_id,
                            reply_channel="dashboard",
                            reply_target="dashboard-user",
                        ))

                        skill_result = bus.get_result(request_id)
                        if not skill_result:
                            skill_result = {"error": "Skill returned no result"}

                        # --raw via AI route
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

                            final_response = _run_async(ai.chat_with_tool_result(
                                model=active_model,
                                system=system,
                                messages=messages_with_tool,
                                tools=tools,
                                tool_use_id=tool_call.tool_use_id,
                                tool_result=skill_result,
                            ))

                            assistant_text = final_response.text or "Got the data but couldn't format it."
                            total_tokens = (
                                response.input_tokens + response.output_tokens +
                                final_response.input_tokens + final_response.output_tokens
                            )
                            route_type = "ai"
                    else:
                        assistant_text = response.text or "I'm not sure how to help with that."
                        total_tokens = response.input_tokens + response.output_tokens
                        route_type = "ai"

            # Track tokens
            stats["total_tokens"] += total_tokens

            # Save conversation (don't save raw output to history)
            chat_metadata: dict = {
                "route": route_type,
                "model": model_short_name,
                "tokens": total_tokens,
            }
            if user_email:
                chat_metadata["user_email"] = user_email
            if not is_raw_response:
                _run_async(memory.save_turn(
                    DEFAULT_TENANT, conversation_id, clean_text, assistant_text,
                    metadata=chat_metadata,
                ))

            self._json_response({
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
            self._json_response({
                "error": friendly.message,
                **friendly.to_dict(),
            }, 500)

    # --- Invitation endpoints ---

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

            # Look up tenant name
            try:
                tenant = _run_async(tenants.get_tenant(invitation.tenant_id))
                tenant_name = tenant.name
            except Exception:
                tenant_name = invitation.tenant_id

            self._json_response({
                "valid": True,
                "tenant_name": tenant_name,
                "tenant_id": invitation.tenant_id,
                "email": invitation.email,
                "role": invitation.role,
            })
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_invitation_accept(self):
        """Accept an invitation — link user to tenant."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            invite_code = body.get("invite_code", "")
            # In local mode, accept email + display_name from body
            email = body.get("email", "")
            display_name = body.get("display_name", email.split("@")[0] if email else "")
            cognito_sub = body.get("cognito_sub", "")

            if not invite_code:
                self._json_response({"error": "invite_code is required"}, 400)
                return

            invitation = _run_async(tenants.get_invitation(invite_code))
            if not invitation or not invitation.is_valid():
                self._json_response({"error": "Invalid or expired invitation"}, 404)
                return

            # Email must match
            if email and invitation.email.lower() != email.lower():
                self._json_response({"error": "Email does not match invitation"}, 403)
                return

            # Check if user is already a member
            existing = _run_async(tenants.get_user_by_email(
                invitation.tenant_id, invitation.email
            ))
            if existing:
                # Already a member — just mark invitation accepted
                invitation.status = "accepted"
                invitation.accepted_at = datetime.now(timezone.utc).isoformat()
                _run_async(tenants.update_invitation(invitation))
                self._json_response({
                    "accepted": True,
                    "tenant_id": invitation.tenant_id,
                    "already_member": True,
                })
                return

            # Create TenantUser
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
            _run_async(tenants.create_user(user))

            # Mark invitation as accepted
            invitation.status = "accepted"
            invitation.accepted_at = datetime.now(timezone.utc).isoformat()
            _run_async(tenants.update_invitation(invitation))

            self._json_response({
                "accepted": True,
                "tenant_id": invitation.tenant_id,
                "user_id": user_id,
                "role": invitation.role,
            })
        except Exception as e:
            logger.exception("Invitation accept error")
            self._json_response({"error": str(e)}, 500)

    def _handle_admin_create_invitation(self, path: str):
        """Admin: create an invitation for a tenant."""
        try:
            # Extract tenant_id from path: /api/admin/tenants/{id}/invitations
            parts = path.rstrip("/").split("/")
            tenant_id = parts[4]  # /api/admin/tenants/{id}/invitations

            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            email = body.get("email", "").strip().lower()
            role = body.get("role", "member")

            if not email:
                self._json_response({"error": "email is required"}, 400)
                return
            if role not in ("member", "admin"):
                self._json_response({"error": "role must be 'member' or 'admin'"}, 400)
                return

            # Verify tenant exists
            try:
                _run_async(tenants.get_tenant(tenant_id))
            except Exception:
                self._json_response({"error": f"Tenant '{tenant_id}' not found"}, 404)
                return

            # Check if already a member
            existing = _run_async(tenants.get_user_by_email(tenant_id, email))
            if existing:
                self._json_response({"error": f"{email} is already a member"}, 409)
                return

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
            _run_async(tenants.create_invitation(invitation))

            # Construct invite URL
            host = self.headers.get("Host", "localhost:8080")
            scheme = "http" if "localhost" in host else "https"
            invite_url = f"{scheme}://{host}/login?invite={invitation.invite_code}"

            self._json_response({
                "invite_code": invitation.invite_code,
                "invite_url": invite_url,
                "email": email,
                "role": role,
                "expires_at": invitation.expires_at,
            }, 201)
        except Exception as e:
            logger.exception("Create invitation error")
            self._json_response({"error": str(e)}, 500)

    def _handle_admin_invitations_list(self, path: str):
        """Admin: list pending invitations for a tenant."""
        try:
            parts = path.rstrip("/").split("/")
            tenant_id = parts[4]

            invitations = _run_async(tenants.list_invitations(tenant_id))
            self._json_response({
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
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_admin_revoke_invitation(self, path: str):
        """Admin: revoke an invitation."""
        try:
            # /api/admin/tenants/{id}/invitations/{code}
            parts = path.rstrip("/").split("/")
            invite_code = parts[-1]

            invitation = _run_async(tenants.get_invitation(invite_code))
            if not invitation:
                self._json_response({"error": "Invitation not found"}, 404)
                return

            invitation.status = "revoked"
            _run_async(tenants.update_invitation(invitation))
            self._json_response({"revoked": True, "invite_code": invite_code})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_admin_list_users(self, path: str):
        """Admin: list users for a tenant."""
        try:
            parts = path.rstrip("/").split("/")
            tenant_id = parts[4]

            users = _run_async(tenants.list_users(tenant_id))
            self._json_response({
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
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_teams_webhook(self):
        """Handle incoming Teams webhook for local development.

        For local testing with Bot Framework Emulator.
        JWT validation is relaxed (emulator may not send auth headers).
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0) or 0)
            body_bytes = self.rfile.read(content_length)
            activity = json.loads(body_bytes) if body_bytes else {}

            activity_type = activity.get("type", "")
            logger.info(f"Teams webhook (local): type={activity_type}")

            # For local dev, create adapter from env or stored secrets
            teams_adapter = self._get_teams_adapter_local()
            if not teams_adapter:
                # In local dev, allow without full config for emulator testing
                logger.warning("No Teams config — using mock adapter for emulator")
                teams_adapter = TeamsAdapter("local-test-app", "local-test-secret")

            if TeamsAdapter.is_message_activity(activity):
                self._handle_teams_message_local(teams_adapter, activity)
            elif TeamsAdapter.is_bot_added(activity):
                logger.info("Teams: bot added to conversation (local)")

            self._json_response({"ok": True})

        except Exception as e:
            logger.exception("Teams webhook error (local)")
            self._json_response({"error": str(e)}, 500)

    def _handle_teams_message_local(
        self, teams_adapter: TeamsAdapter, activity: dict
    ):
        """Process a Teams message through the local router."""
        from agent.models.message import OutboundMessage

        message = teams_adapter.parse_inbound(activity)
        text = message.text
        if not text:
            return

        tenant = _run_async(tenants.get_tenant(DEFAULT_TENANT))
        tenant_id = tenant.tenant_id
        conversation_id = f"teams-{message.conversation_id}"

        logger.info(f"Teams [{tenant_id}]: {text[:100]}")

        clean_text, is_raw = strip_raw_flag(text)
        active_model, model_short_name = _resolve_model(tenant)

        system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
You are communicating via Microsoft Teams. Keep responses clear and well-formatted.
When you have data to present, format it clearly with structure."""

        history = _strip_metadata(
            _run_async(memory.get_conversation(tenant_id, conversation_id))
        )

        # Route through the same pipeline as dashboard
        if not is_raw and rule_router.is_conversational(clean_text):
            stats["conversational"] += 1
            messages = history + [{"role": "user", "content": clean_text}]
            response = _run_async(ai.chat(active_model, system, messages, []))
            assistant_text = response.text or "Hey! How can I help?"
            total_tokens = response.input_tokens + response.output_tokens
        else:
            match = rule_router.match(clean_text, tenant.settings.enabled_skills)
            if match:
                request_id = f"teams-rule-{conversation_id}"
                _run_async(bus.publish_skill_invocation(
                    tenant_id, match.skill_name, match.params,
                    conversation_id, request_id, "teams", message.channel_user_id,
                ))
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
                        f'Format this clearly.'
                    )
                    messages = history + [{"role": "user", "content": prompt}]
                    response = _run_async(ai.chat(active_model, system, messages, []))
                    assistant_text = response.text or "Got data but couldn't format."
                    total_tokens = response.input_tokens + response.output_tokens
            else:
                stats["ai_routed"] += 1
                tools = skills.get_tools_for_tenant(
                    type("C", (), {"tenant": tenant})()
                )
                messages = history + [{"role": "user", "content": clean_text}]
                response = _run_async(ai.chat(active_model, system, messages, tools))
                if response.has_tool_use:
                    tc = response.tool_calls[0]
                    request_id = f"teams-ai-{conversation_id}"
                    _run_async(bus.publish_skill_invocation(
                        tenant_id, tc.tool_name, tc.tool_params,
                        conversation_id, request_id, "teams",
                        message.channel_user_id,
                    ))
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
                    final = _run_async(ai.chat_with_tool_result(
                        active_model, system, messages_with_tool,
                        tools, tc.tool_use_id, skill_result,
                    ))
                    assistant_text = final.text or "Got data but couldn't format."
                    total_tokens = (
                        response.input_tokens + response.output_tokens
                        + final.input_tokens + final.output_tokens
                    )
                else:
                    assistant_text = response.text or "Not sure how to help."
                    total_tokens = response.input_tokens + response.output_tokens

        stats["total_tokens"] += total_tokens

        _run_async(memory.save_turn(
            tenant_id, conversation_id, clean_text, assistant_text,
            metadata={"route": "teams", "model": model_short_name, "tokens": total_tokens},
        ))

        # Send response back via Teams API
        outbound = OutboundMessage(
            channel=ChannelType.TEAMS,
            conversation_id=message.conversation_id,
            recipient_id=message.channel_user_id,
            text=assistant_text,
        )
        _run_async(teams_adapter.send_response(outbound))

    def _get_teams_adapter_local(self) -> TeamsAdapter | None:
        """Get TeamsAdapter from local env/secrets if configured."""
        try:
            creds = _run_async(secrets.get(DEFAULT_TENANT, "teams"))
            app_id = creds.get("app_id", "")
            app_secret = creds.get("app_secret", "")
            if app_id and app_secret:
                return TeamsAdapter(app_id, app_secret)
        except Exception:
            pass
        # Also check environment variables
        app_id = os.environ.get("TEAMS_APP_ID", "")
        app_secret = os.environ.get("TEAMS_APP_SECRET", "")
        if app_id and app_secret:
            return TeamsAdapter(app_id, app_secret)
        return None

    def _handle_clear(self):
        """Clear conversation history."""
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            conversation_id = body.get("conversation_id", DEFAULT_CONVERSATION)
            _run_async(memory.clear_conversation(DEFAULT_TENANT, conversation_id))
            self._json_response({"cleared": True})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _serve_file(self, filename: str):
        """Serve an HTML file from the adapters/local directory."""
        html_path = Path(__file__).parent / filename
        if html_path.exists():
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html_path.read_bytes())
        else:
            self.send_error(404, f"{filename} not found")

    def _json_response(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def init():
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
    # Set default model if not already set
    if not tenant.settings.ai_model:
        tenant.settings.ai_model = DEFAULT_MODEL_ID
        _run_async(tenants.update_tenant(tenant))
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

    connected = _run_async(secrets.list_integrations("local"))
    logger.info(f"Connected integrations: {connected}")


def main():
    init()

    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), DevHandler)

    logger.info(f"")
    logger.info(f"  ╔══════════════════════════════════════╗")
    logger.info(f"  ║  T3nets Dev Server                   ║")
    logger.info(f"  ║                                      ║")
    logger.info(f"  ║  Chat:   http://localhost:{port}       ║")
    logger.info(f"  ║  Health: http://localhost:{port}/health ║")
    logger.info(f"  ║                                      ║")
    logger.info(f"  ║  Routing: Rules → Claude (hybrid)    ║")
    logger.info(f"  ║  Debug:   append --raw to messages   ║")
    logger.info(f"  ╚══════════════════════════════════════╝")
    logger.info(f"")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info(f"Session stats: {json.dumps(stats)}")
        logger.info("Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
