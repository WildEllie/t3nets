"""
Tests for t3nets_sdk.testing — the in-memory Mock* doubles and make_test_context.

These verify the test doubles satisfy the interface contracts they implement
and behave correctly around tenant isolation, JSON convenience helpers,
prefix listing, BlobNotFoundError / SecretNotFoundError semantics, and event capture.
"""

import pytest
from t3nets_sdk.interfaces.blob_store import BlobNotFoundError, BlobStore
from t3nets_sdk.interfaces.conversation_store import ConversationStore
from t3nets_sdk.interfaces.event_bus import EventBus
from t3nets_sdk.interfaces.secrets_provider import SecretNotFoundError, SecretsProvider
from t3nets_sdk.models.context import RequestContext
from t3nets_sdk.models.message import ChannelType
from t3nets_sdk.models.tenant import Tenant, TenantSettings
from t3nets_sdk.testing import (
    MockBlobStore,
    MockConversationStore,
    MockEventBus,
    MockSecretsProvider,
    PublishedEvent,
    make_test_context,
)

# ---------- MockBlobStore ----------


class TestMockBlobStore:
    def test_is_a_blob_store(self) -> None:
        assert isinstance(MockBlobStore(), BlobStore)

    async def test_put_and_get_bytes_roundtrip(self) -> None:
        store = MockBlobStore()
        await store.put("acme", "memory/jan.bin", b"hello")
        assert await store.get("acme", "memory/jan.bin") == b"hello"

    async def test_get_missing_raises_blob_not_found(self) -> None:
        store = MockBlobStore()
        with pytest.raises(BlobNotFoundError):
            await store.get("acme", "missing")

    async def test_json_helpers_roundtrip(self) -> None:
        store = MockBlobStore()
        await store.put_json("acme", "config.json", {"a": 1, "nested": {"b": 2}})
        assert await store.get_json("acme", "config.json") == {"a": 1, "nested": {"b": 2}}

    async def test_tenant_isolation(self) -> None:
        store = MockBlobStore()
        await store.put("acme", "shared", b"acme-data")
        await store.put("globex", "shared", b"globex-data")
        assert await store.get("acme", "shared") == b"acme-data"
        assert await store.get("globex", "shared") == b"globex-data"

    async def test_list_keys_with_prefix_filters_to_tenant(self) -> None:
        store = MockBlobStore()
        await store.put("acme", "memory/a", b"1")
        await store.put("acme", "memory/b", b"2")
        await store.put("acme", "exports/c", b"3")
        await store.put("globex", "memory/leak", b"4")

        memory = await store.list_keys("acme", prefix="memory/")
        assert memory == ["memory/a", "memory/b"]

        all_acme = await store.list_keys("acme")
        assert all_acme == ["exports/c", "memory/a", "memory/b"]

    async def test_delete_is_idempotent(self) -> None:
        store = MockBlobStore()
        await store.put("acme", "k", b"v")
        await store.delete("acme", "k")
        await store.delete("acme", "k")  # second delete should not raise
        with pytest.raises(BlobNotFoundError):
            await store.get("acme", "k")

    async def test_clear_helper_drops_everything(self) -> None:
        store = MockBlobStore()
        await store.put("acme", "k", b"v")
        store.clear()
        assert len(store) == 0


# ---------- MockSecretsProvider ----------


