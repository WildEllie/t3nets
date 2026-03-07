"""
Ollama AI Provider — OpenAI-compatible API.

Zero-cost local AI using Ollama. Works with any model that supports
tool/function calling (Llama 3.1+, Mistral, Qwen, etc.).

Also compatible with any OpenAI-compatible endpoint (vLLM, Groq, Together.ai).
"""

import json
import logging
import urllib.request
from typing import Any

from agent.interfaces.ai_provider import AIProvider, AIResponse, ToolCall, ToolDefinition

logger = logging.getLogger("t3nets.ollama")


class OllamaProvider(AIProvider):
    """Calls Ollama via its OpenAI-compatible chat completions API."""

    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/v1/chat/completions"

    async def chat(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
        max_tokens: int = 4096,
    ) -> AIResponse:
        oai_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        oai_messages.extend(self._convert_messages(messages))

        body: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
        }

        if tools:
            body["tools"] = [self._tool_to_openai(t) for t in tools]

        return self._call_api(body)

    async def chat_with_tool_result(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
        tool_use_id: str,
        tool_result: dict[str, Any],
        max_tokens: int = 4096,
    ) -> AIResponse:
        # Append tool result in OpenAI format
        messages = messages + [
            {
                "role": "tool",
                "tool_call_id": tool_use_id,
                "content": json.dumps(tool_result),
            }
        ]

        oai_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        oai_messages.extend(self._convert_messages(messages))

        body: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
        }

        if tools:
            body["tools"] = [self._tool_to_openai(t) for t in tools]

        return self._call_api(body)

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert T3nets/Anthropic-style messages to OpenAI format."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            # Simple string content — pass through
            if isinstance(content, str):
                result.append({"role": role, "content": content})
                continue

            # Structured content blocks (Anthropic format) — convert
            if isinstance(content, list):
                # Check for tool_result blocks (Anthropic format)
                tool_results = [
                    b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                if tool_results:
                    for tr in tool_results:
                        result.append(
                            {
                                "role": "tool",
                                "tool_call_id": tr.get("tool_use_id", ""),
                                "content": tr.get("content", ""),
                            }
                        )
                    continue

                # Check for tool_use blocks (assistant response with tool calls)
                tool_uses = [
                    b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
                ]
                if tool_uses:
                    text_parts = [
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    tool_calls = []
                    for tu in tool_uses:
                        tool_calls.append(
                            {
                                "id": tu.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": tu.get("name", ""),
                                    "arguments": json.dumps(tu.get("input", {})),
                                },
                            }
                        )
                    assistant_msg: dict[str, Any] = {
                        "role": "assistant",
                        "content": "\n".join(text_parts) if text_parts else None,
                        "tool_calls": tool_calls,
                    }
                    result.append(assistant_msg)
                    continue

                # Already OpenAI-format tool message
                if role == "tool":
                    result.append(msg)
                    continue

                # Text blocks only — join them
                text_parts = [b.get("text", "") if isinstance(b, dict) else str(b) for b in content]
                result.append({"role": role, "content": "\n".join(text_parts)})
                continue

            result.append({"role": role, "content": str(content)})

        return result

    def _tool_to_openai(self, tool: ToolDefinition) -> dict[str, Any]:
        """Convert ToolDefinition to OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }

    def _call_api(self, body: dict[str, Any]) -> AIResponse:
        """Make HTTP request to Ollama's OpenAI-compatible endpoint."""
        data = json.dumps(body).encode()

        req = urllib.request.Request(self.api_url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            logger.error(f"Ollama API error: {e}")
            raise ConnectionError(
                f"Cannot reach Ollama at {self.base_url}. "
                "Is Ollama running? Start it with: ollama serve"
            ) from e

        return self._parse_response(result)

    def _parse_response(self, result: dict[str, Any]) -> AIResponse:
        """Parse OpenAI-compatible response into AIResponse."""
        choice = result.get("choices", [{}])[0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "")

        text = message.get("content")
        tool_calls: list[ToolCall] = []

        for tc in message.get("tool_calls", []):
            func = tc.get("function", {})
            args_str = func.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {}

            tool_calls.append(
                ToolCall(
                    tool_name=func.get("name", ""),
                    tool_params=args,
                    tool_use_id=tc.get("id", ""),
                )
            )

        # Map OpenAI finish_reason to T3nets stop_reason
        stop_reason = "end_turn"
        if finish_reason == "tool_calls":
            stop_reason = "tool_use"
        elif finish_reason == "length":
            stop_reason = "max_tokens"

        usage = result.get("usage", {})
        return AIResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )
