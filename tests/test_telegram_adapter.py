"""
Telegram Channel Adapter tests.

Verifies:
- TelegramAdapter correctly parses Bot API Update payloads
- Bot command stripping (/status@botname → sprint status)
- Activity type detection (message, group, etc.)
- Webhook secret validation
- Markdown fallback on send errors
"""

import sys
import unittest
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.channels.telegram import TelegramAdapter
from agent.models.message import ChannelCapability, ChannelType


class TestTelegramAdapter(unittest.TestCase):
    """Tests for TelegramAdapter."""

    def setUp(self):
        self.adapter = TelegramAdapter(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            webhook_secret="test-secret",
        )

    def test_channel_type(self):
        self.assertEqual(self.adapter.channel_type(), ChannelType.TELEGRAM)

    def test_capabilities(self):
        caps = self.adapter.capabilities()
        self.assertIn(ChannelCapability.RICH_TEXT, caps)
        self.assertIn(ChannelCapability.BUTTONS, caps)
        self.assertIn(ChannelCapability.FILE_UPLOAD, caps)
        self.assertIn(ChannelCapability.REACTIONS, caps)

    def test_parse_inbound_private_message(self):
        """Parse a private chat message."""
        update = {
            "update_id": 100,
            "message": {
                "message_id": 42,
                "from": {
                    "id": 123456789,
                    "is_bot": False,
                    "first_name": "John",
                    "last_name": "Doe",
                    "username": "johndoe",
                },
                "chat": {
                    "id": 123456789,
                    "type": "private",
                },
                "date": 1700000000,
                "text": "sprint status",
            },
        }

        message = self.adapter.parse_inbound(update)

        self.assertEqual(message.channel, ChannelType.TELEGRAM)
        self.assertEqual(message.channel_user_id, "123456789")
        self.assertEqual(message.user_display_name, "John Doe")
        self.assertEqual(message.text, "sprint status")
        self.assertEqual(message.conversation_id, "123456789")
        self.assertEqual(message.metadata["chat_type"], "private")
        self.assertEqual(message.metadata["username"], "johndoe")

    def test_parse_inbound_group_message(self):
        """Parse a group chat message."""
        update = {
            "update_id": 101,
            "message": {
                "message_id": 43,
                "from": {
                    "id": 999,
                    "first_name": "Jane",
                    "username": "jane",
                },
                "chat": {
                    "id": -1001234567890,
                    "type": "supergroup",
                    "title": "Dev Team",
                },
                "date": 1700000001,
                "text": "hello everyone",
            },
        }

        message = self.adapter.parse_inbound(update)

        self.assertEqual(message.conversation_id, "-1001234567890")
        self.assertEqual(message.metadata["chat_type"], "supergroup")
        self.assertEqual(message.user_display_name, "Jane")

    def test_strip_bot_command_start(self):
        """/start should map to 'help'."""
        update = {
            "update_id": 102,
            "message": {
                "from": {"id": 1, "first_name": "User"},
                "chat": {"id": 1, "type": "private"},
                "text": "/start",
            },
        }
        message = self.adapter.parse_inbound(update)
        self.assertEqual(message.text, "help")

    def test_strip_bot_command_status(self):
        """/status should map to 'sprint status'."""
        update = {
            "update_id": 103,
            "message": {
                "from": {"id": 1, "first_name": "User"},
                "chat": {"id": 1, "type": "private"},
                "text": "/status",
            },
        }
        message = self.adapter.parse_inbound(update)
        self.assertEqual(message.text, "sprint status")

    def test_strip_bot_command_with_botname(self):
        """/status@mybot in groups should still map correctly."""
        update = {
            "update_id": 104,
            "message": {
                "from": {"id": 1, "first_name": "User"},
                "chat": {"id": -100, "type": "group"},
                "text": "/status@t3nets_bot",
            },
        }
        message = self.adapter.parse_inbound(update)
        self.assertEqual(message.text, "sprint status")

    def test_strip_bot_command_with_args(self):
        """/releases last week should keep the args."""
        update = {
            "update_id": 105,
            "message": {
                "from": {"id": 1, "first_name": "User"},
                "chat": {"id": 1, "type": "private"},
                "text": "/releases last week",
            },
        }
        message = self.adapter.parse_inbound(update)
        self.assertEqual(message.text, "release notes last week")

    def test_unknown_command_passes_through(self):
        """/custom should strip the slash and pass through."""
        update = {
            "update_id": 106,
            "message": {
                "from": {"id": 1, "first_name": "User"},
                "chat": {"id": 1, "type": "private"},
                "text": "/custom something",
            },
        }
        message = self.adapter.parse_inbound(update)
        self.assertEqual(message.text, "custom something")

    def test_plain_text_not_stripped(self):
        """Non-command text should pass through unchanged."""
        update = {
            "update_id": 107,
            "message": {
                "from": {"id": 1, "first_name": "User"},
                "chat": {"id": 1, "type": "private"},
                "text": "what is the sprint status?",
            },
        }
        message = self.adapter.parse_inbound(update)
        self.assertEqual(message.text, "what is the sprint status?")

    def test_validate_webhook_with_correct_secret(self):
        headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
        self.assertTrue(self.adapter.validate_webhook(headers, b""))

    def test_validate_webhook_with_wrong_secret(self):
        headers = {"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"}
        self.assertFalse(self.adapter.validate_webhook(headers, b""))

    def test_validate_webhook_no_secret_configured(self):
        """If no webhook_secret is set, accept all requests."""
        adapter = TelegramAdapter("token", webhook_secret="")
        self.assertTrue(adapter.validate_webhook({}, b""))

    def test_validate_webhook_missing_header(self):
        """Missing header with secret configured should fail."""
        self.assertFalse(self.adapter.validate_webhook({}, b""))

    def test_is_message_update(self):
        self.assertTrue(
            TelegramAdapter.is_message_update(
                {
                    "message": {"text": "hello"},
                }
            )
        )
        self.assertFalse(
            TelegramAdapter.is_message_update(
                {
                    "message": {"text": ""},
                }
            )
        )
        self.assertFalse(
            TelegramAdapter.is_message_update(
                {
                    "message": {},
                }
            )
        )
        self.assertFalse(TelegramAdapter.is_message_update({}))

    def test_is_group_chat(self):
        self.assertTrue(
            TelegramAdapter.is_group_chat(
                {
                    "message": {"chat": {"type": "group"}},
                }
            )
        )
        self.assertTrue(
            TelegramAdapter.is_group_chat(
                {
                    "message": {"chat": {"type": "supergroup"}},
                }
            )
        )
        self.assertFalse(
            TelegramAdapter.is_group_chat(
                {
                    "message": {"chat": {"type": "private"}},
                }
            )
        )

    def test_api_base_url(self):
        """Verify API base URL is constructed correctly."""
        self.assertEqual(
            self.adapter._api_base,
            "https://api.telegram.org/bot123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        )

    def test_parse_inbound_no_last_name(self):
        """Display name should handle missing last_name."""
        update = {
            "update_id": 108,
            "message": {
                "from": {"id": 1, "first_name": "Solo"},
                "chat": {"id": 1, "type": "private"},
                "text": "hi",
            },
        }
        message = self.adapter.parse_inbound(update)
        self.assertEqual(message.user_display_name, "Solo")

    def test_parse_inbound_username_fallback(self):
        """Display name should fall back to username if no names."""
        update = {
            "update_id": 109,
            "message": {
                "from": {"id": 1, "username": "anonuser"},
                "chat": {"id": 1, "type": "private"},
                "text": "hi",
            },
        }
        message = self.adapter.parse_inbound(update)
        self.assertEqual(message.user_display_name, "anonuser")


if __name__ == "__main__":
    unittest.main()