class TestMockSecretsProvider:
    def test_is_a_secrets_provider(self) -> None:
        assert isinstance(MockSecretsProvider(), SecretsProvider)

    async def test_seed_constructor_uses_default_tenant(self) -> None:
        secrets = MockSecretsProvider({"jira": {"url": "https://x", "token": "y"}})
        result = await secrets.get("default", "jira")
        assert result == {"url": "https://x", "token": "y"}

    async def test_seed_constructor_with_explicit_tenant(self) -> None:
        secrets = MockSecretsProvider({"jira": {"token": "y"}}, tenant_id="acme")
        result = await secrets.get("acme", "jira")
        assert result == {"token": "y"}
        with pytest.raises(SecretNotFoundError):
            await secrets.get("default", "jira")

    async def test_get_missing_raises(self) -> None:
        secrets = MockSecretsProvider()
        with pytest.raises(SecretNotFoundError):
            await secrets.get("acme", "github")

    async def test_put_then_get(self) -> None:
        secrets = MockSecretsProvider()
        await secrets.put("acme", "github", {"token": "ghp_x"})
        assert await secrets.get("acme", "github") == {"token": "ghp_x"}

    async def test_get_returns_a_copy(self) -> None:
        # Mutating the returned dict must not poison the store.
        secrets = MockSecretsProvider({"jira": {"token": "y"}})
        first = await secrets.get("default", "jira")
        first["token"] = "MUTATED"
        second = await secrets.get("default", "jira")
        assert second == {"token": "y"}

    async def test_seed_dict_is_not_mutated_by_put(self) -> None:
        seed = {"jira": {"token": "y"}}
        secrets = MockSecretsProvider(seed)
        await secrets.put("default", "jira", {"token": "z"})
        assert seed == {"jira": {"token": "y"}}

    async def test_list_integrations_is_tenant_scoped(self) -> None:
        secrets = MockSecretsProvider()
        await secrets.put("acme", "jira", {})
        await secrets.put("acme", "github", {})
        await secrets.put("globex", "jira", {})
        assert await secrets.list_integrations("acme") == ["github", "jira"]
        assert await secrets.list_integrations("globex") == ["jira"]

    async def test_delete(self) -> None:
        secrets = MockSecretsProvider({"jira": {"token": "y"}})
        await secrets.delete("default", "jira")
        with pytest.raises(SecretNotFoundError):
            await secrets.get("default", "jira")


# ---------- MockEventBus ----------


class TestMockEventBus:
    def test_is_an_event_bus(self) -> None:
        assert isinstance(MockEventBus(), EventBus)

    async def test_publish_records_event(self) -> None:
        bus = MockEventBus()
        await bus.publish("agent.router", "skill.invoke", {"skill_name": "ping"})
        assert len(bus) == 1
        assert bus.events[0] == PublishedEvent(
            source="agent.router",
            detail_type="skill.invoke",
            detail={"skill_name": "ping"},
        )

    async def test_published_detail_is_defensively_copied(self) -> None:
        bus = MockEventBus()
        detail: dict[str, object] = {"a": 1}
        await bus.publish("src", "dt", detail)
        detail["a"] = 999
        assert bus.events[0].detail == {"a": 1}

    async def test_publish_skill_invocation_routes_through_publish(self) -> None:
        bus = MockEventBus()
        await bus.publish_skill_invocation(
            tenant_id="acme",
            skill_name="release_notes",
            params={"days": 7},
            session_id="sess-1",
            request_id="req-1",
            reply_channel="dashboard",
            reply_target="user-1",
        )
        assert len(bus) == 1
        event = bus.events[0]
        assert event.source == "agent.router"
        assert event.detail_type == "skill.invoke"
        assert event.detail["skill_name"] == "release_notes"
        assert event.detail["tenant_id"] == "acme"

    async def test_find_filters(self) -> None:
        bus = MockEventBus()
        await bus.publish("src.a", "type.x", {})
        await bus.publish("src.a", "type.y", {})
        await bus.publish("src.b", "type.x", {})

        assert len(bus.find(source="src.a")) == 2
        assert len(bus.find(detail_type="type.x")) == 2
        assert len(bus.find(source="src.a", detail_type="type.y")) == 1
        assert bus.find(source="missing") == []

    async def test_last_and_clear(self) -> None:
        bus = MockEventBus()
        assert bus.last() is None
        await bus.publish("s", "t", {"n": 1})
        await bus.publish("s", "t", {"n": 2})
        last = bus.last()
        assert last is not None
        assert last.detail == {"n": 2}
        bus.clear()
        assert bus.last() is None
        assert len(bus) == 0


# ---------- MockConversationStore ----------


