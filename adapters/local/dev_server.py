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
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/chat":
            self._handle_chat()
        elif path == "/api/clear":
            self._handle_clear()
        elif path == "/api/settings":
            self._handle_settings_post()
        elif path.startswith("/api/integrations/"):
            self._handle_integrations_post(path)
        elif path == "/api/admin/tenants":
            self._handle_create_tenant()
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
            self._json_response({
                "ai_model": tenant.settings.ai_model or DEFAULT_MODEL_ID,
                "provider": PROVIDER,
                "models": get_models_for_provider(PROVIDER),
                "platform": os.getenv("T3NETS_PLATFORM", "local"),
                "stage": os.getenv("T3NETS_STAGE", "dev"),
                "build": BUILD_NUMBER,
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
                _run_async(tenants.update_tenant(tenant))
                logger.info(f"Model changed to: {model.display_name} ({model_id})")

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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, OPTIONS")
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
