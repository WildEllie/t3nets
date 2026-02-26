"""
AI Provider Interface

Cloud-agnostic abstraction for LLM interactions.
Implementations: BedrockProvider (AWS), AnthropicProvider (local), etc.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ToolDefinition:
    """A skill exposed to the AI as a callable tool."""
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    """AI's request to invoke a specific tool."""
    tool_name: str
    tool_params: dict
    tool_use_id: str  # For correlating tool results back to AI


@dataclass
class AIResponse:
    """Normalized AI response."""
    text: Optional[str] = None          # Direct text response (if no tool use)
    tool_calls: list[ToolCall] = field(default_factory=list)  # Tool invocations
    stop_reason: str = ""               # "end_turn", "tool_use", etc.
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def has_tool_use(self) -> bool:
        return len(self.tool_calls) > 0


class AIProvider(ABC):
    """
    Abstract base class for AI model providers.

    The router calls this to get Claude's response. The implementation
    handles the specifics of Bedrock, direct Anthropic API, OpenAI, etc.
    """

    @abstractmethod
    async def chat(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[ToolDefinition],
        max_tokens: int = 4096,
    ) -> AIResponse:
        """
        Send a conversation to the AI model and get a response.

        Args:
            model: Model identifier (e.g., "claude-sonnet-4-5-20250929")
            system: System prompt
            messages: Conversation history [{"role": "user"/"assistant", "content": "..."}]
            tools: Available tools (skills) the AI can invoke
            max_tokens: Maximum response tokens

        Returns:
            AIResponse with either text or tool calls
        """
        ...

    @abstractmethod
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
        """
        Continue a conversation after a tool has returned results.
        Claude needs to see the tool result to formulate a final response.

        Args:
            tool_use_id: Correlates to the original ToolCall
            tool_result: The data returned by the skill
        """
        ...
