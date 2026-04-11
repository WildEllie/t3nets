"""
WhatsApp Channel Adapter (via Whapi.cloud).

Receives messages from WhatsApp via Whapi.cloud webhook and sends
responses via the Whapi.cloud REST API. Setup flow:
  1. Create a channel on whapi.cloud
  2. Scan the QR code with your WhatsApp
  3. Copy the API token and paste it in T3nets settings
  4. T3nets auto-registers the webhook on save

Whapi.cloud API docs: https://whapi.cloud/docs
"""

import hmac
import json
import logging
from typing import Any, cast
from urllib.request import Request, urlopen

from agent.channels.base import ChannelAdapter
from agent.models.message import (
    ChannelCapability,
    ChannelType,
    InboundMessage,
    OutboundMessage,
)

logger = logging.getLogger("t3nets.whatsapp")

WHAPI_API_BASE = "https://gate.whapi.cloud"


class WhatsAppAdapter(ChannelAdapter):
    """
    WhatsApp channel adapter via Whapi.cloud.

    Uses the Whapi.cloud REST API — simple HTTP/JSON, no SDK needed.
    Authentication via Bearer token from the Whapi.cloud dashboard.
    """

    def __init__(self, api_token: str, webhook_secret: str = ""):
        self.api_token = api_token
        self.webhook_secret = webhook_secret
        self._api_base = WHAPI_API_BASE

    def channel_type(self) -> ChannelType:
        return ChannelType.WHATSAPP

    def capabilities(self) -> set[ChannelCapability]:
        return {
            ChannelCapability.RICH_TEXT,
            ChannelCapability.FILE_UPLOAD,
            ChannelCapability.FILE_RECEIVE,
        }

    def _headers(self, content_type: str = "application/json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": content_type,
            "User-Agent": "T3nets/1.0",
        }

    def parse_inbound(self, raw_event: dict[str, Any]) -> InboundMessage:
        """
        Parse a Whapi.cloud webhook event into an InboundMessage.

        Whapi.cloud webhook payload:
            {
                "messages": [{
                    "id": "msg_id",
                    "from": "1234567890@s.whatsapp.net",
                    "chat_id": "1234567890@s.whatsapp.net",
                    "type": "text",
                    "text": {"body": "hello"},
                    "from_name": "John Doe",
                    "timestamp": 1234567890
                }]
            }
        """
        messages = raw_event.get("messages", [])
        msg = messages[0] if messages else {}

        from_id = msg.get("from", "unknown")
        chat_id = msg.get("chat_id", from_id)
        from_name = msg.get("from_name", "")
        display_name = from_name or from_id.split("@")[0]

        # Extract text body
        text = ""
        if msg.get("type") == "text":
            text = msg.get("text", {}).get("body", "")

        return InboundMessage(
            channel=ChannelType.WHATSAPP,
            channel_user_id=from_id,
            user_display_name=display_name,
            user_email=None,
            conversation_id=chat_id,
            text=text.strip(),
            raw_event=raw_event,
            metadata={
                "message_id": msg.get("id", ""),
                "message_type": msg.get("type", ""),
                "chat_id": chat_id,
            },
            timestamp=str(msg.get("timestamp", "")),
        )

    async def send_response(self, message: OutboundMessage) -> bool:
        """
        Send a response via Whapi.cloud API.

        If the message contains an audio attachment, sends via /messages/voice.
        Otherwise sends text via /messages/text.
        """
        audio_attachment = next((a for a in message.attachments if a.get("type") == "audio"), None)
        if audio_attachment:
            return await self._send_audio(message, audio_attachment)

        payload = {
            "to": message.conversation_id,
            "body": message.text,
        }

        try:
            data = json.dumps(payload).encode()
            req = Request(
                f"{self._api_base}/messages/text",
                data=data,
                headers=self._headers(),
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                if result.get("sent"):
                    logger.info(f"Sent WhatsApp text to {message.conversation_id}")
                    return True
                else:
                    logger.error(f"Whapi.cloud send error: {result}")
                    return False
        except Exception as e:
            error_detail = ""
            if hasattr(e, "read"):
                try:
                    error_detail = e.read().decode()
                except Exception:
                    pass
            logger.error(
                f"Failed to send WhatsApp message: {e}"
                + (f" — {error_detail}" if error_detail else "")
            )
            return False

    async def _send_audio(self, message: OutboundMessage, audio: dict[str, Any]) -> bool:
        """Send voice message via Whapi.cloud /messages/voice.

        Whapi.cloud accepts a media URL and auto-converts to OGG/Opus for WhatsApp.
        If only base64 is available (no URL), falls back to text-only.
        """
        audio_url = audio.get("audio_url", "")

        if not audio_url:
            # Whapi.cloud voice endpoint requires a URL — can't send inline base64
            logger.warning("No audio_url for WhatsApp voice — falling back to text")
            if message.text:
                # Send just the text caption
                text_msg = OutboundMessage(
                    channel=ChannelType.WHATSAPP,
                    conversation_id=message.conversation_id,
                    recipient_id=message.recipient_id,
                    text=message.text,
                    attachments=[],
                )
                return await self.send_response(text_msg)
            return False

        payload: dict[str, Any] = {
            "to": message.conversation_id,
            "media": audio_url,
        }

        try:
            data = json.dumps(payload).encode()
            req = Request(
                f"{self._api_base}/messages/voice",
                data=data,
                headers=self._headers(),
                method="POST",
            )
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                if result.get("sent"):
                    logger.info(f"Sent WhatsApp voice to {message.conversation_id}")
                    # Also send caption text if present
                    if message.text:
                        caption_msg = OutboundMessage(
                            channel=ChannelType.WHATSAPP,
                            conversation_id=message.conversation_id,
                            recipient_id=message.recipient_id,
                            text=message.text,
                            attachments=[],
                        )
                        await self.send_response(caption_msg)
                    return True
                else:
                    logger.error(f"Whapi.cloud voice error: {result}")
                    return False
        except Exception as e:
            error_detail = ""
            if hasattr(e, "read"):
                try:
                    error_detail = e.read().decode()
                except Exception:
                    pass
            logger.error(
                f"Failed to send WhatsApp voice: {e}"
                + (f" — {error_detail}" if error_detail else "")
            )
            return False

    def validate_webhook(self, headers: dict[str, Any], body: bytes) -> bool:
        """
        Validate incoming Whapi.cloud webhook.

        Uses a custom header X-Whapi-Secret set during webhook registration.
        """
        if not self.webhook_secret:
            return True

        header_secret = headers.get("X-Whapi-Secret", "") or headers.get("x-whapi-secret", "")
        return hmac.compare_digest(header_secret, self.webhook_secret)

    # --- Setup helpers ---

    def register_webhook(self, webhook_url: str) -> dict[str, Any]:
        """
        Register the webhook URL with Whapi.cloud.

        Call this when the user saves WhatsApp integration config.
        PATCH https://gate.whapi.cloud/settings
        """
        webhook_config: dict[str, Any] = {
            "url": webhook_url,
            "events": [{"type": "messages", "method": "post"}],
        }
        if self.webhook_secret:
            webhook_config["custom_headers"] = [
                {"name": "X-Whapi-Secret", "value": self.webhook_secret}
            ]

        payload = {"webhooks": [webhook_config]}

        try:
            data = json.dumps(payload).encode()
            req = Request(
                f"{self._api_base}/settings",
                data=data,
                headers=self._headers(),
                method="PATCH",
            )
            with urlopen(req, timeout=10) as resp:
                result = cast(dict[str, Any], json.loads(resp.read().decode()))
                logger.info(f"Whapi.cloud webhook registered: {webhook_url}")
                return result
        except Exception as e:
            error_detail = ""
            if hasattr(e, "read"):
                try:
                    error_detail = e.read().decode()
                except Exception:
                    pass
            logger.error(
                f"Failed to register Whapi.cloud webhook: {e}"
                + (f" — {error_detail}" if error_detail else "")
            )
            return {"error": str(e)}

    def get_health(self) -> dict[str, Any]:
        """
        Verify API token by calling GET /health.

        Returns health info if token is valid.
        Useful for testing the connection.
        """
        try:
            req = Request(
                f"{self._api_base}/health",
                headers=self._headers(),
                method="GET",
            )
            with urlopen(req, timeout=10) as resp:
                return cast(dict[str, Any], json.loads(resp.read().decode()))
        except Exception as e:
            error_detail = ""
            if hasattr(e, "read"):
                try:
                    error_detail = e.read().decode()
                except Exception:
                    pass
            return {"error": str(e) + (f" — {error_detail}" if error_detail else "")}

    # --- Static helpers ---

    @staticmethod
    def is_message_event(event: dict[str, Any]) -> bool:
        """Check if a webhook event contains an inbound user text message.

        Filters out:
        - Non-text messages (images, stickers, etc.)
        - Messages sent by the connected account (from_me=true) to prevent loops
        """
        messages = event.get("messages", [])
        if not messages:
            return False
        msg = messages[0]
        # Skip messages sent by the bot itself — prevents infinite loops
        if msg.get("from_me", False):
            return False
        return msg.get("type") == "text" and bool(msg.get("text", {}).get("body", "").strip())
