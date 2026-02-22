"""
T3nets AWS Server Entrypoint

Same HTTP server as local, but wired to AWS adapters:
  - Bedrock instead of direct Anthropic API
  - DynamoDB instead of SQLite
  - Secrets Manager instead of .env
  - DirectBus (still synchronous for Phase 1)

Runs inside ECS Fargate container.

Usage:
    python -m adapters.aws.server
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

from agent.skills.registry import SkillRegistry
from agent.channels.base import ChannelRegistry
from agent.channels.dashboard import DashboardAdapter
from agent.router.rule_router import RuleBasedRouter, strip_raw_flag
from adapters.aws.bedrock_provider import BedrockProvider
from adapters.aws.dynamodb_conversation_store import DynamoDBConversationStore
from adapters.aws.dynamodb_tenant_store import DynamoDBTenantStore
from adapters.aws.secrets_manager import SecretsManagerProvider
from adapters.local.direct_bus import DirectBus  # Reuse synchronous bus for Phase 1

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
bus: DirectBus
rule_router: RuleBasedRouter
started_at: float = 0.0

DEFAULT_TENANT = "default"
DEFAULT_MODEL = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0")

stats = {
    "rule_routed": 0,
    "ai_routed": 0,
    "conversational": 0,
    "raw": 0,
    "errors": 0,
    "total_tokens": 0,
}


def _run_async(coro):
    return asyncio.run(coro)


def _format_raw_json(data: dict) -> str:
    return json.dumps(data, indent=2, default=str)


def _uptime_human(seconds: float) -> str:
    s = int(seconds)
    if s < 60: return f"{s}s"
    elif s < 3600: return f"{s // 60}m {s % 60}s"
    elif s < 86400: return f"{s // 3600}h {(s % 3600) // 60}m"
    else: return f"{s // 86400}d {(s % 86400) // 3600}h"


class AWSHandler(BaseHTTPRequestHandler):
    """HTTP request handler for AWS deployment."""

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/chat":
            self._serve_file("chat.html", "adapters/local")
        elif path == "/health":
            self._serve_file("health.html", "adapters/local")
        elif path == "/api/health":
            self._handle_health_api()
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/chat":
            self._handle_chat()
        elif path == "/api/clear":
            self._handle_clear()
        else:
            self.send_error(404)

    def _handle_health_api(self):
        try:
            uptime_secs = time.time() - started_at
            tenant = _run_async(tenants.get_tenant(DEFAULT_TENANT))
            connected = _run_async(secrets.list_integrations(DEFAULT_TENANT))

            health = {
                "status": "ok",
                "environment": "aws",
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
                    "model": DEFAULT_MODEL,
                    "api_key_preview": "IAM role (no key)",
                    "total_tokens": stats["total_tokens"],
                },
                "routing": stats,
                "integrations": {
                    name: {"connected": name in connected}
                    for name in ["jira", "github", "teams", "twilio"]
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
            }
            self._json_response(health)
        except Exception as e:
            logger.exception("Health check error")
            self._json_response({"status": "error", "error": str(e)}, 500)

    def _handle_chat(self):
        """Handle chat — identical logic to local, different adapters underneath."""
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = body.get("text", "").strip()
            if not text:
                self._json_response({"error": "Empty message"}, 400)
                return

            conversation_id = body.get("conversation_id", "default")
            clean_text, is_raw = strip_raw_flag(text)
            is_raw_response = False

            logger.info(f"Chat: {text[:100]}" + (" [RAW]" if is_raw else ""))

            history = _run_async(memory.get_conversation(DEFAULT_TENANT, conversation_id))
            tenant = _run_async(tenants.get_tenant(DEFAULT_TENANT))

            system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
When you have data to present, format it clearly with structure."""

            if not is_raw and rule_router.is_conversational(clean_text):
                stats["conversational"] += 1
                messages = history + [{"role": "user", "content": clean_text}]
                response = _run_async(ai.chat(DEFAULT_MODEL, system, messages, []))
                assistant_text = response.text or "Hey! How can I help?"
                total_tokens = response.input_tokens + response.output_tokens
                route_type = "conversational"
            else:
                match = rule_router.match(clean_text, tenant.settings.enabled_skills)

                if match:
                    request_id = f"rule-{conversation_id}"
                    _run_async(bus.publish_skill_invocation(
                        DEFAULT_TENANT, match.skill_name, match.params,
                        conversation_id, request_id, "dashboard", "user",
                    ))
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
                        response = _run_async(ai.chat(DEFAULT_MODEL, system, messages, []))
                        assistant_text = response.text or "Got data but couldn't format."
                        total_tokens = response.input_tokens + response.output_tokens
                        route_type = "rule"
                else:
                    stats["ai_routed"] += 1
                    tools = skills.get_tools_for_tenant(type("C", (), {"tenant": tenant})())
                    messages = history + [{"role": "user", "content": clean_text}]
                    response = _run_async(ai.chat(DEFAULT_MODEL, system, messages, tools))

                    if response.has_tool_use:
                        tc = response.tool_calls[0]
                        request_id = f"ai-{conversation_id}"
                        _run_async(bus.publish_skill_invocation(
                            DEFAULT_TENANT, tc.tool_name, tc.tool_params,
                            conversation_id, request_id, "dashboard", "user",
                        ))
                        skill_result = bus.get_result(request_id) or {"error": "No result"}

                        if is_raw and rule_router.supports_raw(tc.tool_name):
                            stats["raw"] += 1
                            assistant_text = _format_raw_json(skill_result)
                            total_tokens = response.input_tokens + response.output_tokens
                            route_type = "ai"
                            is_raw_response = True
                        else:
                            messages_with_tool = messages + [{"role": "assistant", "content": [{"type": "tool_use", "id": tc.tool_use_id, "name": tc.tool_name, "input": tc.tool_params}]}]
                            final = _run_async(ai.chat_with_tool_result(DEFAULT_MODEL, system, messages_with_tool, tools, tc.tool_use_id, skill_result))
                            assistant_text = final.text or "Got data but couldn't format."
                            total_tokens = response.input_tokens + response.output_tokens + final.input_tokens + final.output_tokens
                            route_type = "ai"
                    else:
                        assistant_text = response.text or "Not sure how to help."
                        total_tokens = response.input_tokens + response.output_tokens
                        route_type = "ai"

            stats["total_tokens"] += total_tokens
            if not is_raw_response:
                _run_async(memory.save_turn(DEFAULT_TENANT, conversation_id, clean_text, assistant_text))

            self._json_response({
                "text": assistant_text,
                "conversation_id": conversation_id,
                "tokens": total_tokens,
                "route": route_type,
                "raw": is_raw_response,
            })
        except Exception as e:
            logger.exception("Chat error")
            stats["errors"] += 1
            self._json_response({"error": str(e)}, 500)

    def _handle_clear(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            cid = body.get("conversation_id", "default")
            _run_async(memory.clear_conversation(DEFAULT_TENANT, cid))
            self._json_response({"cleared": True})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _serve_file(self, filename: str, search_dir: str = None):
        """Serve HTML — check local adapter dir (shared UI files)."""
        base = Path(__file__).parent.parent.parent
        if search_dir:
            path = base / search_dir / filename
        else:
            path = base / filename

        if path.exists():
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(path.read_bytes())
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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass


# Single region for all AWS calls (data residency / IAM scope)
AWS_REGION = "us-east-1"


def init():
    global ai, memory, tenants, secrets, skills, bus, rule_router, started_at

    started_at = time.time()

    region = AWS_REGION
    conversations_table = os.getenv("DYNAMODB_CONVERSATIONS_TABLE")
    tenants_table = os.getenv("DYNAMODB_TENANTS_TABLE")
    secrets_prefix = os.getenv("SECRETS_PREFIX")

    if not all([conversations_table, tenants_table, secrets_prefix]):
        logger.error("Missing required env vars: DYNAMODB_CONVERSATIONS_TABLE, DYNAMODB_TENANTS_TABLE, SECRETS_PREFIX")
        sys.exit(1)

    ai = BedrockProvider(region=region, model_id=DEFAULT_MODEL)
    memory = DynamoDBConversationStore(conversations_table, region=region)
    tenants = DynamoDBTenantStore(tenants_table, region=region)
    secrets = SecretsManagerProvider(secrets_prefix, region=region)

    skills_obj = SkillRegistry()
    skills_dir = Path(__file__).parent.parent.parent / "agent" / "skills"
    skills_obj.load_from_directory(skills_dir)
    skills = skills_obj
    logger.info(f"Loaded skills: {skills.list_skill_names()}")

    rule_router = RuleBasedRouter(skills, confidence_threshold=0.5)
    bus = DirectBus(skills, secrets)

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


def main():
    init()
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), AWSHandler)

    logger.info("")
    logger.info("  ╔══════════════════════════════════════╗")
    logger.info("  ║  T3nets AWS Server                   ║")
    logger.info(f"  ║  http://0.0.0.0:{port}               ║")
    logger.info(f"  ║  Model: {DEFAULT_MODEL[:30]}  ║")
    logger.info("  ╚══════════════════════════════════════╝")
    logger.info("")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info(f"Stats: {json.dumps(stats)}")
        server.server_close()


if __name__ == "__main__":
    main()
