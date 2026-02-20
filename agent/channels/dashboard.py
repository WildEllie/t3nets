"""
Dashboard Channel Adapter.

The built-in web chat. For the local dev server, this works over
simple HTTP POST (not WebSocket — keep it simple for now).
"""

from agent.channels.base import ChannelAdapter
from agent.models.message import (
    ChannelType,
    ChannelCapability,
    InboundMessage,
    OutboundMessage,
)


class DashboardAdapter(ChannelAdapter):
    """Dashboard channel — web-based chat interface."""

    def channel_type(self) -> ChannelType:
        return ChannelType.DASHBOARD

    def capabilities(self) -> set[ChannelCapability]:
        return {
            ChannelCapability.RICH_TEXT,
            ChannelCapability.BUTTONS,
            ChannelCapability.CARDS,
            ChannelCapability.FILE_UPLOAD,
            ChannelCapability.FILE_RECEIVE,
            ChannelCapability.THREADING,
            ChannelCapability.TYPING_INDICATOR,
        }

    def parse_inbound(self, raw_event: dict) -> InboundMessage:
        return InboundMessage(
            channel=ChannelType.DASHBOARD,
            channel_user_id=raw_event.get("user_id", "dashboard-user"),
            user_display_name=raw_event.get("user_name", "Dashboard User"),
            user_email=raw_event.get("user_email"),
            conversation_id=raw_event.get("conversation_id", "dashboard-default"),
            text=raw_event.get("text", ""),
            raw_event=raw_event,
            timestamp=raw_event.get("timestamp", ""),
        )

    async def send_response(self, message: OutboundMessage) -> bool:
        # In HTTP mode, the response is returned directly by the dev server.
        # This method would be used for WebSocket push in production.
        return True

    def validate_webhook(self, headers: dict, body: bytes) -> bool:
        # Dashboard uses JWT auth, not webhook signatures.
        return True