class TestMockConversationStore:
    def test_is_a_conversation_store(self) -> None:
        assert isinstance(MockConversationStore(), ConversationStore)

    async def test_save_turn_appends_user_then_assistant(self) -> None:
        store = MockConversationStore()
        await store.save_turn("acme", "conv-1", "hi", "hello")
        history = await store.get_conversation("acme", "conv-1")
        assert history == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

    async def test_save_turn_with_metadata_attaches_to_assistant(self) -> None:
        store = MockConversationStore()
        await store.save_turn("acme", "conv-1", "hi", "hello", metadata={"tokens": 5})
        history = await store.get_conversation("acme", "conv-1")
        assert history[1]["metadata"] == {"tokens": 5}

    async def test_max_turns_truncates_to_most_recent(self) -> None:
        store = MockConversationStore()
        for i in range(5):
            await store.save_turn("acme", "conv-1", f"q{i}", f"a{i}")

        # max_turns=2 → 2 turns × 2 messages = 4 messages, the most recent ones
        history = await store.get_conversation("acme", "conv-1", max_turns=2)
        assert [m["content"] for m in history] == ["q3", "a3", "q4", "a4"]

    async def test_max_turns_zero_returns_empty(self) -> None:
        store = MockConversationStore()
        await store.save_turn("acme", "conv-1", "q", "a")
        assert await store.get_conversation("acme", "conv-1", max_turns=0) == []

    async def test_get_returns_a_copy(self) -> None:
        store = MockConversationStore()
        await store.save_turn("acme", "conv-1", "q", "a")
        history = await store.get_conversation("acme", "conv-1")
        history[0]["content"] = "MUTATED"
        # Re-fetch to confirm the store wasn't poisoned
        again = await store.get_conversation("acme", "conv-1")
        assert again[0]["content"] == "q"

    async def test_tenant_isolation(self) -> None:
        store = MockConversationStore()
        await store.save_turn("acme", "conv-1", "q-acme", "a-acme")
        await store.save_turn("globex", "conv-1", "q-globex", "a-globex")
        acme = await store.get_conversation("acme", "conv-1")
        globex = await store.get_conversation("globex", "conv-1")
        assert acme[0]["content"] == "q-acme"
        assert globex[0]["content"] == "q-globex"

    async def test_clear_conversation(self) -> None:
        store = MockConversationStore()
        await store.save_turn("acme", "conv-1", "q", "a")
        await store.clear_conversation("acme", "conv-1")
        assert await store.get_conversation("acme", "conv-1") == []

    async def test_get_unknown_conversation_returns_empty(self) -> None:
        store = MockConversationStore()
        assert await store.get_conversation("acme", "nope") == []


# ---------- make_test_context ----------


class TestMakeTestContext:
    def test_default_context_is_well_formed(self) -> None:
        ctx = make_test_context()
        assert isinstance(ctx, RequestContext)
        assert ctx.tenant_id == "test-tenant"
        assert ctx.user_id == "test-user"
        assert ctx.user.email == "test@example.com"
        assert ctx.channel == ChannelType.DASHBOARD
        assert ctx.conversation_id == "test-conversation"
        # request_id and timestamp are auto-generated and non-empty
        assert ctx.request_id
        assert ctx.timestamp

    def test_overrides_propagate(self) -> None:
        ctx = make_test_context(
            tenant_id="acme",
            user_id="alice",
            user_email="alice@acme.com",
            user_display_name="Alice",
            channel=ChannelType.TELEGRAM,
            conversation_id="tg-42",
        )
        assert ctx.tenant_id == "acme"
        assert ctx.user.tenant_id == "acme"  # tenant_id flows into user
        assert ctx.user.email == "alice@acme.com"
        assert ctx.user.display_name == "Alice"
        assert ctx.channel == ChannelType.TELEGRAM
        assert ctx.conversation_id == "tg-42"

    def test_explicit_tenant_overrides_tenant_id_arg(self) -> None:
        explicit = Tenant(tenant_id="explicit", name="Explicit Inc")
        ctx = make_test_context(tenant_id="ignored", tenant=explicit)
        assert ctx.tenant is explicit
        assert ctx.tenant_id == "explicit"

    def test_settings_argument_lands_on_default_tenant(self) -> None:
        settings = TenantSettings(enabled_skills=["ping", "release_notes"])
        ctx = make_test_context(settings=settings)
        assert ctx.tenant.settings.enabled_skills == ["ping", "release_notes"]

    def test_log_prefix_is_usable(self) -> None:
        ctx = make_test_context()
        prefix = ctx.log_prefix()
        assert "test-tenant" in prefix
        assert "Test User" in prefix
