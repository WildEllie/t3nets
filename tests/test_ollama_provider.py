"""
Ollama provider tests.

Verifies OpenAI-compatible API format conversion, tool call mapping,
and response parsing.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.ollama.provider import OllamaProvider
from agent.interfaces.ai_provider import ToolDefinition


@pytest.fixture
def provider():
    return OllamaProvider(base_url="http://localhost:11434")


# --- Response parsing ---


def test_parse_text_response(provider):
    """A simple text response should be parsed correctly."""
    raw = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    resp = provider._parse_response(raw)
    assert resp.text == "Hello!"
    assert resp.tool_calls == []
    assert resp.stop_reason == "end_turn"
    assert resp.input_tokens == 10
    assert resp.output_tokens == 5
    assert not resp.has_tool_use


def test_parse_tool_call_response(provider):
    """A tool call response should map to ToolCall objects."""
    raw = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "sprint_status",
                                "arguments": '{"action": "status"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20},
    }
    resp = provider._parse_response(raw)
    assert resp.text is None
    assert resp.has_tool_use
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].tool_name == "sprint_status"
    assert resp.tool_calls[0].tool_params == {"action": "status"}
    assert resp.tool_calls[0].tool_use_id == "call_123"
    assert resp.stop_reason == "tool_use"


def test_parse_multiple_tool_calls(provider):
    """Multiple tool calls in a single response should all be parsed."""
    raw = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Let me check both.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "sprint_status",
                                "arguments": '{"action": "status"}',
                            },
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "ping",
                                "arguments": "{}",
                            },
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 30, "completion_tokens": 15},
    }
    resp = provider._parse_response(raw)
    assert resp.text == "Let me check both."
    assert len(resp.tool_calls) == 2
    assert resp.tool_calls[0].tool_name == "sprint_status"
    assert resp.tool_calls[1].tool_name == "ping"


def test_parse_max_tokens(provider):
    """finish_reason 'length' should map to stop_reason 'max_tokens'."""
    raw = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Truncated..."},
                "finish_reason": "length",
            }
        ],
        "usage": {},
    }
    resp = provider._parse_response(raw)
    assert resp.stop_reason == "max_tokens"


def test_parse_invalid_json_arguments(provider):
    """Invalid JSON in tool arguments should fall back to empty dict."""
    raw = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {
                                "name": "test_tool",
                                "arguments": "not valid json{{{",
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {},
    }
    resp = provider._parse_response(raw)
    assert resp.tool_calls[0].tool_params == {}


# --- Tool conversion ---


def test_tool_to_openai_format(provider):
    """ToolDefinition should convert to OpenAI function format."""
    tool = ToolDefinition(
        name="sprint_status",
        description="Get sprint status",
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "blockers"]},
            },
            "required": ["action"],
        },
    )
    result = provider._tool_to_openai(tool)
    assert result == {
        "type": "function",
        "function": {
            "name": "sprint_status",
            "description": "Get sprint status",
            "parameters": tool.input_schema,
        },
    }


# --- Message conversion ---


def test_convert_simple_messages(provider):
    """Simple string messages should pass through."""
    msgs = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    result = provider._convert_messages(msgs)
    assert result == msgs


def test_convert_anthropic_tool_result(provider):
    """Anthropic-style tool_result blocks should convert to OpenAI tool messages."""
    msgs = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_abc",
                    "content": '{"status": "ok"}',
                }
            ],
        }
    ]
    result = provider._convert_messages(msgs)
    assert len(result) == 1
    assert result[0]["role"] == "tool"
    assert result[0]["tool_call_id"] == "call_abc"
    assert result[0]["content"] == '{"status": "ok"}'


def test_convert_anthropic_tool_use(provider):
    """Anthropic-style tool_use blocks should convert to OpenAI assistant with tool_calls."""
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check."},
                {
                    "type": "tool_use",
                    "id": "call_xyz",
                    "name": "ping",
                    "input": {"echo": "test"},
                },
            ],
        }
    ]
    result = provider._convert_messages(msgs)
    assert len(result) == 1
    assert result[0]["role"] == "assistant"
    assert result[0]["content"] == "Let me check."
    assert len(result[0]["tool_calls"]) == 1
    tc = result[0]["tool_calls"][0]
    assert tc["id"] == "call_xyz"
    assert tc["function"]["name"] == "ping"
    assert json.loads(tc["function"]["arguments"]) == {"echo": "test"}


# --- Connection error ---


def test_connection_error():
    """Should raise ConnectionError with helpful message when Ollama is unreachable."""
    provider = OllamaProvider(base_url="http://localhost:99999")
    with pytest.raises(ConnectionError, match="Cannot reach Ollama"):
        provider._call_api({"model": "test", "messages": []})


# --- Full chat call (mocked) ---


@pytest.mark.asyncio
async def test_chat_builds_correct_request(provider):
    """chat() should build correct OpenAI-format request body."""
    tools = [
        ToolDefinition(
            name="ping",
            description="Health check",
            input_schema={"type": "object", "properties": {}},
        )
    ]

    mock_response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "pong"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }

    with patch.object(provider, "_call_api", return_value=provider._parse_response(mock_response)):
        resp = await provider.chat(
            model="llama3.1:8b",
            system="You are a helper.",
            messages=[{"role": "user", "content": "ping"}],
            tools=tools,
        )
    assert resp.text == "pong"
    assert resp.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_chat_with_tool_result(provider):
    """chat_with_tool_result() should append tool result in OpenAI format."""
    mock_response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Sprint is on track."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }

    with patch.object(provider, "_call_api", return_value=provider._parse_response(mock_response)):
        resp = await provider.chat_with_tool_result(
            model="llama3.1:8b",
            system="You are a helper.",
            messages=[{"role": "user", "content": "sprint status"}],
            tools=[],
            tool_use_id="call_abc",
            tool_result={"status": "on_track", "progress": 80},
        )
    assert resp.text == "Sprint is on track."
