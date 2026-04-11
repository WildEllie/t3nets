"""
Microsoft Teams Channel Adapter tests.

Verifies:
- TeamsAdapter correctly parses Bot Framework Activity payloads
- Bot mention stripping works for group chats and channels
- Activity type detection (message, conversationUpdate, bot added)
- Service URL caching for outbound responses
- BotFrameworkAuth JWT header decoding and token caching
"""

import json
import sys
import time
import unittest
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.channels.teams import TeamsAdapter
from agent.channels.teams_auth import BotFrameworkAuth, SigningKeyCache, TokenCache
from agent.models.message import ChannelCapability, ChannelType


class TestTeamsAdapter(unittest.TestCase):
    """Tests for TeamsAdapter."""

    def setUp(self):
        self.adapter = TeamsAdapter(
            app_id="test-app-id",
            app_secret="test-app-secret",
        )

    def test_channel_type(self):
        self.assertEqual(self.adapter.channel_type(), ChannelType.TEAMS)

    def test_capabilities(self):
        caps = self.adapter.capabilities()
        self.assertIn(ChannelCapability.RICH_TEXT, caps)
        self.assertIn(ChannelCapability.BUTTONS, caps)
        self.assertIn(ChannelCapability.CARDS, caps)
        self.assertIn(ChannelCapability.THREADING, caps)
        self.assertIn(ChannelCapability.TYPING_INDICATOR, caps)

    def test_parse_inbound_personal_chat(self):
        """Parse a personal (1:1) chat message."""
        activity = {
            "type": "message",
            "id": "act-123",
            "timestamp": "2026-02-20T10:30:00.000Z",
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "channelId": "msteams",
            "from": {
                "id": "29:user-aad-id",
                "name": "John Doe",
                "aadObjectId": "aad-guid-123",
            },
            "conversation": {
                "id": "19:conv-id@thread.tacv2",
                "tenantId": "azure-tenant-id",
                "conversationType": "personal",
            },
            "recipient": {
                "id": "28:test-app-id",
                "name": "T3nets Bot",
            },
            "text": "sprint status",
            "channelData": {
                "tenant": {"id": "azure-tenant-id"},
            },
        }

        message = self.adapter.parse_inbound(activity)

        self.assertEqual(message.channel, ChannelType.TEAMS)
        self.assertEqual(message.channel_user_id, "aad-guid-123")
        self.assertEqual(message.user_display_name, "John Doe")
        self.assertEqual(message.text, "sprint status")
        self.assertEqual(message.conversation_id, "19:conv-id@thread.tacv2")
        self.assertEqual(message.metadata["conversation_type"], "personal")
        self.assertEqual(message.metadata["azure_tenant_id"], "azure-tenant-id")
        self.assertEqual(message.timestamp, "2026-02-20T10:30:00.000Z")

    def test_parse_inbound_group_chat_with_mention(self):
        """Parse a group chat message where the bot is @mentioned."""
        activity = {
            "type": "message",
            "text": "<at>T3nets Bot</at> sprint status",
            "from": {
                "id": "29:user-id",
                "name": "Jane Smith",
                "aadObjectId": "aad-guid-456",
            },
            "conversation": {
                "id": "19:group-conv@thread.tacv2",
                "conversationType": "groupChat",
            },
            "recipient": {
                "id": "28:test-app-id",
                "name": "T3nets Bot",
            },
            "entities": [
                {
                    "type": "mention",
                    "mentioned": {
                        "id": "28:test-app-id",
                        "name": "T3nets Bot",
                    },
                    "text": "<at>T3nets Bot</at>",
                }
            ],
            "channelData": {"tenant": {"id": "azure-tenant"}},
        }

        message = self.adapter.parse_inbound(activity)

        # Bot mention should be stripped
        self.assertEqual(message.text, "sprint status")
        self.assertEqual(message.metadata["conversation_type"], "groupChat")

    def test_parse_inbound_no_aad_object_id(self):
        """Fall back to from.id when aadObjectId is missing."""
        activity = {
            "type": "message",
            "text": "hello",
            "from": {"id": "29:fallback-id", "name": "User"},
            "conversation": {"id": "conv-1"},
            "channelData": {},
        }

        message = self.adapter.parse_inbound(activity)
        self.assertEqual(message.channel_user_id, "29:fallback-id")

    def test_service_url_caching(self):
        """Service URL should be cached for outbound responses."""
        activity = {
            "type": "message",
            "text": "test",
            "serviceUrl": "https://smba.trafficmanager.net/teams/",
            "from": {"id": "user-1", "name": "Test"},
            "conversation": {"id": "conv-123"},
            "channelData": {},
        }

        self.adapter.parse_inbound(activity)

        self.assertIn("conv-123", self.adapter._service_urls)
        self.assertEqual(
            self.adapter._service_urls["conv-123"],
            "https://smba.trafficmanager.net/teams",
        )

    def test_is_message_activity(self):
        self.assertTrue(TeamsAdapter.is_message_activity({"type": "message", "text": "hello"}))
        self.assertFalse(TeamsAdapter.is_message_activity({"type": "message", "text": ""}))
        self.assertFalse(TeamsAdapter.is_message_activity({"type": "conversationUpdate"}))

    def test_is_conversation_update(self):
        self.assertTrue(TeamsAdapter.is_conversation_update({"type": "conversationUpdate"}))
        self.assertFalse(TeamsAdapter.is_conversation_update({"type": "message"}))

    def test_is_bot_added(self):
        activity = {
            "type": "conversationUpdate",
            "membersAdded": [{"id": "28:test-app-id"}],
            "recipient": {"id": "28:test-app-id"},
        }
        self.assertTrue(TeamsAdapter.is_bot_added(activity))

        activity_other = {
            "type": "conversationUpdate",
            "membersAdded": [{"id": "29:some-user"}],
            "recipient": {"id": "28:test-app-id"},
        }
        self.assertFalse(TeamsAdapter.is_bot_added(activity_other))

    def test_strip_bot_mention_preserves_other_mentions(self):
        """Only the bot's mention should be stripped, not other user mentions."""
        activity = {
            "text": "<at>T3nets Bot</at> please help <at>John</at>",
            "recipient": {"id": "28:bot-id"},
            "entities": [
                {
                    "type": "mention",
                    "mentioned": {"id": "28:bot-id"},
                    "text": "<at>T3nets Bot</at>",
                },
                {
                    "type": "mention",
                    "mentioned": {"id": "29:john-id"},
                    "text": "<at>John</at>",
                },
            ],
        }

        result = self.adapter._strip_bot_mention(activity["text"], activity)
        self.assertIn("<at>John</at>", result)
        self.assertNotIn("T3nets Bot", result)


