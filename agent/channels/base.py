"""
Channel Adapter — base class and registry.

Every delivery channel (Teams, Slack, WhatsApp, etc.) implements
ChannelAdapter. The router discovers available channels via ChannelRegistry.
"""

from abc import ABC, abstractmethod

from agent.models.message import (
    ChannelType,
    ChannelCapability,
    InboundMessage,
    OutboundMessage,
)


class ChannelAdapter(ABC):
    """
    Base class for all channel adapters.
    Implement this to add a new channel. Five required methods.
    """

    @abstractmethod
    def channel_type(self) -> ChannelType:
        """Which channel this adapter handles."""
        ...

    @abstractmethod
    def capabilities(self) -> set[ChannelCapability]:
        """
        What this channel supports.
        Router uses this to adapt responses (e.g., no markdown for SMS).
        """
        ...

    @abstractmethod
    def parse_inbound(self, raw_event: dict) -> InboundMessage:
        """
        Convert a raw webhook/event payload into a normalized InboundMessage.
        Each channel has its own payload format — this is where translation happens.
        """
        ...

    @abstractmethod
    async def send_response(self, message: OutboundMessage) -> bool:
        """
        Send a response back through this channel.
        Returns True if sent successfully.
        """
        ...

    @abstractmethod
    def validate_webhook(self, headers: dict, body: bytes) -> bool:
        """
        Verify that an incoming webhook is authentic.
        Each channel has its own signature/token verification.
        """
        ...

    # --- Optional methods with defaults ---

    async def send_typing_indicator(self, conversation_id: str) -> None:
        """Show typing indicator. No-op by default."""
        pass

    async def send_acknowledgment(
        self, conversation_id: str, text: str = "On it..."
    ) -> None:
        """Send a quick ack before processing. Useful for slow skills."""
        if ChannelCapability.TYPING_INDICATOR in self.capabilities():
            await self.send_typing_indicator(conversation_id)


class ChannelRegistry:
    """
    Manages available channel adapters.
    Channels register at startup. Router queries the registry to handle messages.
    """

    def __init__(self):
        self._adapters: dict[ChannelType, ChannelAdapter] = {}

    def register(self, adapter: ChannelAdapter) -> None:
        """Register a channel adapter."""
        self._adapters[adapter.channel_type()] = adapter

    def get(self, channel_type: ChannelType) -> ChannelAdapter:
        """Get adapter for a channel type. Raises if not registered."""
        if channel_type not in self._adapters:
            raise ChannelNotRegistered(
                f"Channel '{channel_type.value}' is not registered. "
                f"Available: {[c.value for c in self._adapters.keys()]}"
            )
        return self._adapters[channel_type]

    def available_channels(self) -> list[ChannelType]:
        """List all registered channel types."""
        return list(self._adapters.keys())

    def is_registered(self, channel_type: ChannelType) -> bool:
        return channel_type in self._adapters


class ChannelNotRegistered(Exception):
    pass
