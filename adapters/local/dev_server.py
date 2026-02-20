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

DEFAULT_TENANT = "local"
DEFAULT_CONVERSATION = "dashboard-default"
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"

# Stats for the session
stats = {"rule_routed": 0, "ai_routed": 0, "conversational": 0, "raw": 0}


def _run_async(coro):
    """Run async code from sync context."""
    return asyncio.run(coro)


def _format_raw_json(data: dict) -> str:
    """Format raw JSON for dashboard display."""
    return json.dumps(data, indent=2, default=str)


class DevHandler(BaseHTTPRequestHandler):
    """HTTP request handler for local development."""

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "/chat":
            self._serve_chat_page()
        elif path == "/health":
            self._json_response({"status": "ok", "env": "local", "routing_stats": stats})
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

    def _handle_chat(self):
        """Handle a chat message with hybrid routing."""
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = body.get("text", "").strip()

            if not text:
                self._json_response({"error": "Empty message"}, 400)
                return

            conversation_id = body.get("conversation_id", DEFAULT_CONVERSATION)

            # Check for --raw flag
            clean_text, is_raw = strip_raw_flag(text)

            logger.info(f"Chat: {text[:100]}" + (" [RAW]" if is_raw else ""))

            # Load conversation history
            history = _run_async(
                memory.get_conversation(DEFAULT_TENANT, conversation_id)
            )

            # Get tenant
            tenant = _run_async(tenants.get_tenant(DEFAULT_TENANT))

            system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
When you have data to present, format it clearly with structure."""

            # === TIER 0: Conversational (no tools, cheap) ===
            if not is_raw and rule_router.is_conversational(clean_text):
                logger.info("Route: CONVERSATIONAL (no tools)")
                stats["conversational"] += 1

                messages = history + [{"role": "user", "content": clean_text}]
                response = _run_async(ai.chat(
                    model=DEFAULT_MODEL,
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

                    # === RAW MODE: return skill data directly ===
                    if is_raw and rule_router.supports_raw(match.skill_name):
                        logger.info(f"Returning raw output for {match.skill_name}")
                        stats["raw"] += 1

                        assistant_text = _format_raw_json(skill_result)
                        total_tokens = 0
                        route_type = "raw"

                    # === NORMAL: Claude formats the result ===
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
                            model=DEFAULT_MODEL,
                            system=system,
                            messages=messages,
                            tools=[],
                        ))

                        assistant_text = response.text or "Got the data but couldn't format it."
                        total_tokens = response.input_tokens + response.output_tokens
                        route_type = "rule"

                # === TIER 2: Full Claude routing ===
                else:
                    # --raw with no rule match: warn the user
                    if is_raw:
                        logger.info("--raw flag ignored: no rule match, using AI routing")

                    logger.info("Route: AI (full Claude with tools)")
                    stats["ai_routed"] += 1

                    tools = skills.get_tools_for_tenant(
                        type("Ctx", (), {"tenant": tenant})()
                    )

                    messages = history + [{"role": "user", "content": clean_text}]
                    response = _run_async(ai.chat(
                        model=DEFAULT_MODEL,
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

                        # --raw via AI route: still honor it if skill supports it
                        if is_raw and rule_router.supports_raw(tool_call.tool_name):
                            logger.info(f"Returning raw output for {tool_call.tool_name} (AI-routed)")
                            stats["raw"] += 1

                            assistant_text = _format_raw_json(skill_result)
                            total_tokens = response.input_tokens + response.output_tokens
                            route_type = "raw"
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
                                model=DEFAULT_MODEL,
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

            # Save conversation (don't save raw output to history — it's debug noise)
            if route_type != "raw":
                _run_async(memory.save_turn(
                    DEFAULT_TENANT, conversation_id, clean_text, assistant_text,
                ))

            self._json_response({
                "text": assistant_text,
                "conversation_id": conversation_id,
                "tokens": total_tokens,
                "route": route_type,
            })

        except Exception as e:
            logger.exception("Chat error")
            self._json_response({"error": str(e)}, 500)

    def _handle_clear(self):
        """Clear conversation history."""
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            conversation_id = body.get("conversation_id", DEFAULT_CONVERSATION)
            _run_async(memory.clear_conversation(DEFAULT_TENANT, conversation_id))
            self._json_response({"cleared": True})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _serve_chat_page(self):
        """Serve the built-in chat HTML page."""
        html_path = Path(__file__).parent / "chat.html"
        if html_path.exists():
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html_path.read_bytes())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>T3nets</h1><p>chat.html not found</p>")

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


def init():
    """Initialize all components."""
    global ai, memory, tenants, secrets, skills, bus, rule_router

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

    # Rule-based router
    rule_router = RuleBasedRouter(skills, confidence_threshold=0.6)

    # Direct bus
    bus = DirectBus(skills, secrets)

    # Register channels
    channels = ChannelRegistry()
    channels.register(DashboardAdapter())

    # Seed default tenant
    tenant = tenants.seed_default_tenant(
        tenant_id="local",
        name="Local Development",
        enabled_skills=skills.list_skill_names(),
    )
    logger.info(f"Tenant: {tenant.name} (skills: {tenant.settings.enabled_skills})")

    connected = _run_async(secrets.list_integrations("local"))
    logger.info(f"Connected integrations: {connected}")


def main():
    init()

    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), DevHandler)

    logger.info(f"")
    logger.info(f"  ╔══════════════════════════════════════╗")
    logger.info(f"  ║  T3nets Dev Server                   ║")
    logger.info(f"  ║  http://localhost:{port}               ║")
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
