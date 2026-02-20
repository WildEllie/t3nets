"""
Inbound and Outbound message models.
Normalized representations that all channels produce/consume.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ChannelType(Enum):
    DASHBOARD = "dashboard"
    API = "api"
    TEAMS = "teams"
    WHATSAPP = "whatsapp"
    SMS = "sms"
    VOICE = "voice"
    MESSENGER = "messenger"
    SLACK = "slack"
    TELEGRAM = "telegram"
    EMAIL = "email"
    WEBHOOK = "webhook"


class ChannelCapability(Enum):
    """What a channel can do. Router adapts responses based on this."""
    RICH_TEXT = "rich_text"
    BUTTONS = "buttons"
    CARDS = "cards"
    FILE_UPLOAD = "file_upload"
    FILE_RECEIVE = "file_receive"
    THREADING = "threading"
    TYPING_INDICATOR = "typing"
    REACTIONS = "reactions"
    VOICE_INPUT = "voice_input"
    VOICE_OUTPUT = "voice_output"


@dataclass
class InboundMessage:
    """
    Normalized inbound message.
    Every channel adapter produces this. The router only sees this.
    """

    channel: ChannelType
    channel_user_id: str          # Channel-specific user ID
    user_display_name: str        # Human-readable name
    user_email: Optional[str]     # If available (Teams/Slack have it; SMS doesn't)
    conversation_id: str          # Channel-specific conversation/thread ID
    text: str                     # The actual message content
    attachments: list[dict] = field(default_factory=list)
    raw_event: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    timestamp: str = ""


@dataclass
class OutboundMessage:
    """
    Normalized outbound message.
    Router produces this. Channel adapter sends it.
    """

    channel: ChannelType
    conversation_id: str
    recipient_id: str
    text: str                                   # Plain text or markdown
    rich_content: Optional[dict] = None         # Cards, buttons, etc.
    attachments: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
