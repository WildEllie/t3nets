"""
Telegram Channel Adapter.

Receives messages from Telegram via Bot API webhook and sends
responses via the sendMessage API. Simplest channel to set up:
  1. Message @BotFather in Telegram → /newbot → get token
  2. Save token in T3nets settings
  3. T3nets auto-registers the webhook on save

Telegram Bot API docs: https://core.telegram.org/bots/api
"""

import hashlib
import hmac
import json
import logging
from urllib.request import urlopen, Request

from agent.channels.base import ChannelAdapter
from agent.models.message import (
    ChannelType,
    ChannelCapability,
    InboundMessage,
    OutboundMessage,
)

logger = logging.getLogger("t3nets.telegram")

TELEGRAM_API_BASE = "https://api.telegram.org/bot"


class TelegramAdapter(ChannelAdapter):
    """
    Telegram channel adapter.

    Uses the Telegram Bot API — a simple HTTP/JSON interface.
    No SDK dependency, no OAuth, no complex auth flows.
    """

    def __init__(self, bot_token: str, webhook_secret: str = ""):
        self.bot_token = bot_token
        self.webhook_secret = webhook_secret  # Optional X-Telegram-Bot-Api-Secret-Token
        self._api_base = f"{TELEGRAM_API_BASE}{bot_token}"

    def channel_type(self) -> ChannelType:
        return ChannelType.TELEGRAM

    def capabilities(self) -> set[ChannelCapability]:
        return {
            ChannelCapability.RICH_TEXT,
            ChannelCapability.BUTTONS,
            ChannelCapability.FILE_UPLOAD,
            ChannelCapability.FILE_RECEIVE,
            ChannelCapability.REACTIONS,
        }

    def parse_inbound(self, raw_event: dict) -> InboundMessage:
        """
        Parse a Telegram Update into an InboundMessage.

        Telegram Update structure (message type):
            {
                "update_id": 123456789,
                "message": {
                    "message_id": 42,
                    "from": {
                        "id": 123456789,
                        "is_bot": false,
                        "first_name": "John",
                        "last_name": "Doe",
                        "username": "johndoe"
                    },
                    "chat": {
                        "id": -1001234567890,  # negative for groups
                        "type": "private" | "group" | "supergroup" | "channel",
                        "title": "My Group"  # for groups
                    },
                    "date": 1234567890,
                    "text": "sprint status",
                    "entities": [...]  # @mentions, commands, etc.
                }
            }
        """
        update = raw_event
        message = update.get("message", {})

        # Extract user info
        from_user = message.get("from", {})
        user_id = str(from_user.get("id", "unknown"))
        first_name = from_user.get("first_name", "")
        last_name = from_user.get("last_name", "")
        username = from_user.get("username", "")
        display_name = f"{first_name} {last_name}".strip() or username or "Telegram User"

        # Extract chat info
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        chat_type = chat.get("type", "private")

        # Extract text — strip bot command prefix if present
        text = message.get("text", "")
        text = self._strip_bot_command(text, message)

        return InboundMessage(
            channel=ChannelType.TELEGRAM,
            channel_user_id=user_id,
            user_display_name=display_name,
            user_email=None,  # Telegram doesn't provide email
            conversation_id=chat_id,
            text=text.strip(),
            raw_event=update,
            metadata={
                "chat_type": chat_type,
                "username": username,
                "message_id": str(message.get("message_id", "")),
                "update_id": str(update.get("update_id", "")),
            },
            timestamp=str(message.get("date", "")),
        )

    async def send_response(self, message: OutboundMessage) -> bool:
        """
        Send a response via Telegram sendMessage API.

        POST https://api.telegram.org/bot{token}/sendMessage
        """
        payload = {
            "chat_id": message.conversation_id,
            "text": message.text,
            "parse_mode": "Markdown",
        }

        # If replying in a group, quote the original message
        if message.metadata.get("reply_to_message_id"):
            payload["reply_to_message_id"] = message.metadata["reply_to_message_id"]

        try:
            data = json.dumps(payload).encode()
            req = Request(
                f"{self._api_base}/sendMessage",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                if result.get("ok"):
                    logger.info(f"Sent Telegram message to chat {message.conversation_id}")
                    return True
                else:
                    logger.error(f"Telegram API error: {result.get('description', 'unknown')}")
                    return False

        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            # Retry without Markdown in case of parse errors
            if "parse_mode" in payload:
                return await self._send_plain(message)
            return False

    async def _send_plain(self, message: OutboundMessage) -> bool:
        """Fallback: send without Markdown formatting."""
        payload = {
            "chat_id": message.conversation_id,
            "text": message.text,
        }
        try:
            data = json.dumps(payload).encode()
            req = Request(
                f"{self._api_base}/sendMessage",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                return result.get("ok", False)
        except Exception as e:
            logger.error(f"Plain send also failed: {e}")
            return False

    def validate_webhook(self, headers: dict, body: bytes) -> bool:
        """
        Validate incoming Telegram webhook.

        If a webhook_secret was set during setWebhook, Telegram sends it
        in the X-Telegram-Bot-Api-Secret-Token header. Simple string comparison.
        """
        if not self.webhook_secret:
            # No secret configured — accept all (common for dev)
            return True

        header_secret = (
            headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            or headers.get("x-telegram-bot-api-secret-token", "")
        )
        return hmac.compare_digest(header_secret, self.webhook_secret)

    async def send_typing_indicator(self, conversation_id: str) -> None:
        """Send 'typing...' indicator via sendChatAction."""
        payload = {
            "chat_id": conversation_id,
            "action": "typing",
        }
        try:
            data = json.dumps(payload).encode()
            req = Request(
                f"{self._api_base}/sendChatAction",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=5) as resp:
                pass
        except Exception:
            pass  # Non-critical

    # --- Setup helpers ---

    def register_webhook(self, webhook_url: str) -> dict:
        """
        Register the webhook URL with Telegram.

        Call this when the user saves Telegram integration config.
        POST https://api.telegram.org/bot{token}/setWebhook

        Returns: {"ok": true, "description": "Webhook was set"} on success
        """
        payload: dict = {"url": webhook_url}
        if self.webhook_secret:
            payload["secret_token"] = self.webhook_secret

        try:
            data = json.dumps(payload).encode()
            req = Request(
                f"{self._api_base}/setWebhook",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                if result.get("ok"):
                    logger.info(f"Telegram webhook registered: {webhook_url}")
                else:
                    logger.error(f"Telegram setWebhook failed: {result}")
                return result

        except Exception as e:
            logger.error(f"Failed to register Telegram webhook: {e}")
            return {"ok": False, "description": str(e)}

    def get_bot_info(self) -> dict:
        """
        Verify bot token by calling getMe.

        Returns bot info: {"id": 123, "first_name": "MyBot", "username": "mybot"}
        Useful for testing the connection.
        """
        try:
            req = Request(f"{self._api_base}/getMe", method="GET")
            with urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                if result.get("ok"):
                    return result.get("result", {})
                return {"error": result.get("description", "Unknown error")}
        except Exception as e:
            return {"error": str(e)}

    # --- Helpers ---

    def _strip_bot_command(self, text: str, message: dict) -> str:
        """
        Strip /command@botname prefix from the message.

        In groups, Telegram sends commands as "/status@mybot args".
        In private chats, just "/status args".
        We strip the command prefix to get the actual intent.
        """
        if not text.startswith("/"):
            return text

        # Split off the command
        parts = text.split(None, 1)  # Split on first whitespace
        command = parts[0]  # e.g., "/status@mybot"
        rest = parts[1] if len(parts) > 1 else ""

        # Remove @botname suffix from command
        if "@" in command:
            command = command.split("@")[0]

        # Map common commands to natural language
        command_map = {
            "/start": "help",
            "/help": "help",
            "/status": "sprint status",
            "/releases": "release notes",
        }

        mapped = command_map.get(command, command.lstrip("/"))
        return f"{mapped} {rest}".strip() if rest else mapped

    @staticmethod
    def is_message_update(update: dict) -> bool:
        """Check if an update contains a user message (not edited, not service)."""
        message = update.get("message", {})
        return bool(message.get("text", "").strip())

    @staticmethod
    def is_group_chat(update: dict) -> bool:
        """Check if the message is from a group or supergroup."""
        chat_type = update.get("message", {}).get("chat", {}).get("type", "")
        return chat_type in ("group", "supergroup")
