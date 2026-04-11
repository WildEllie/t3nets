"""
Tests for Phase 3b async skill execution components.

Tests:
    - SQS poller message processing
    - Result router channel dispatch
    - Lambda handler idempotency logic
    - Pending request store operations
"""

import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock boto3 and botocore before any AWS adapter imports
if "boto3" not in sys.modules:
    boto3_mock = ModuleType("boto3")
    boto3_mock.client = MagicMock()
    boto3_mock.resource = MagicMock()
    sys.modules["boto3"] = boto3_mock

    botocore_mock = ModuleType("botocore")
    botocore_exceptions = ModuleType("botocore.exceptions")
    botocore_exceptions.ClientError = type("ClientError", (Exception,), {})
    botocore_mock.exceptions = botocore_exceptions
    sys.modules["botocore"] = botocore_mock
    sys.modules["botocore.exceptions"] = botocore_exceptions

from adapters.aws.pending_requests import PendingRequest, PendingRequestsStore
from adapters.aws.result_router import AsyncResultRouter
from adapters.aws.sqs_poller import SQSResultPoller
from agent.sse import SSEConnectionManager

# ─── SQS Poller Tests ─────────────────────────────────────────────────


def test_poller_process_valid_message():
    """Poller should parse JSON and call callback with the body."""
    received = []
    poller = SQSResultPoller.__new__(SQSResultPoller)
    poller.callback = lambda body: received.append(body)
    poller._client = MagicMock()
    poller._client.delete_message = MagicMock()
    poller.queue_url = "https://sqs.us-east-1.amazonaws.com/123/test"

    msg = {
        "ReceiptHandle": "handle-1",
        "Body": json.dumps(
            {
                "request_id": "req-123",
                "skill_name": "ping",
                "result": {"status": "ok"},
            }
        ),
    }
    poller._process_message(msg)

    assert len(received) == 1
    assert received[0]["request_id"] == "req-123"
    poller._client.delete_message.assert_called_once()


def test_poller_process_invalid_json():
    """Poller should delete malformed JSON messages (dead letters)."""
    poller = SQSResultPoller.__new__(SQSResultPoller)
    poller.callback = MagicMock()
    poller._client = MagicMock()
    poller.queue_url = "https://sqs.us-east-1.amazonaws.com/123/test"

    msg = {"ReceiptHandle": "handle-bad", "Body": "not-json!!!"}
    poller._process_message(msg)

    poller.callback.assert_not_called()
    poller._client.delete_message.assert_called_once()


def test_poller_callback_failure_keeps_message():
    """If callback raises, message should NOT be deleted (returned to queue)."""

    def fail_callback(body):
        raise RuntimeError("Simulated failure")

    poller = SQSResultPoller.__new__(SQSResultPoller)
    poller.callback = fail_callback
    poller._client = MagicMock()
    poller.queue_url = "https://sqs.us-east-1.amazonaws.com/123/test"

    msg = {
        "ReceiptHandle": "handle-fail",
        "Body": json.dumps({"request_id": "req-fail"}),
    }
    poller._process_message(msg)

    poller._client.delete_message.assert_not_called()


# ─── Result Router Tests ──────────────────────────────────────────────


def test_router_dashboard_raw_mode():
    """Raw mode should send JSON result directly via SSE without AI formatting."""
    sse = MagicMock(spec=SSEConnectionManager)
    sse.send_event.return_value = 1

    pending = MagicMock(spec=PendingRequestsStore)
    pending.get.return_value = PendingRequest(
        request_id="req-1",
        tenant_id="default",
        skill_name="ping",
        channel="dashboard",
        conversation_id="conv-1",
        reply_target="user@test.com",
        user_key="user@test.com",
        is_raw=True,
        user_message="ping --raw",
    )

    router = AsyncResultRouter(sse, pending)
    router.handle_result(
        {
            "request_id": "req-1",
            "reply_channel": "dashboard",
            "skill_name": "ping",
            "result": {"status": "ok"},
        }
    )

    sse.send_event.assert_called_once()
    call_args = sse.send_event.call_args
    assert call_args[0][0] == "user@test.com"
    assert call_args[0][1] == "message"
    assert call_args[0][2]["raw"] is True


