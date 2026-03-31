"""
Telegram Channel Adapter.

Receives messages from Telegram via Bot API webhook and sends
responses via the sendMessage API. Simplest channel to set up:
  1. Message @BotFather in Telegram → /newbot → get token
  2. Save token in T3nets settings
  3. T3nets auto-registers the webhook on save

Telegram Bot API docs: https://core.telegram.org/bots/api
"""

import base64
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

    def parse_inbound(self, raw_event: dict[str, Any]) -> InboundMessage:
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
        Send a response via Telegram sendMessage or sendAudio API.

        If the message contains an audio attachment, sends via sendAudio.
        Otherwise sends text via sendMessage.
        """
        # Check for audio attachment
        audio_attachment = next(
            (a for a in message.attachments if a.get("type") == "audio"), None
        )
        if audio_attachment:
            return await self._send_audio(message, audio_attachment)

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
            # Read the response body from Telegram for detailed error info
            error_detail = ""
            if hasattr(e, "read"):
                try:
                    error_detail = e.read().decode()
                except Exception:
                    pass
            logger.error(
                f"Failed to send Telegram message: {e}"
                + (f" — {error_detail}" if error_detail else "")
            )
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
                return bool(result.get("ok", False))
        except Exception as e:
            error_detail = ""
            if hasattr(e, "read"):
                try:
                    error_detail = e.read().decode()
                except Exception:
                    pass
            logger.error(
                f"Plain send also failed: {e}" + (f" — {error_detail}" if error_detail else "")
            )
            return False

    async def _send_audio(self, message: OutboundMessage, audio: dict[str, Any]) -> bool:
        """Send audio via Telegram sendAudio API (multipart/form-data).

        Supports both inline base64 (audio_b64) and presigned URL (audio_url).
        """
        audio_bytes = None

        # Try presigned URL first (large audio offloaded to S3)
        if audio.get("audio_url"):
            try:
                req = Request(audio["audio_url"], method="GET")
                with urlopen(req, timeout=30) as resp:
                    audio_bytes = resp.read()
                logger.info(f"Fetched audio from URL ({len(audio_bytes)} bytes)")
            except Exception as e:
                logger.error(f"Failed to fetch audio URL: {e}")

        # Fall back to inline base64
        if audio_bytes is None and audio.get("audio_b64"):
            try:
                audio_bytes = base64.b64decode(audio["audio_b64"])
            except Exception as e:
                logger.error(f"Failed to decode audio base64: {e}")

        if audio_bytes is None:
            logger.error("No audio data available (no URL or base64)")
            return await self._send_plain(message)

        fmt = audio.get("format", "wav")
        boundary = "----T3netsBoundary"
        body_parts: list[bytes] = []

        # chat_id field
        body_parts.append(f"--{boundary}\r\n".encode())
        body_parts.append(b'Content-Disposition: form-data; name="chat_id"\r\n\r\n')
        body_parts.append(f"{message.conversation_id}\r\n".encode())

        # audio file field
        body_parts.append(f"--{boundary}\r\n".encode())
        body_parts.append(
            f'Content-Disposition: form-data; name="audio"; filename="response.{fmt}"\r\n'
            .encode()
        )
        body_parts.append(f"Content-Type: audio/{fmt}\r\n\r\n".encode())
        body_parts.append(audio_bytes)
        body_parts.append(b"\r\n")

        # caption field (optional)
        if message.text:
            caption = message.text[:1024]  # Telegram caption limit
            body_parts.append(f"--{boundary}\r\n".encode())
            body_parts.append(b'Content-Disposition: form-data; name="caption"\r\n\r\n')
            body_parts.append(f"{caption}\r\n".encode())

        body_parts.append(f"--{boundary}--\r\n".encode())
        body_data = b"".join(body_parts)

        try:
            req = Request(
                f"{self._api_base}/sendAudio",
                data=body_data,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                if result.get("ok"):
                    logger.info(f"Sent Telegram audio to chat {message.conversation_id}")
                    return True
                else:
                    logger.error(f"Telegram sendAudio error: {result.get('description')}")
                    return False
        except Exception as e:
            logger.error(f"Failed to send Telegram audio: {e}")
            return False

    def validate_webhook(self, headers: dict[str, Any], body: bytes) -> bool:
        """
        Validate incoming Telegram webhook.

        If a webhook_secret was set during setWebhook, Telegram sends it
        in the X-Telegram-Bot-Api-Secret-Token header. Simple string comparison.
        """
        if not self.webhook_secret:
            # No secret configured — accept all (common for dev)
            return True

        header_secret = headers.get("X-Telegram-Bot-Api-Secret-Token", "") or headers.get(
            "x-telegram-bot-api-secret-token", ""
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

    def register_webhook(self, webhook_url: str) -> dict[str, Any]:
        """
        Register the webhook URL with Telegram.

        Call this when the user saves Telegram integration config.
        POST https://api.telegram.org/bot{token}/setWebhook

        Returns: {"ok": true, "description": "Webhook was set"} on success
        """
        payload: dict[str, Any] = {"url": webhook_url}
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
                result = cast(dict[str, Any], json.loads(resp.read().decode()))
                if result.get("ok"):
                    logger.info(f"Telegram webhook registered: {webhook_url}")
                else:
                    logger.error(f"Telegram setWebhook failed: {result}")
                return result

        except Exception as e:
            logger.error(f"Failed to register Telegram webhook: {e}")
            return {"ok": False, "description": str(e)}

    def get_bot_info(self) -> dict[str, Any]:
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
                    return cast(dict[str, Any], result.get("result", {}))
                return {"error": str(result.get("description", "Unknown error"))}
        except Exception as e:
            return {"error": str(e)}

    # --- Helpers ---

    def _strip_bot_command(self, text: str, message: dict[str, Any]) -> str:
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
    def is_message_update(update: dict[str, Any]) -> bool:
        """Check if an update contains a user message (not edited, not service)."""
        message = update.get("message", {})
        return bool(message.get("text", "").strip())

    @staticmethod
    def is_group_chat(update: dict[str, Any]) -> bool:
        """Check if the message is from a group or supergroup."""
        chat_type = update.get("message", {}).get("chat", {}).get("type", "")
        return chat_type in ("group", "supergroup")
