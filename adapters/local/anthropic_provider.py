"""
Local AI Provider — Direct Anthropic API.

For local development. No AWS/Bedrock dependency.
"""

import json
import urllib.request
from agent.interfaces.ai_provider import AIProvider, AIResponse, ToolDefinition, ToolCall


class AnthropicProvider(AIProvider):
    """Calls Anthropic API directly using urllib (no dependencies)."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.anthropic.com/v1/messages"

    async def chat(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[ToolDefinition],
        max_tokens: int = 4096,
    ) -> AIResponse:
        body = {
            "model": model,
            "system": system,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        if tools:
            body["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in tools
            ]

        return self._call_api(body)

    async def chat_with_tool_result(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[ToolDefinition],
        tool_use_id: str,
        tool_result: dict,
        max_tokens: int = 4096,
    ) -> AIResponse:
        # Append tool result to messages
        messages = messages + [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": json.dumps(tool_result),
                    }
                ],
            }
        ]

        body = {
            "model": model,
            "system": system,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        if tools:
            body["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in tools
            ]

        return self._call_api(body)

    def _call_api(self, body: dict) -> AIResponse:
        """Make HTTP request to Anthropic API."""
        data = json.dumps(body).encode()

        req = urllib.request.Request(self.base_url, data=data, method="POST")
        req.add_header("x-api-key", self.api_key)
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("content-type", "application/json")

        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())

        return self._parse_response(result)

    def _parse_response(self, result: dict) -> AIResponse:
        """Parse Anthropic API response into AIResponse."""
        text_parts = []
        tool_calls = []

        for block in result.get("content", []):
            if block["type"] == "text":
                text_parts.append(block["text"])
            elif block["type"] == "tool_use":
                tool_calls.append(
                    ToolCall(
                        tool_name=block["name"],
                        tool_params=block["input"],
                        tool_use_id=block["id"],
                    )
                )

        return AIResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=result.get("stop_reason", ""),
            input_tokens=result.get("usage", {}).get("input_tokens", 0),
            output_tokens=result.get("usage", {}).get("output_tokens", 0),
        )