def test_router_dashboard_formatted_without_ai():
    """Without AI provider, should format result as markdown code block."""
    sse = MagicMock(spec=SSEConnectionManager)
    sse.send_event.return_value = 1

    pending = MagicMock(spec=PendingRequestsStore)
    pending.get.return_value = PendingRequest(
        request_id="req-2",
        tenant_id="default",
        skill_name="ping",
        channel="dashboard",
        conversation_id="conv-1",
        reply_target="user@test.com",
        user_key="user@test.com",
        is_raw=False,
        user_message="ping",
    )

    router = AsyncResultRouter(sse, pending, ai_provider=None)
    router.handle_result(
        {
            "request_id": "req-2",
            "reply_channel": "dashboard",
            "skill_name": "ping",
            "result": {"status": "ok"},
        }
    )

    sse.send_event.assert_called_once()
    call_args = sse.send_event.call_args
    data = call_args[0][2]
    assert "**ping**" in data["text"]
    assert data["raw"] is False


def test_router_error_result():
    """Error results should be formatted as friendly error messages."""
    sse = MagicMock(spec=SSEConnectionManager)
    sse.send_event.return_value = 1

    pending = MagicMock(spec=PendingRequestsStore)
    pending.get.return_value = PendingRequest(
        request_id="req-err",
        tenant_id="default",
        skill_name="jira",
        channel="dashboard",
        conversation_id="conv-1",
        reply_target="user@test.com",
        user_key="user@test.com",
        is_raw=False,
        user_message="show jira issues",
    )

    router = AsyncResultRouter(sse, pending, ai_provider=None)
    router.handle_result(
        {
            "request_id": "req-err",
            "reply_channel": "dashboard",
            "skill_name": "jira",
            "result": {"error": "API token expired"},
        }
    )

    call_args = sse.send_event.call_args
    assert "error" in call_args[0][2]["text"].lower()
    assert "API token expired" in call_args[0][2]["text"]


def test_router_unknown_channel():
    """Unknown channels should be logged but not raise."""
    sse = MagicMock(spec=SSEConnectionManager)
    pending = MagicMock(spec=PendingRequestsStore)
    pending.get.return_value = None

    router = AsyncResultRouter(sse, pending)
    # Should not raise
    router.handle_result(
        {
            "request_id": "req-x",
            "reply_channel": "slack",
            "skill_name": "ping",
            "result": {"status": "ok"},
        }
    )

    sse.send_event.assert_not_called()


def test_router_no_pending_request_dashboard():
    """Dashboard routing without pending request should handle gracefully."""
    sse = MagicMock(spec=SSEConnectionManager)

    pending = MagicMock(spec=PendingRequestsStore)
    pending.get.return_value = None  # Expired or not found

    router = AsyncResultRouter(sse, pending)
    # Should not raise — just log a warning
    router.handle_result(
        {
            "request_id": "req-expired",
            "reply_channel": "dashboard",
            "skill_name": "ping",
            "result": {"status": "ok"},
        }
    )

    sse.send_event.assert_not_called()


# ─── Pending Request Tests ────────────────────────────────────────────


def test_pending_request_dataclass():
    """PendingRequest should have correct defaults."""
    req = PendingRequest(
        request_id="test-123",
        tenant_id="default",
        skill_name="ping",
        channel="dashboard",
        conversation_id="conv-1",
        reply_target="user@test.com",
        user_key="user@test.com",
    )
    assert req.status == "pending"
    assert req.is_raw is False
    assert req.service_url == ""
    assert req.created_at == 0.0


# ─── Run all tests ───────────────────────────────────────────────────

if __name__ == "__main__":
    import traceback

    tests = [
        test_poller_process_valid_message,
        test_poller_process_invalid_json,
        test_poller_callback_failure_keeps_message,
        test_router_dashboard_raw_mode,
        test_router_dashboard_formatted_without_ai,
        test_router_error_result,
        test_router_unknown_channel,
        test_router_no_pending_request_dashboard,
        test_pending_request_dataclass,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed}/{passed + failed} tests passed")
    if failed:
        sys.exit(1)
