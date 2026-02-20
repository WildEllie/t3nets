"""
T3nets Local Development Server

A simple HTTP server that wires all local adapters together.
Handles the full message → Claude → skill → Claude → response loop synchronously.

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

DEFAULT_TENANT = "local"
DEFAULT_CONVERSATION = "dashboard-default"
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


def _run_async(coro):
    """Run async code from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


class DevHandler(BaseHTTPRequestHandler):
    """HTTP request handler for local development."""

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "/chat":
            self._serve_chat_page()
        elif path == "/health":
            self._json_response({"status": "ok", "env": "local"})
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/chat":
            self._handle_chat()
        else:
            self.send_error(404)

    def _handle_chat(self):
        """Handle a chat message — full synchronous loop."""
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = body.get("text", "").strip()

            if not text:
                self._json_response({"error": "Empty message"}, 400)
                return

            conversation_id = body.get("conversation_id", DEFAULT_CONVERSATION)

            logger.info(f"Chat: {text[:100]}")

            # 1. Load conversation history
            history = _run_async(
                memory.get_conversation(DEFAULT_TENANT, conversation_id)
            )

            # 2. Get tools
            tenant = _run_async(tenants.get_tenant(DEFAULT_TENANT))
            tools = skills.get_tools_for_tenant(
                type("Ctx", (), {"tenant": tenant})()  # Minimal context
            )

            # 3. Build system prompt
            system = f"""You are an AI assistant for {tenant.name} on the T3nets platform.
Be direct, helpful, and action-oriented. Flag risks early. Suggest actions.
When you need data, use the available tools.
When you can answer directly, do so without tools."""

            # 4. Call Claude
            messages = history + [{"role": "user", "content": text}]
            response = _run_async(ai.chat(
                model=DEFAULT_MODEL,
                system=system,
                messages=messages,
                tools=tools,
            ))

            logger.info(
                f"Claude: tool_use={response.has_tool_use}, "
                f"tokens={response.input_tokens}+{response.output_tokens}"
            )

            # 5. If tool use, execute skill and call Claude again
            if response.has_tool_use:
                tool_call = response.tool_calls[0]  # Handle first tool call
                logger.info(f"Skill: {tool_call.tool_name}({json.dumps(tool_call.tool_params)[:200]})")

                # Execute via DirectBus
                request_id = f"local-{conversation_id}"
                _run_async(bus.publish_skill_invocation(
                    tenant_id=DEFAULT_TENANT,
                    skill_name=tool_call.tool_name,
                    params=tool_call.tool_params,
                    session_id=conversation_id,
                    request_id=request_id,
                    reply_channel="dashboard",
                    reply_target="dashboard-user",
                ))

                # Get result
                skill_result = bus.get_result(request_id)
                if not skill_result:
                    skill_result = {"error": "Skill returned no result"}

                logger.info(f"Skill result: {json.dumps(skill_result)[:300]}")

                # Build messages with tool use + tool result for Claude
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

                # Call Claude again with tool result
                final_response = _run_async(ai.chat_with_tool_result(
                    model=DEFAULT_MODEL,
                    system=system,
                    messages=messages_with_tool,
                    tools=tools,
                    tool_use_id=tool_call.tool_use_id,
                    tool_result=skill_result,
                ))

                assistant_text = final_response.text or "I got the data but couldn't format a response."
                total_tokens = (
                    response.input_tokens + response.output_tokens +
                    final_response.input_tokens + final_response.output_tokens
                )
            else:
                assistant_text = response.text or "I'm not sure how to respond to that."
                total_tokens = response.input_tokens + response.output_tokens

            # 6. Save conversation turn
            _run_async(memory.save_turn(
                DEFAULT_TENANT, conversation_id, text, assistant_text,
            ))

            # 7. Return response
            self._json_response({
                "text": assistant_text,
                "conversation_id": conversation_id,
                "tokens": total_tokens,
            })

        except Exception as e:
            logger.exception("Chat error")
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
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        """Suppress default access logs (we have our own logging)."""
        pass


def init():
    """Initialize all components."""
    global ai, memory, tenants, secrets, skills, bus

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

    # Direct bus (synchronous skill execution)
    bus = DirectBus(skills, secrets)

    # Register channels
    channels = ChannelRegistry()
    channels.register(DashboardAdapter())

    # Seed default tenant with all loaded skills enabled
    tenant = tenants.seed_default_tenant(
        tenant_id="local",
        name="Local Development",
        enabled_skills=skills.list_skill_names(),
    )
    logger.info(f"Tenant: {tenant.name} (skills: {tenant.settings.enabled_skills})")

    # Check integrations
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
    logger.info(f"  ╚══════════════════════════════════════╝")
    logger.info(f"")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
