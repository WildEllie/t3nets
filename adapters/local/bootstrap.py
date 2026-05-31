"""Local dev server bootstrap — constructs the LocalServerState.

Mirrors adapters/aws/bootstrap.py but for SQLite + Anthropic + .env.
"""

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adapters.local.admin_api import LocalAdminAPI
from adapters.local.anthropic_provider import AnthropicProvider
from adapters.local.direct_bus import DirectBus
from adapters.local.env_secrets import EnvSecretsProvider
from adapters.local.file_blob_store import FileStore
from adapters.local.local_pending_store import LocalPendingStore
from adapters.local.platform_api import LocalPlatformAPI
from adapters.local.server_helpers import (
    api_key_preview,
    enrich_match_params,
    get_teams_adapter_local,
    get_telegram_adapter_local,
    resolve_auth_single_tenant,
    resolve_model,
)
from adapters.local.sqlite_rule_store import SQLiteRuleStore
from adapters.local.sqlite_store import SQLiteConversationStore
from adapters.local.sqlite_tenant_store import SQLiteTenantStore
from adapters.local.sqlite_training_store import SQLiteTrainingStore
from adapters.ollama.provider import OllamaProvider
from adapters.shared.handlers.chat import ChatHandlers
from adapters.shared.handlers.health import HealthHandlers
from adapters.shared.handlers.history import HistoryHandlers
from adapters.shared.handlers.integrations import IntegrationHandlers
from adapters.shared.handlers.practices import PracticeHandlers
from adapters.shared.handlers.settings import SettingsHandlers
from adapters.shared.handlers.training import TrainingHandlers
from adapters.shared.handlers.webhooks import WebhookHandlers
from adapters.shared.multi_provider import MultiAIProvider
from agent.channels.base import ChannelRegistry
from agent.channels.dashboard import DashboardAdapter
from agent.errors.handler import ErrorHandler
from agent.models.ai_models import DEFAULT_MODEL_ID, get_model
from agent.practices.registry import PracticeRegistry
from agent.router.compiled_engine import CompiledRuleEngine
from agent.skills.registry import SkillRegistry
from agent.sse import SSEConnectionManager

logger = logging.getLogger("t3nets.local.bootstrap")

DEFAULT_TENANT = "local"
DEFAULT_CONVERSATION = "dashboard-default"


