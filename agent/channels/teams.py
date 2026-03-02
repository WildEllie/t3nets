"""
Microsoft Teams Channel Adapter.

Receives messages from Teams via Bot Framework webhook and sends
responses back via Bot Framework REST API. No SDK dependency —
uses the HTTP API directly for minimal footprint.

Setup:
    1. Register a bot in Azure Portal (Bot Services → Create Azure Bot)
    2. Note the App ID and create a Client Secret
    3. Set Messaging Endpoint to: https://{your-api}/api/channels/teams/webhook
    4. In T3nets settings, add Teams integration with App ID + Secret
    5. Install bot in Teams (via Teams Admin Center or App Studio manifest)
"""

import json
import logging
from typing import Any, Optional
from urllib.request import urlopen, Request

from agent.channels.base import ChannelAdapter
from agent.channels.teams_auth import BotFrameworkAuth
from agent.models.message import (
    ChannelType,
    ChannelCapability,
    InboundMessage,
    OutboundMessage,
)

logger = logging.getLogger("t3nets.teams")


class TeamsAdapter(ChannelAdapter):
    """
    Microsoft Teams channel adapter.

    Translates Bot Framework Activity payloads to/from T3nets messages.
    Uses Bot Framework REST API for outbound responses.
    """

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.auth = BotFrameworkAuth(app_id, app_secret)
        # serviceUrl is set per-conversation from inbound activities
        self._service_urls: dict[str, str] = {}

    def channel_type(self) -> ChannelType:
        return ChannelType.TEAMS

    def capabilities(self) -> set[ChannelCapability]:
        return {
            ChannelCapability.RICH_TEXT,
            ChannelCapability.BUTTONS,
            ChannelCapability.CARDS,
            ChannelCapability.THREADING,
            ChannelCapability.TYPING_INDICATOR,
        }

    def parse_inbound(self, raw_event: dict[str, Any]) -> InboundMessage:
        """
        Parse a Bot Framework Activity into an InboundMessage.

        Bot Framework Activity structure (key fields):
            {
                "type": "message",
                "id": "activity-id",
                "timestamp": "2026-01-15T10:30:00.000Z",
                "serviceUrl": "https://smba.trafficmanager.net/teams/",
                "channelId": "msteams",
                "from": {
                    "id": "29:aad-object-id",
                    "name": "John Doe",
                    "aadObjectId": "guid"
                },
                "conversation": {
                    "id": "19:thread-id@thread.tacv2",
                    "tenantId": "azure-ad-tenant-id",
                    "conversationType": "personal" | "groupChat" | "channel"
                },
                "recipient": {
                    "id": "28:bot-app-id",
                    "name": "T3nets Bot"
                },
                "text": "sprint status",
                "channelData": {
                    "tenant": {"id": "azure-ad-tenant-id"},
                    "team": {"id": "team-id"},  # if in a team channel
                    "channel": {"id": "channel-id"}  # if in a team channel
                }
            }
        """
        activity = raw_event

        # Extract user info
        from_user = activity.get("from", {})
        user_id = from_user.get("aadObjectId", from_user.get("id", "unknown"))
        user_name = from_user.get("name", "Teams User")

        # Extract conversation info
        conversation = activity.get("conversation", {})
        conversation_id = conversation.get("id", "")
        conversation_type = conversation.get("conversationType", "personal")

        # Extract message text
        text = activity.get("text", "")

        # In group chats/channels, the bot is @mentioned.
        # Strip the @mention to get the actual command.
        text = self._strip_bot_mention(text, activity)

        # Cache the serviceUrl for this conversation (needed for responses)
        service_url = activity.get("serviceUrl", "")
        if service_url and conversation_id:
            self._service_urls[conversation_id] = service_url.rstrip("/")

        # Extract Azure AD tenant ID for multi-tenant resolution
        channel_data = activity.get("channelData", {})
        azure_tenant_id = (
            channel_data.get("tenant", {}).get("id", "")
            or conversation.get("tenantId", "")
        )

        return InboundMessage(
            channel=ChannelType.TEAMS,
            channel_user_id=user_id,
            user_display_name=user_name,
            user_email=None,  # Teams doesn't include email in activities
            conversation_id=conversation_id,
            text=text.strip(),
            raw_event=activity,
            metadata={
                "conversation_type": conversation_type,
                "azure_tenant_id": azure_tenant_id,
                "activity_id": activity.get("id", ""),
                "service_url": service_url,
            },
            timestamp=activity.get("timestamp", ""),
        )

    async def send_response(self, message: OutboundMessage) -> bool:
        """
        Send a response back to Teams via Bot Framework REST API.

        POST {serviceUrl}/v3/conversations/{conversationId}/activities
        """
        service_url = self._service_urls.get(message.conversation_id, "")
        if not service_url:
            logger.error(
                f"No serviceUrl cached for conversation {message.conversation_id}"
            )
            return False

        # Get bot token for outbound auth
        token = await self.auth.get_bot_token()
        if not token:
            logger.error("Failed to acquire bot token for response")
            return False

        # Build the Activity response
        response_activity: dict[str, Any] = {
            "type": "message",
            "text": message.text,
        }

        # Add rich content if available (Adaptive Cards)
        if message.rich_content:
            response_activity["attachments"] = [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": message.rich_content,
                }
            ]

        url = (
            f"{service_url}/v3/conversations/"
            f"{message.conversation_id}/activities"
        )

        try:
            data = json.dumps(response_activity).encode()
            req = Request(
                url,
                data=data,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )

            with urlopen(req, timeout=15) as resp:
                status = resp.status
                if status in (200, 201):
                    logger.info(
                        f"Sent response to Teams conversation "
                        f"{message.conversation_id[:20]}..."
                    )
                    return True
                else:
                    body = resp.read().decode()
                    logger.error(
                        f"Teams API returned {status}: {body[:200]}"
                    )
                    return False

        except Exception as e:
            logger.error(f"Failed to send Teams response: {e}")
            return False

    def validate_webhook(self, headers: dict[str, Any], body: bytes) -> bool:
        """
        Validate that an incoming webhook is from Microsoft Bot Framework.

        Checks the Authorization header JWT against Microsoft's signing keys.
        """
        auth_header = headers.get("Authorization", headers.get("authorization", ""))
        return self.auth.validate_incoming(auth_header)

    async def send_typing_indicator(self, conversation_id: str) -> None:
        """
        Send a typing indicator to Teams.

        POST {serviceUrl}/v3/conversations/{conversationId}/activities
        with activity type "typing"
        """
        service_url = self._service_urls.get(conversation_id, "")
        if not service_url:
            return

        token = await self.auth.get_bot_token()
        if not token:
            return

        typing_activity = {"type": "typing"}
        url = f"{service_url}/v3/conversations/{conversation_id}/activities"

        try:
            data = json.dumps(typing_activity).encode()
            req = Request(
                url,
                data=data,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(req, timeout=5) as resp:
                pass  # Fire and forget
        except Exception as e:
            logger.debug(f"Typing indicator failed (non-critical): {e}")

    # --- Helpers ---

    def _strip_bot_mention(self, text: str, activity: dict[str, Any]) -> str:
        """
        Remove @bot mention from message text.

        In group chats and channels, Teams prepends "<at>BotName</at> "
        to the message text. We strip this to get the actual command.
        """
        entities = activity.get("entities", [])
        for entity in entities:
            if entity.get("type") == "mention":
                mentioned = entity.get("mentioned", {})
                # If the mention is the bot itself, remove it from text
                if mentioned.get("id") == activity.get("recipient", {}).get("id"):
                    mention_text = entity.get("text", "")
                    if mention_text:
                        text = text.replace(mention_text, "")
        return text

    @staticmethod
    def is_message_activity(activity: dict[str, Any]) -> bool:
        """Check if an activity is a user message (not system event)."""
        return activity.get("type") == "message" and bool(
            activity.get("text", "").strip()
        )

    @staticmethod
    def is_conversation_update(activity: dict[str, Any]) -> bool:
        """Check if this is a conversationUpdate (bot added/removed)."""
        return activity.get("type") == "conversationUpdate"

    @staticmethod
    def is_bot_added(activity: dict[str, Any]) -> bool:
        """Check if the bot was just added to a conversation."""
        if activity.get("type") != "conversationUpdate":
            return False
        members_added = activity.get("membersAdded", [])
        recipient_id = activity.get("recipient", {}).get("id", "")
        return any(m.get("id") == recipient_id for m in members_added)
