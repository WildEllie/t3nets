"""
Re-export shim — canonical definitions live in t3nets_sdk.models.message.

Kept for backwards-compatible imports of the form:
    from agent.models.message import ChannelType, InboundMessage, OutboundMessage
"""

from t3nets_sdk.models.message import (
    ChannelCapability,
    ChannelType,
    InboundMessage,
    OutboundMessage,
)

__all__ = [
    "ChannelCapability",
    "ChannelType",
    "InboundMessage",
    "OutboundMessage",
]
