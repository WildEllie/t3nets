"""
Audio response type tests.

Verifies:
- Telegram adapter detects audio attachments and calls sendAudio
- Teams adapter includes audio attachment in Activity
- OutboundMessage carries audio via attachments convention
"""

import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.channels.telegram import TelegramAdapter
from agent.models.message import ChannelType, OutboundMessage


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestAudioAttachmentConvention(unittest.TestCase):
    """Verify that audio attachments follow the convention."""

    def test_audio_attachment_structure(self):
        """Audio attachment has required fields."""
        attachment = {
            "type": "audio",
            "audio_b64": base64.b64encode(b"\x00" * 100).decode(),
            "format": "wav",
        }
        self.assertEqual(attachment["type"], "audio")
        self.assertTrue(len(attachment["audio_b64"]) > 0)
        self.assertEqual(attachment["format"], "wav")

    def test_outbound_message_with_audio(self):
        """OutboundMessage can carry audio attachments."""
        audio = {
            "type": "audio",
            "audio_b64": base64.b64encode(b"fake-wav-data").decode(),
            "format": "wav",
        }
        msg = OutboundMessage(
            channel=ChannelType.TELEGRAM,
            conversation_id="12345",
            recipient_id="",
            text="Here is the audio",
            attachments=[audio],
        )
        self.assertEqual(len(msg.attachments), 1)
        self.assertEqual(msg.attachments[0]["type"], "audio")


class TestTelegramAudioSend(unittest.TestCase):
    """Verify TelegramAdapter handles audio attachments."""

    def setUp(self):
        self.adapter = TelegramAdapter(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        )

    def test_send_response_detects_audio(self):
        """send_response calls _send_audio when audio attachment present."""
        audio = {
            "type": "audio",
            "audio_b64": base64.b64encode(b"fake-wav").decode(),
            "format": "wav",
        }
        msg = OutboundMessage(
            channel=ChannelType.TELEGRAM,
            conversation_id="12345",
            recipient_id="",
            text="Caption text",
            attachments=[audio],
        )

        with patch.object(self.adapter, "_send_audio", return_value=True) as mock_audio:
            result = run(self.adapter.send_response(msg))
            mock_audio.assert_called_once_with(msg, audio)
            self.assertTrue(result)

    def test_send_response_text_only_no_audio(self):
        """send_response sends text when no audio attachment."""
        msg = OutboundMessage(
            channel=ChannelType.TELEGRAM,
            conversation_id="12345",
            recipient_id="",
            text="Plain text",
        )

        with patch("agent.channels.telegram.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"ok": True}).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            result = run(self.adapter.send_response(msg))
            self.assertTrue(result)

            # Should have called sendMessage, not sendAudio
            call_args = mock_urlopen.call_args
            url = call_args[0][0].full_url
            self.assertIn("sendMessage", url)


class TestTeamsAudioAttachment(unittest.TestCase):
    """Verify TeamsAdapter includes audio in Activity payload."""

    def test_audio_attachment_in_activity(self):
        """When OutboundMessage has audio attachment, Activity includes it."""
        from agent.channels.teams import TeamsAdapter

        adapter = TeamsAdapter(app_id="test-id", app_secret="test-secret")
        adapter._service_urls["conv-123"] = "https://smba.test.net"

        audio = {
            "type": "audio",
            "audio_b64": base64.b64encode(b"fake-wav").decode(),
            "format": "wav",
        }
        msg = OutboundMessage(
            channel=ChannelType.TEAMS,
            conversation_id="conv-123",
            recipient_id="",
            text="Caption",
            attachments=[audio],
        )

        with patch.object(adapter.auth, "get_bot_token", return_value="fake-token"):
            with patch("agent.channels.teams.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.read.return_value = b""
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = mock_resp

                run(adapter.send_response(msg))

                # Verify the Activity payload includes audio attachment
                call_args = mock_urlopen.call_args
                req = call_args[0][0]
                body = json.loads(req.data.decode())
                self.assertIn("attachments", body)
                self.assertEqual(body["attachments"][0]["contentType"], "audio/wav")
                self.assertTrue(body["attachments"][0]["contentUrl"].startswith("data:audio/wav"))


if __name__ == "__main__":
    unittest.main()