class TestBotFrameworkAuth(unittest.TestCase):
    """Tests for BotFrameworkAuth."""

    def setUp(self):
        self.auth = BotFrameworkAuth(
            app_id="test-app-id",
            app_secret="test-app-secret",
        )

    def test_validate_missing_auth_header(self):
        self.assertFalse(self.auth.validate_incoming(""))
        self.assertFalse(self.auth.validate_incoming("Basic abc"))

    def test_decode_jwt_header(self):
        """Test JWT header decoding."""
        import base64

        header = {"alg": "RS256", "kid": "test-key-id", "typ": "JWT"}
        header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        # Create a minimal JWT-like string (header.payload.signature)
        token = f"{header_b64}.eyJ0ZXN0IjoidmFsdWUifQ.fake-signature"

        decoded = self.auth._decode_jwt_header(token)
        self.assertEqual(decoded["kid"], "test-key-id")
        self.assertEqual(decoded["alg"], "RS256")

    def test_unsafe_decode_jwt_payload(self):
        """Test JWT payload decoding (dev/test mode)."""
        import base64

        payload = {
            "iss": "https://api.botframework.com",
            "aud": "test-app-id",
            "exp": int(time.time()) + 3600,
        }
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"eyJhbGciOiJSUzI1NiJ9.{payload_b64}.fake"

        decoded = self.auth._unsafe_decode_jwt_payload(token)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["iss"], "https://api.botframework.com")
        self.assertEqual(decoded["aud"], "test-app-id")


class TestTokenCache(unittest.TestCase):
    """Tests for TokenCache."""

    def test_empty_cache_is_invalid(self):
        cache = TokenCache()
        self.assertFalse(cache.is_valid())

    def test_valid_token(self):
        cache = TokenCache(
            access_token="token-123",
            expires_at=time.time() + 3600,  # 1 hour from now
        )
        self.assertTrue(cache.is_valid())

    def test_expired_token(self):
        cache = TokenCache(
            access_token="token-123",
            expires_at=time.time() - 100,  # Already expired
        )
        self.assertFalse(cache.is_valid())

    def test_near_expiry_token(self):
        """Token within 5-minute buffer should be considered invalid."""
        cache = TokenCache(
            access_token="token-123",
            expires_at=time.time() + 200,  # 3.3 min — within 5-min buffer
        )
        self.assertFalse(cache.is_valid())


class TestSigningKeyCache(unittest.TestCase):
    """Tests for SigningKeyCache."""

    def test_empty_cache_not_fresh(self):
        cache = SigningKeyCache()
        self.assertFalse(cache.is_fresh())

    def test_fresh_cache(self):
        cache = SigningKeyCache(
            keys=[{"kid": "key-1"}],
            fetched_at=time.time(),
        )
        self.assertTrue(cache.is_fresh())

    def test_stale_cache(self):
        cache = SigningKeyCache(
            keys=[{"kid": "key-1"}],
            fetched_at=time.time() - 100000,  # Way past max_age
        )
        self.assertFalse(cache.is_fresh())


if __name__ == "__main__":
    unittest.main()