@dataclass
class LocalServerState:
    """Container for runtime state of the local dev server."""

    # Config
    default_tenant: str = DEFAULT_TENANT
    default_conversation: str = DEFAULT_CONVERSATION
    platform: str = "local"
    stage: str = "dev"
    build_number: str = "0"

    # Adapters
    ai: MultiAIProvider | None = None
    memory: SQLiteConversationStore | None = None
    tenants: SQLiteTenantStore | None = None
    secrets: EnvSecretsProvider | None = None
    skills: SkillRegistry | None = None
    bus: DirectBus | None = None
    blobs: FileStore | None = None
    practices: PracticeRegistry | None = None
    pending_store: LocalPendingStore | None = None
    rule_store: SQLiteRuleStore | None = None
    training_store: SQLiteTrainingStore | None = None
    error_handler: ErrorHandler | None = None

    # APIs
    admin_api: LocalAdminAPI | None = None
    platform_api: LocalPlatformAPI | None = None

    # Handlers
    settings_handlers: SettingsHandlers | None = None
    integration_handlers: IntegrationHandlers | None = None
    chat_handlers: ChatHandlers | None = None
    history_handlers: HistoryHandlers | None = None
    training_handlers: TrainingHandlers | None = None
    health_handlers: HealthHandlers | None = None
    practice_handlers: PracticeHandlers | None = None
    webhook_handlers: WebhookHandlers | None = None

    # Push transport
    sse_manager: SSEConnectionManager = field(default_factory=SSEConnectionManager)

    # Rule routing
    compiled_engines: dict[str, CompiledRuleEngine] = field(default_factory=dict)

    # Bookkeeping
    bg_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    stats: dict[str, int] = field(
        default_factory=lambda: {
            "rule_routed": 0,
            "ai_routed": 0,
            "conversational": 0,
            "raw": 0,
            "errors": 0,
            "total_tokens": 0,
        }
    )
    started_at: float = 0.0

    def fire_and_forget(self, coro: Any) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self.bg_tasks.add(task)
        task.add_done_callback(self.bg_tasks.discard)

    def resolve_model(self, tenant: Any) -> tuple[str, str, str]:
        assert self.ai is not None
        return resolve_model(tenant, ai=self.ai)

    async def resolve_auth(self, request: Any) -> tuple[str, str]:
        return await resolve_auth_single_tenant(request, self.default_tenant)

    def enrich_match(self, match: Any, clean_text: str) -> None:
        enrich_match_params(match, clean_text, skills=self.skills)

    async def resolve_teams(self, recipient_id: str) -> Any:
        return await get_teams_adapter_local(self.default_tenant, self.secrets)

    async def resolve_telegram(self, token_hash: str) -> Any:
        return await get_telegram_adapter_local(token_hash, self.default_tenant, self.secrets)

    async def resolve_tenant_by_channel(self, channel: str, channel_key: str) -> Any:
        assert self.tenants is not None
        return await self.tenants.get_tenant(self.default_tenant)

    async def deliver_callback_via_sse(self, event_data: dict[str, Any], pending: Any) -> None:
        user_key = pending.get("user_key", "") if isinstance(pending, dict) else ""
        if user_key:
            self.sse_manager.send_event(user_key, "message", event_data)

    async def training_admin(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        method = request.method
        path = str(request.url.path)
        parts = path.rstrip("/").split("/")
        example_id = parts[4] if len(parts) > 4 else ""
        assert self.training_handlers is not None
        try:
            if method == "GET" and not example_id:
                limit = int(request.query_params.get("limit", "50"))
                unannotated = request.query_params.get("unannotated", "false").lower() == "true"
                data, status_code = await self.training_handlers.list_training(
                    self.default_tenant, limit=limit, unannotated=unannotated
                )
                return JSONResponse(data, status_code=status_code)
            if method == "PATCH" and example_id:
                body = await request.json()
                data, status_code = await self.training_handlers.annotate_training(
                    self.default_tenant, example_id, body
                )
                return JSONResponse(data, status_code=status_code)
            if method == "DELETE" and example_id:
                data, status_code = await self.training_handlers.delete_training(
                    self.default_tenant, example_id
                )
                return JSONResponse(data, status_code=status_code)
            return JSONResponse({"error": "Not found"}, status_code=404)
        except Exception as e:
            logger.exception("Training admin error")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def rules_admin(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        method = request.method
        path = str(request.url.path)
        assert self.training_handlers is not None and self.chat_handlers is not None
        try:
            if method == "POST" and path.endswith("/rebuild"):
                self.fire_and_forget(self.chat_handlers.rebuild_rules(self.default_tenant))
                data, status_code = await self.training_handlers.rebuild_rules(self.default_tenant)
                return JSONResponse(data, status_code=status_code)
            if method == "GET" and path.endswith("/status"):
                data, status_code = await self.training_handlers.rules_status(self.default_tenant)
                return JSONResponse(data, status_code=status_code)
            return JSONResponse({"error": "Not found"}, status_code=404)
        except Exception as e:
            logger.exception("Rules admin error")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def blob_upload(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        key = request.path_params["key"]
        assert self.blobs is not None
        try:
            body = await request.body()
            await self.blobs.put(self.default_tenant, key, body)
            return JSONResponse({"ok": True, "key": key, "size": len(body)})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def blob_read(self, request: Any) -> Any:
        from starlette.responses import JSONResponse, Response

        key = request.path_params["key"]
        assert self.blobs is not None
        try:
            data = await self.blobs.get(self.default_tenant, key)
            ct = "application/octet-stream"
            if key.endswith(".json"):
                ct = "application/json"
            elif key.endswith(".wav"):
                ct = "audio/wav"
            elif key.endswith(".webm"):
                ct = "audio/webm"
            elif key.endswith(".html"):
                ct = "text/html"
            return Response(data, media_type=ct)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=404)

    async def auth_me(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        assert self.tenants is not None
        tenant = await self.tenants.get_tenant(self.default_tenant)
        return JSONResponse(
            {
                "authenticated": True,
                "user_id": "local-admin",
                "tenant_id": self.default_tenant,
                "email": "admin@local.dev",
                "role": "admin",
                "tenant_status": tenant.status,
                "tenant_name": tenant.name,
            }
        )

    async def history(self, request: Any) -> Any:
        assert self.history_handlers is not None
        return await self.history_handlers.get_history(
            request, self.default_tenant, self.default_conversation
        )


# ---------------------------------------------------------------------------
# Init helpers
# ---------------------------------------------------------------------------


def _init_adapters(state: LocalServerState) -> None:
    state.secrets = EnvSecretsProvider(".env")

    ollama_url = os.getenv("OLLAMA_API_URL", "")
    _providers: dict[str, AnthropicProvider | OllamaProvider] = {}
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if api_key:
        logger.info("Using Anthropic provider (direct API)")
        _providers["anthropic"] = AnthropicProvider(api_key)
    if ollama_url:
        logger.info(f"Using Ollama provider at {ollama_url}")
        _providers["ollama"] = OllamaProvider(base_url=ollama_url)
    if not _providers:
        logger.error(
            "No AI provider configured. Set ANTHROPIC_API_KEY or OLLAMA_API_URL "
            "in .env. For free local AI: ollama serve & set OLLAMA_API_URL=http://localhost:11434"
        )
        sys.exit(1)
    state.ai = MultiAIProvider(_providers)
    state.memory = SQLiteConversationStore("data/t3nets.db")
    state.tenants = SQLiteTenantStore("data/t3nets.db")
    state.blobs = FileStore("data/blobs")
    state.pending_store = LocalPendingStore()

    state.skills = SkillRegistry()
    skills_dir = Path(__file__).parent.parent.parent / "agent" / "skills"
    state.skills.load_from_directory(skills_dir)


def _init_practices(state: LocalServerState, extra_practice_dirs: list[Path] | None) -> None:
    state.practices = PracticeRegistry()
    practices_dir = Path(__file__).parent.parent.parent / "agent" / "practices"
    state.practices.load_builtin(practices_dir)
    state.practices.load_uploaded(Path("data"))
    for extra_dir in extra_practice_dirs or []:
        state.practices.load_builtin(extra_dir)
        logger.info(f"Loaded extra practice dir: {extra_dir}")
    assert state.skills is not None
    state.practices.register_skills(state.skills)
    logger.info(f"Loaded skills: {state.skills.list_skill_names()}")
    logger.info(f"Loaded practices: {[p.name for p in state.practices.list_all()]}")


def _init_dispatch(state: LocalServerState) -> None:
    state.rule_store = SQLiteRuleStore("data/t3nets.db")
    state.training_store = SQLiteTrainingStore("data/t3nets.db")
    state.error_handler = ErrorHandler()
    assert state.skills is not None and state.secrets is not None
    state.bus = DirectBus(state.skills, state.secrets, context={"blob_store": state.blobs})

    channels = ChannelRegistry()
    channels.register(DashboardAdapter())


def _init_handlers(state: LocalServerState) -> None:
    assert state.tenants is not None and state.secrets is not None and state.skills is not None
    assert state.practices is not None and state.ai is not None and state.memory is not None
    assert state.bus is not None and state.rule_store is not None
    assert state.training_store is not None and state.error_handler is not None

    state.settings_handlers = SettingsHandlers(
        tenant_store=state.tenants,
        secrets_provider=state.secrets,
        skill_registry=state.skills,
        practice_registry=state.practices,
        active_providers=lambda: state.ai.active_providers,  # type: ignore[union-attr]
        platform=os.getenv("T3NETS_PLATFORM", "local"),
        stage=os.getenv("T3NETS_STAGE", "dev"),
        build_number=state.build_number,
        rebuild_callback=lambda tid: state.fire_and_forget(
            state.chat_handlers.rebuild_rules(tid)  # type: ignore[union-attr]
        ),
    )

    state.integration_handlers = IntegrationHandlers(secrets=state.secrets)

    state.admin_api = LocalAdminAPI(tenants=state.tenants, skills=state.skills)
    state.platform_api = LocalPlatformAPI(tenants=state.tenants, skills=state.skills)

    state.history_handlers = HistoryHandlers(conversation_store=state.memory)

    state.training_handlers = TrainingHandlers(
        training_store=state.training_store,
        rule_store=state.rule_store,
        compiled_engines=state.compiled_engines,
        rebuild_rules_fn=None,
    )

    state.health_handlers = HealthHandlers(
        tenants=state.tenants,
        secrets=state.secrets,
        skill_registry=state.skills,
        started_at=state.started_at,
        connection_count=lambda: state.sse_manager.connection_count,
        get_stats=lambda: {
            "rule_routed": state.stats["rule_routed"],
            "ai_routed": state.stats["ai_routed"],
            "conversational": state.stats["conversational"],
            "raw": state.stats["raw"],
            "errors": state.stats["errors"],
        },
        get_ai_info=lambda: {
            "providers": state.ai.active_providers,  # type: ignore[union-attr]
            "model": state.resolve_model(
                type("T", (), {"settings": type("S", (), {"ai_model": DEFAULT_MODEL_ID})()})()
            )[1],
            "api_key_preview": api_key_preview(),
            "total_tokens": state.stats["total_tokens"],
        },
        platform=os.getenv("T3NETS_PLATFORM", "local"),
        stage=os.getenv("T3NETS_STAGE", "dev"),
        default_tenant=state.default_tenant,
        connection_label="sse_connections",
    )

    state.practice_handlers = PracticeHandlers(
        practices=state.practices,
        skills=state.skills,
        blobs=state.blobs,
        tenants=state.tenants,
        secrets=state.secrets,
        pending_store=state.pending_store,
        callback_delivery=state.deliver_callback_via_sse,
    )

    state.chat_handlers = ChatHandlers(
        memory=state.memory,
        tenants=state.tenants,
        ai=state.ai,
        skills=state.skills,
        compiled_engines=state.compiled_engines,
        rule_store=state.rule_store,
        training_store=state.training_store,
        stats=state.stats,
        error_handler=state.error_handler,
        resolve_auth=state.resolve_auth,
        resolve_model=state.resolve_model,
        fire_and_forget=state.fire_and_forget,
        skill_invoker=_make_sync_skill_invoker(state),
    )

    state.webhook_handlers = WebhookHandlers(
        ai=state.ai,
        memory=state.memory,
        bus=state.bus,
        skills=state.skills,
        stats=state.stats,
        compiled_engines=state.compiled_engines,
        fallback_router=None,
        resolve_model=state.resolve_model,
        resolve_teams_adapter=state.resolve_teams,
        resolve_telegram_adapter=state.resolve_telegram,
        resolve_tenant_by_channel=state.resolve_tenant_by_channel,
        log_training=state.chat_handlers.log_training,
        enrich_match_params=state.enrich_match,
        tenant_store=state.tenants,
    )


def _make_sync_skill_invoker(state: LocalServerState) -> Any:
    async def _invoker(
        tenant_id: str,
        skill_name: str,
        params: dict[str, Any],
        conversation_id: str,
        request_id: str,
        reply_channel: str,
        reply_target: str,
        is_raw: bool = False,
        user_message: str = "",
        model_id: str = "",
        model_short_name: str = "",
    ) -> dict[str, Any] | None:
        assert state.bus is not None
        await state.bus.publish_skill_invocation(
            tenant_id,
            skill_name,
            params,
            conversation_id,
            request_id,
            reply_channel,
            reply_target,
            is_raw=is_raw,
        )
        return state.bus.get_result(request_id)

    return _invoker


async def _seed_tenants_and_engines(state: LocalServerState) -> None:
    assert state.tenants is not None and state.skills is not None and state.ai is not None
    assert state.rule_store is not None and state.chat_handlers is not None
    assert state.secrets is not None

    tenant = state.tenants.seed_default_tenant(
        tenant_id="local",
        name="Dev",
        enabled_skills=state.skills.list_skill_names(),
    )
    active = state.ai.active_providers
    default = "llama-3.2-3b" if active == ["ollama"] else DEFAULT_MODEL_ID
    current_model = get_model(tenant.settings.ai_model or "")
    if (
        not tenant.settings.ai_model
        or not current_model
        or not any(p in current_model.providers for p in active)
    ):
        tenant.settings.ai_model = default
        await state.tenants.update_tenant(tenant)
    logger.info(f"Tenant: {tenant.name} (skills: {tenant.settings.enabled_skills})")

    acme = state.tenants.seed_default_tenant(
        tenant_id="acme",
        name="Acme Corp",
        admin_email="admin@acme.dev",
        admin_name="Acme Admin",
        enabled_skills=["sprint_status", "ping"],
    )
    logger.info(f"Tenant: {acme.name} (skills: {acme.settings.enabled_skills})")

    connected = await state.secrets.list_integrations("local")
    logger.info(f"Connected integrations: {connected}")

    for t in [tenant, acme]:
        cached = await state.rule_store.load_rule_set(t.tenant_id)
        if cached:
            state.compiled_engines[t.tenant_id] = CompiledRuleEngine(cached, state.skills)
            logger.info(
                f"Loaded rule engine for '{t.tenant_id}' "
                f"(v{cached.version}, generated {cached.generated_at[:10]})"
            )
        else:
            logger.info(f"No rules cached for '{t.tenant_id}' -- generating via AI...")
            await state.chat_handlers.rebuild_rules(t.tenant_id)


def _read_build_number() -> str:
    path = Path(__file__).resolve().parent.parent.parent / "version.txt"
    return path.read_text().strip() if path.exists() else "0"


async def init(extra_practice_dirs: list[Path] | None = None) -> LocalServerState:
    """Construct and wire all runtime components for the local dev server."""
    state = LocalServerState(
        build_number=_read_build_number(),
        started_at=time.time(),
    )
    _init_adapters(state)
    _init_practices(state, extra_practice_dirs)
    _init_dispatch(state)
    _init_handlers(state)
    await _seed_tenants_and_engines(state)
    return state
