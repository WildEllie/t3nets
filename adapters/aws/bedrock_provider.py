"""
AWS AI Provider — Amazon Bedrock.

Calls Claude via Bedrock. Uses IAM role auth (no API key needed).
"""

import json
import boto3
from agent.interfaces.ai_provider import AIProvider, AIResponse, ToolDefinition, ToolCall


class BedrockProvider(AIProvider):
    """Calls Claude via Amazon Bedrock using the Converse API."""

    def __init__(self, region: str = "us-east-1", model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"):
        self.client = boto3.client("bedrock-runtime", region_name=region)
        self.model_id = model_id

    async def chat(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[ToolDefinition],
        max_tokens: int = 4096,
    ) -> AIResponse:
        request = self._build_request(system, messages, tools, max_tokens)
        response = self.client.converse(**request)
        return self._parse_response(response)

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
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [{"json": tool_result}],
                        }
                    }
                ],
            }
        ]

        request = self._build_request(system, messages, tools, max_tokens)
        response = self.client.converse(**request)
        return self._parse_response(response)

    def _build_request(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolDefinition],
        max_tokens: int,
    ) -> dict:
        """Build Bedrock Converse API request."""
        request = {
            "modelId": self.model_id,
            "system": [{"text": system}],
            "messages": self._convert_messages(messages),
            "inferenceConfig": {
                "maxTokens": max_tokens,
            },
        }

        if tools:
            request["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": {"json": t.input_schema},
                        }
                    }
                    for t in tools
                ]
            }

        return request

    def _convert_messages(self, messages: list[dict]) -> list[dict]:
        """
        Convert our message format to Bedrock Converse format.
        Handles both simple string content and structured content blocks.
        """
        converted = []
        for msg in messages:
            content = msg.get("content")

            # Already structured (tool_use, tool_result blocks)
            if isinstance(content, list):
                bedrock_content = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            bedrock_content.append({
                                "toolUse": {
                                    "toolUseId": block["id"],
                                    "name": block["name"],
                                    "input": block["input"],
                                }
                            })
                        elif block.get("type") == "tool_result":
                            bedrock_content.append({
                                "toolResult": {
                                    "toolUseId": block["tool_use_id"],
                                    "content": [{"json": json.loads(block["content"])}]
                                    if isinstance(block["content"], str)
                                    else [{"json": block["content"]}],
                                }
                            })
                        else:
                            bedrock_content.append({"text": str(block)})
                    else:
                        bedrock_content.append({"text": str(block)})

                converted.append({
                    "role": msg["role"],
                    "content": bedrock_content,
                })
            # Simple string content
            else:
                converted.append({
                    "role": msg["role"],
                    "content": [{"text": str(content)}],
                })

        return converted

    def _parse_response(self, response: dict) -> AIResponse:
        """Parse Bedrock Converse API response."""
        text_parts = []
        tool_calls = []

        output = response.get("output", {})
        message = output.get("message", {})

        for block in message.get("content", []):
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                tool_calls.append(
                    ToolCall(
                        tool_name=tu["name"],
                        tool_params=tu["input"],
                        tool_use_id=tu["toolUseId"],
                    )
                )

        usage = response.get("usage", {})
        stop_reason = response.get("stopReason", "")

        return AIResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
        )
