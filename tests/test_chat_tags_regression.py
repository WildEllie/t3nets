"""Regression tests for chat-tag plumbing.

These guard the three regressions Ellie observed in the 2026-04-23 deploy:
    1. User command text disappeared from saved history (only timestamp).
    2. AI-model badge disappeared from the response.
    3. Worker output regressed from AI-formatted markdown to plainer text.

Each test targets a narrow seam — the contract between the shared
``ChatHandlers`` and the injected ``skill_invoker``, or the
``AsyncResultRouter``'s formatter and dashboard dispatch.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

# boto3 stubs — AWS adapters import it at module load time.
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

from t3nets_sdk.contracts import RENDER_PROMPT_KEY, TEXT_KEY  # noqa: E402

from adapters.aws.pending_requests import PendingRequest, PendingRequestsStore  # noqa: E402
from adapters.aws.result_router import AsyncResultRouter  # noqa: E402
from adapters.shared.handlers.chat import ChatHandlers  # noqa: E402
from agent.interfaces.ai_provider import AIResponse  # noqa: E402
from agent.router.compiled_engine import RouteMatch  # noqa: E402
from agent.sse import SSEConnectionManager  # noqa: E402

# ─── Test 1: ChatHandlers → skill_invoker threads user_message + model ────────


async def test_route_with_skills_passes_user_message_and_model_to_invoker():
    """Regression: async dispatcher must receive clean_text, model_id, model_short_name.

    When these args arrived empty, the AWS server's ``_chat_skill_invoker``
    stored a PendingRequest with empty ``user_message`` and empty model
    metadata. That caused:
      - ``save_turn`` to persist "" for the user's message (issue #1).
      - The SSE payload ``model`` field to be blank (issue #2).
    """
    captured: dict = {}

    async def fake_invoker(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"some": "data"}  # plain dict — no render meta → Bedrock fallback

    engine = MagicMock()
    engine.match.return_value = RouteMatch(
        skill_name="sprint_status",
        action="status",
        params={"action": "status"},
    )
    engine.supports_raw.return_value = False

    tenant = MagicMock()
    tenant.settings.enabled_skills = ["sprint_status"]

    provider_ai = MagicMock()
    provider_ai.chat = AsyncMock(
        return_value=AIResponse(text="formatted", input_tokens=5, output_tokens=7)
    )

    handlers = ChatHandlers.__new__(ChatHandlers)
    handlers._memory = MagicMock()
    handlers._tenants = MagicMock()
    handlers._ai = MagicMock()
    handlers._skills = MagicMock()
    handlers._compiled_engines = {}
    handlers._rule_store = MagicMock()
    handlers._training_store = MagicMock()
    handlers._stats = {"rule_routed": 0, "ai_routed": 0, "raw": 0}
    handlers._error_handler = MagicMock()
    handlers._resolve_auth = MagicMock()
    handlers._resolve_model = MagicMock()
    handlers._fire_and_forget = lambda _coro: None
    handlers._skill_invoker = fake_invoker
    handlers._enrich_match = None
    handlers._fallback_router = None

    await handlers._route_with_skills(
        tenant_id="default",
        user_email="ellie@t3nets.dev",
        tenant=tenant,
        engine=engine,
        clean_text="show me sprint status",
        is_raw=False,
        conversation_id="conv-1",
        history=[],
        system="sys",
        provider_ai=provider_ai,
        active_model="us.amazon.nova-micro-v1:0",
        model_short_name="Nova Micro",
        request_start=0.0,
    )

    args = captured["args"]
    # skill_invoker is called positionally; the last three args carry the
    # regression-sensitive fields.
    assert args[-3] == "show me sprint status", (
        f"user_message not threaded through: got {args[-3]!r}"
    )
    assert args[-2] == "us.amazon.nova-micro-v1:0", (
        f"model_id not threaded through: got {args[-2]!r}"
    )
    assert args[-1] == "Nova Micro", f"model_short_name not threaded through: got {args[-1]!r}"


# ─── Test 2: AsyncResultRouter._format_result runs AI when render_prompt set ──


def test_format_result_calls_ai_formatter_when_render_prompt_present():
    """Regression: render_prompt must NOT short-circuit to verbatim text.

    In the bad state, skills set ``text=...`` on SkillResult, which caused
    ``_format_result`` to return verbatim worker text without calling
    Bedrock. This lost the AI-formatted rich markdown (tables, emojis,
    contributor callouts). Now skills set ``render_prompt=...`` instead,
    and the router's formatter must actually run Bedrock.
    """
    bedrock = MagicMock()
    bedrock.chat = AsyncMock(
        return_value=AIResponse(
            text="### Sprint Alpha\n- ticket 1\n- ticket 2",
            input_tokens=40,
            output_tokens=80,
        )
    )
    ai = MagicMock()
    ai.for_provider = MagicMock(return_value=bedrock)

    sse = MagicMock(spec=SSEConnectionManager)
    pending = MagicMock(spec=PendingRequestsStore)

    router = AsyncResultRouter(
        push_client=sse,
        pending_store=pending,
        ai_provider=ai,
        bedrock_model_id="fallback-model",
    )

    pending_req = PendingRequest(
        request_id="req-fmt",
        tenant_id="default",
        skill_name="sprint_status",
        channel="dashboard",
        conversation_id="conv-1",
        reply_target="ellie@t3nets.dev",
        user_key="ellie@t3nets.dev",
        user_message="show me sprint status",
        model_id="us.amazon.nova-micro-v1:0",
        model_short_name="Nova Micro",
    )

    result: dict = {
        "sprint": {"name": "Alpha"},
        "progress": {"done": 1, "total": 3},
        RENDER_PROMPT_KEY: "Format this sprint report with headings and bullets.",
    }

    text, tokens, model = router._format_result(result, "sprint_status", pending_req)

    ai.for_provider.assert_called_once_with("bedrock")
    bedrock.chat.assert_called_once()
    chat_args, _ = bedrock.chat.call_args
    # Model id from the pending request should drive the call.
    assert chat_args[0] == "us.amazon.nova-micro-v1:0"
    # The render_prompt should be embedded in the prompt string.
    prompt_text = chat_args[2][0]["content"]
    assert "Format this sprint report with headings and bullets." in prompt_text

    assert text == "### Sprint Alpha\n- ticket 1\n- ticket 2"
    assert tokens == 120
    assert model == "Nova Micro"


def test_format_result_honors_worker_verbatim_text():
    """The verbatim path still works — text= takes precedence, zero tokens."""
    ai = MagicMock()
    sse = MagicMock(spec=SSEConnectionManager)
    pending = MagicMock(spec=PendingRequestsStore)

    router = AsyncResultRouter(
        push_client=sse, pending_store=pending, ai_provider=ai, bedrock_model_id=""
    )

    pending_req = PendingRequest(
        request_id="r",
        tenant_id="default",
        skill_name="ping",
        channel="dashboard",
        conversation_id="c",
        reply_target="u",
        user_key="u",
        model_id="m",
        model_short_name="M",
    )
    result = {"status": "ok", TEXT_KEY: "pong"}

    text, tokens, model = router._format_result(result, "ping", pending_req)

    assert text == "pong"
    assert tokens == 0
    assert model == ""
    ai.for_provider.assert_not_called()


# ─── Test 3: dashboard dispatch emits model tag + saves user_message ─────────


def test_route_dashboard_emits_model_and_saves_user_message():
    """Regression: the SSE payload and saved turn must carry model + user text.

    When the pending request stored empty user_message / model_short_name,
    the dashboard showed only a timestamp for the user bubble and no AI
    badge on the response. This test asserts both flow through.
    """
    bedrock = MagicMock()
    bedrock.chat = AsyncMock(
        return_value=AIResponse(text="formatted response", input_tokens=10, output_tokens=20)
    )
    ai = MagicMock()
    ai.for_provider = MagicMock(return_value=bedrock)

    sse = MagicMock(spec=SSEConnectionManager)
    sse.send_event.return_value = 1

    pending_store = MagicMock(spec=PendingRequestsStore)
    pending_store.get.return_value = PendingRequest(
        request_id="req-dash",
        tenant_id="default",
        skill_name="sprint_status",
        channel="dashboard",
        conversation_id="conv-dash",
        reply_target="ellie@t3nets.dev",
        user_key="ellie@t3nets.dev",
        is_raw=False,
        user_message="what's our sprint status",
        model_id="us.amazon.nova-micro-v1:0",
        model_short_name="Nova Micro",
        route_type="rule",
        created_at=1_700_000_000.0,
    )

    memory = MagicMock()
    memory.save_turn = AsyncMock()

    router = AsyncResultRouter(
        push_client=sse,
        pending_store=pending_store,
        ai_provider=ai,
        conversation_store=memory,
        bedrock_model_id="fallback",
    )

    router.handle_result(
        {
            "request_id": "req-dash",
            "reply_channel": "dashboard",
            "skill_name": "sprint_status",
            "result": {
                "sprint": {"name": "Alpha"},
                RENDER_PROMPT_KEY: "Format this clearly with headings.",
            },
        }
    )

    # SSE payload — the response bubble
    sse.send_event.assert_called_once()
    _user_key, _event_type, payload = sse.send_event.call_args[0]
    assert payload["model"] == "Nova Micro", f"SSE payload missing model badge: {payload!r}"
    assert payload["text"] == "formatted response"
    assert payload["raw"] is False

    # save_turn — the persisted conversation
    memory.save_turn.assert_called_once()
    turn_args, turn_kwargs = memory.save_turn.call_args
    # positional: (tenant_id, conversation_id, user_message, assistant_text)
    assert turn_args[2] == "what's our sprint status", (
        f"save_turn got empty user_message: {turn_args!r}"
    )
    assert turn_args[3] == "formatted response"
    metadata = turn_kwargs["metadata"]
    assert metadata["model"] == "Nova Micro", f"save_turn metadata missing model: {metadata!r}"
    assert metadata["user_email"] == "ellie@t3nets.dev"
