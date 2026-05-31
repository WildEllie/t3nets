"""AWS server bootstrap — constructs the ServerState that holds all runtime
adapters, handlers, and config.

server.py keeps a single module-level reference to a ServerState instance
populated by bootstrap.init(). Route handlers and helpers all read from
state rather than from per-attribute module globals.

The init helpers (`_init_*`) and closures (`_chat_skill_invoker`,
`_on_credentials_saved`, `_post_install_hook`) are kept here because they
are pure wiring — they only run during startup and own the full graph of
component construction.
"""

import asyncio
import hashlib
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.responses import Response

from adapters.aws.admin_api import AdminAPI
from adapters.aws.async_dispatch import AsyncSkillDispatcher
from adapters.aws.auth_api import AuthAPI
from adapters.aws.bedrock_provider import BedrockProvider
from adapters.aws.channel_resolvers import ChannelResolvers
from adapters.aws.dynamo_rule_store import DynamoDBRuleStore
from adapters.aws.dynamo_training_store import DynamoDBTrainingStore
from adapters.aws.dynamodb_conversation_store import DynamoDBConversationStore
from adapters.aws.dynamodb_tenant_store import DynamoDBTenantStore
from adapters.aws.event_bridge_bus import EventBridgeBus
from adapters.aws.pending_requests import PendingRequestsStore
from adapters.aws.platform_api import PlatformAPI
from adapters.aws.result_router import AsyncResultRouter
from adapters.aws.secrets_manager import SecretsManagerProvider
from adapters.aws.server_helpers import (
    enrich_match_params,
    get_auth_info,
    get_lambda_deploy_config,
    resolve_model,
)
from adapters.aws.sqs_poller import SQSResultPoller
from adapters.aws.webhook_registration import (
    register_telegram_webhook,
    register_whatsapp_webhook,
)
from adapters.aws.ws_connections import WebSocketConnectionManager
from adapters.local.direct_bus import DirectBus
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
from agent.models.ai_models import DEFAULT_MODEL_ID
from agent.practices.registry import PracticeRegistry
from agent.router.compiled_engine import CompiledRuleEngine
from agent.router.rule_router import RuleBasedRouter
from agent.skills.registry import SkillRegistry
from agent.sse import SSEConnectionManager

logger = logging.getLogger("t3nets.aws.bootstrap")

DEFAULT_TENANT = "default"


@dataclass
class ServerState:
    """Container for runtime state — populated by `init()`."""

    # Config
    aws_region: str = "us-east-1"
    bedrock_model_id: str = ""
    cognito_user_pool_id: str = ""
    use_async_skills: bool = False
    platform: str = "aws"
    stage: str = "dev"
    build_number: str = "0"
    default_tenant: str = DEFAULT_TENANT

    # Adapters
    ai: MultiAIProvider | None = None
    memory: DynamoDBConversationStore | None = None
    tenants: DynamoDBTenantStore | None = None
    secrets: SecretsManagerProvider | None = None
    skills: SkillRegistry | None = None
    bus: DirectBus | None = None
    blobs: Any = None
    practices: PracticeRegistry | None = None
    channel_resolvers: ChannelResolvers | None = None

    # Async dispatch
    event_bus: EventBridgeBus | None = None
    pending_store: PendingRequestsStore | None = None
    sqs_poller: SQSResultPoller | None = None
    result_router: AsyncResultRouter | None = None
    async_dispatch: AsyncSkillDispatcher | None = None

    # APIs
    rule_store: DynamoDBRuleStore | None = None
    training_store: DynamoDBTrainingStore | None = None
    admin_api: AdminAPI | None = None
    auth_api: AuthAPI | None = None
    platform_api: PlatformAPI | None = None
    error_handler: ErrorHandler | None = None

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
    push_client: SSEConnectionManager | WebSocketConnectionManager | None = None
    ws_manager: WebSocketConnectionManager | None = None
    sse_manager: SSEConnectionManager | None = None

    # Rule routing
    compiled_engines: dict[str, CompiledRuleEngine] = field(default_factory=dict)
    fallback_router: RuleBasedRouter | None = None

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
        """Schedule a coroutine, retaining a strong reference."""
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self.bg_tasks.add(task)
        task.add_done_callback(self.bg_tasks.discard)

    def resolve_model(self, tenant: Any) -> tuple[str, str, str]:
        assert self.ai is not None
        return resolve_model(
            tenant,
            ai=self.ai,
            aws_region=self.aws_region,
            bedrock_model_id=self.bedrock_model_id,
        )

    async def get_auth_info(self, request: Any) -> tuple[str, str]:
        return await get_auth_info(
            request,
            tenants=self.tenants,
            cognito_user_pool_id=self.cognito_user_pool_id,
            default_tenant=self.default_tenant,
        )

    def enrich_match(self, match: Any, clean_text: str) -> None:
        enrich_match_params(match, clean_text, skills=self.skills)

    async def admin_dispatch(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        method = request.method
        path = str(request.url.path)
        body = None
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            try:
                body = await request.json()
            except Exception:
                body = None
        headers = dict(request.headers)
        tenant_id, _ = await self.get_auth_info(request)
        headers["x-tenant-id"] = tenant_id
        assert self.admin_api is not None
        data, status_code = await self.admin_api.handle_request(method, path, headers, body)
        return JSONResponse(data, status_code=status_code)

    async def platform_dispatch(self, request: Any) -> Any:
        import asyncio as _asyncio

        from starlette.responses import JSONResponse

        method = request.method
        path = str(request.url.path)
        body = None
        if method in ("POST", "PUT", "PATCH"):
            body = await request.json()
        headers = dict(request.headers)
        assert self.platform_api is not None
        data, status_code = await _asyncio.to_thread(
            self.platform_api.handle_request, method, path, headers, body
        )
        return JSONResponse(data, status_code=status_code)

    async def rules_admin(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        method = request.method
        path = str(request.url.path)
        tenant_id, _ = await self.get_auth_info(request)
        assert self.chat_handlers is not None and self.training_handlers is not None
        if method == "POST" and path.endswith("/rebuild"):
            self.fire_and_forget(self.chat_handlers.rebuild_rules(tenant_id))
            data, status_code = await self.training_handlers.rebuild_rules(tenant_id)
            return JSONResponse(data, status_code=status_code)
        if method == "GET" and path.endswith("/status"):
            data, status_code = await self.training_handlers.rules_status(tenant_id)
            return JSONResponse(data, status_code=status_code)
        return JSONResponse({"error": "Not found"}, status_code=404)

    async def history(self, request: Any) -> Any:
        tenant_id, _ = await self.get_auth_info(request)
        assert self.history_handlers is not None
        return await self.history_handlers.get_history(request, tenant_id, "dashboard-default")


# ---------------------------------------------------------------------------
# Init helpers
# ---------------------------------------------------------------------------


async def _init_adapters(state: ServerState) -> None:
    conversations_table = os.getenv("DYNAMODB_CONVERSATIONS_TABLE")
    tenants_table = os.getenv("DYNAMODB_TENANTS_TABLE")
    secrets_prefix = os.getenv("SECRETS_PREFIX")
    if not all([conversations_table, tenants_table, secrets_prefix]):
        logger.error(
            "Missing required env vars: DYNAMODB_CONVERSATIONS_TABLE, "
            "DYNAMODB_TENANTS_TABLE, SECRETS_PREFIX"
        )
        sys.exit(1)
    assert conversations_table and tenants_table and secrets_prefix

    region = state.aws_region
    _providers: dict[str, BedrockProvider | OllamaProvider] = {}
    if state.bedrock_model_id:
        logger.info(f"Using Bedrock provider (model={state.bedrock_model_id})")
        _providers["bedrock"] = BedrockProvider(region=region, model_id=state.bedrock_model_id)
    ollama_url = os.environ.get("OLLAMA_API_URL", "")
    if ollama_url:
        logger.info(f"Using Ollama provider at {ollama_url}")
        _providers["ollama"] = OllamaProvider(base_url=ollama_url)
    if not _providers:
        logger.error("No AI provider configured. Set BEDROCK_MODEL_ID and/or OLLAMA_API_URL.")
        sys.exit(1)

    state.ai = MultiAIProvider(_providers)
    state.memory = DynamoDBConversationStore(conversations_table, region=region)
    state.tenants = DynamoDBTenantStore(tenants_table, region=region)
    state.secrets = SecretsManagerProvider(secrets_prefix, region=region)
    state.channel_resolvers = ChannelResolvers(state.tenants, state.secrets)

    skills_obj = SkillRegistry()
    skills_dir = Path(__file__).parent.parent.parent / "agent" / "skills"
    skills_obj.load_from_directory(skills_dir)
    state.skills = skills_obj

    try:
        from adapters.aws.s3_blob_store import S3BlobStore

        s3_bucket = os.getenv("S3_BUCKET_NAME", "t3nets-dev-static")
        state.blobs = S3BlobStore(bucket_name=s3_bucket, region=region)
        logger.info(f"S3 BlobStore: {s3_bucket}")
    except Exception as e:
        logger.warning(f"S3BlobStore init failed ({e}), blobs disabled")
        state.blobs = None


async def _init_practices(state: ServerState) -> None:
    practices_obj = PracticeRegistry()
    practices_dir = Path(__file__).parent.parent.parent / "agent" / "practices"
    practices_obj.load_builtin(practices_dir)

    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    if state.blobs and state.tenants:
        try:
            default_tenant = await state.tenants.get_tenant(state.default_tenant)
            installed_versions = default_tenant.settings.installed_practices
            restored = await practices_obj.restore_from_blob_store(
                state.blobs,
                state.default_tenant,
                data_dir,
                installed_versions=installed_versions,
            )
            if restored:
                logger.info(f"Restored {restored} practice(s) from S3")
        except Exception as e:
            logger.warning(f"Practice restore from S3 failed: {e}")

    practices_obj.load_uploaded(data_dir)
    assert state.skills is not None
    practices_obj.register_skills(state.skills)
    state.practices = practices_obj
    logger.info(f"Loaded practices: {[p.name for p in practices_obj.list_all()]}")
    logger.info(f"Loaded skills: {state.skills.list_skill_names()}")

    lambda_config = get_lambda_deploy_config()
    if lambda_config["lambda_role_arn"]:
        try:
            fixed = await practices_obj.ensure_skill_lambdas(lambda_config)
            if fixed:
                logger.info(f"Deployed {fixed} missing practice skill Lambda(s)")
        except Exception as e:
            logger.warning(f"Lambda ensure check failed: {e}")

    state.fallback_router = RuleBasedRouter(state.skills)


def _init_dispatch(state: ServerState) -> None:
    tenants_table = os.environ["DYNAMODB_TENANTS_TABLE"]
    region = state.aws_region
    state.rule_store = DynamoDBRuleStore(tenants_table, region=region)
    state.training_store = DynamoDBTrainingStore(tenants_table, region=region)
    assert state.skills is not None and state.secrets is not None and state.tenants is not None
    state.bus = DirectBus(state.skills, state.secrets)
    state.admin_api = AdminAPI(state.tenants, state.secrets, state.skills, state.training_store)
    state.platform_api = PlatformAPI(state.tenants, state.secrets, state.skills)
    state.auth_api = AuthAPI(state.tenants)
    state.error_handler = ErrorHandler()


def _init_async_dispatch(state: ServerState) -> None:
    if not state.use_async_skills:
        logger.info("Async skills DISABLED (USE_ASYNC_SKILLS=false), using DirectBus")
        return

    eb_bus_name = os.environ.get("EVENTBRIDGE_BUS_NAME", "")
    sqs_queue_url = os.environ.get("SQS_RESULTS_QUEUE_URL", "")
    pending_table = os.environ.get("PENDING_REQUESTS_TABLE", "")
    if not all([eb_bus_name, sqs_queue_url, pending_table]):
        logger.error(
            "USE_ASYNC_SKILLS=true but missing required env vars: "
            "EVENTBRIDGE_BUS_NAME, SQS_RESULTS_QUEUE_URL, PENDING_REQUESTS_TABLE"
        )
        logger.warning("Falling back to synchronous DirectBus")
        return

    region = state.aws_region
    state.event_bus = EventBridgeBus(eb_bus_name, region=region)
    state.pending_store = PendingRequestsStore(pending_table, region=region)
    state.async_dispatch = AsyncSkillDispatcher(
        event_bus=state.event_bus,
        pending_store=state.pending_store,
        stats=state.stats,
        fire_and_forget=state.fire_and_forget,
        log_training=lambda *a, **kw: state.chat_handlers.log_training(*a, **kw),  # type: ignore[union-attr]
    )
    assert state.ai is not None and state.memory is not None and state.secrets is not None
    state.result_router = AsyncResultRouter(
        push_client=state.push_client,
        pending_store=state.pending_store,
        ai_provider=state.ai,
        conversation_store=state.memory,
        bedrock_model_id=state.bedrock_model_id,
        secrets_provider=state.secrets,
    )
    state.sqs_poller = SQSResultPoller(
        queue_url=sqs_queue_url,
        callback=state.result_router.handle_result,
        region=region,
    )
    state.sqs_poller.start()
    logger.info(
        f"Async skills ENABLED: EventBridge={eb_bus_name}, "
        f"SQS={sqs_queue_url[-30:]}, Pending={pending_table}"
    )


async def _init_state(state: ServerState) -> None:
    channels = ChannelRegistry()
    channels.register(DashboardAdapter())

    assert state.tenants is not None and state.secrets is not None and state.skills is not None
    try:
        await state.tenants.get_tenant(state.default_tenant)
        logger.info(f"Tenant '{state.default_tenant}' exists")
    except Exception:
        from agent.models.tenant import Tenant, TenantSettings

        now = datetime.now(timezone.utc).isoformat()
        tenant = Tenant(
            tenant_id=state.default_tenant,
            name="T3nets Default",
            status="active",
            created_at=now,
            settings=TenantSettings(enabled_skills=state.skills.list_skill_names()),
        )
        await state.tenants.create_tenant(tenant)
        logger.info(f"Seeded tenant '{state.default_tenant}'")

    connected = await state.secrets.list_integrations(state.default_tenant)
    logger.info(f"Connected integrations: {connected}")

    assert state.rule_store is not None
    try:
        all_tenants = await state.tenants.list_tenants()
        for t in all_tenants:
            cached = await state.rule_store.load_rule_set(t.tenant_id)
            if cached:
                state.compiled_engines[t.tenant_id] = CompiledRuleEngine(cached, state.skills)
                logger.info(
                    f"Loaded rule engine for '{t.tenant_id}' "
                    f"(v{cached.version}, generated {cached.generated_at[:10]})"
                )
            else:
                logger.info(
                    f"No rule set found for tenant '{t.tenant_id}' — "
                    "AI routing will be used until rules are built via /api/admin/rules/rebuild"
                )
    except Exception:
        logger.exception("Failed to load rule engines at startup — AI routing will be used")


# ---------------------------------------------------------------------------
# Wiring closures
# ---------------------------------------------------------------------------


def _make_chat_skill_invoker(state: ServerState) -> Any:
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
    ) -> dict[str, Any] | Response | None:
        if state.use_async_skills and state.async_dispatch is not None:
            user_email = reply_target
            route_type = "rule" if request_id.startswith("rule-") else "ai"
            return await state.async_dispatch.dispatch_chat(
                tenant_id,
                user_email,
                skill_name,
                params,
                conversation_id,
                user_message,
                is_raw,
                route_type,
                model_id,
                model_short_name,
            )
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


def _make_on_credentials_saved(state: ServerState) -> Any:
    async def _hook(
        tenant_id: str,
        integration_name: str,
        merged: dict[str, Any],
    ) -> None:
        assert state.tenants is not None
        if integration_name == "telegram":
            bot_token = merged.get("bot_token", "")
            if bot_token:
                register_telegram_webhook({}, merged)
                t_hash = hashlib.sha256(bot_token.encode()).hexdigest()[:16]
                await state.tenants.set_channel_mapping(tenant_id, "telegram", t_hash)

        elif integration_name == "whatsapp":
            api_token = merged.get("api_token", "")
            if api_token:
                register_whatsapp_webhook({}, merged)
                wa_hash = hashlib.sha256(api_token.encode()).hexdigest()[:16]
                await state.tenants.set_channel_mapping(tenant_id, "whatsapp", wa_hash)

    return _hook


def _make_post_install_hook(state: ServerState) -> Any:
    async def _hook(practice_obj: Any, tenant_id: str) -> None:
        assert state.practices is not None and state.chat_handlers is not None
        lc = get_lambda_deploy_config()
        if lc["lambda_role_arn"]:
            deployed = await state.practices.deploy_skill_lambdas(practice_obj, lc)
            logger.info(f"Background: deployed Lambdas for {deployed}")

        from adapters.aws.practice_publish import publish_practice_pages

        try:
            uploaded = publish_practice_pages(
                practice_obj,
                s3_bucket=os.environ.get("S3_BUCKET_NAME", ""),
                cloudfront_distribution_id=os.environ.get("CLOUDFRONT_DISTRIBUTION_ID", ""),
                region=state.aws_region,
            )
            logger.info(f"Background: published {uploaded} practice page(s) to S3")
        except Exception as e:
            logger.error(f"Background: practice page publish failed: {e}")
        await state.chat_handlers.rebuild_rules(tenant_id)
        logger.info(f"Background: rules rebuilt for tenant {tenant_id}")

    return _hook


def _init_handlers(state: ServerState) -> None:
    assert state.tenants is not None and state.secrets is not None and state.skills is not None
    assert state.practices is not None and state.ai is not None and state.memory is not None
    assert state.bus is not None and state.rule_store is not None
    assert state.training_store is not None and state.error_handler is not None
    assert state.push_client is not None and state.channel_resolvers is not None

    state.settings_handlers = SettingsHandlers(
        tenant_store=state.tenants,
        secrets_provider=state.secrets,
        skill_registry=state.skills,
        practice_registry=state.practices,
        active_providers=lambda: state.ai.active_providers,  # type: ignore[union-attr]
        platform=state.platform,
        stage=state.stage,
        build_number=state.build_number,
        rebuild_callback=lambda tid: state.fire_and_forget(
            state.chat_handlers.rebuild_rules(tid)  # type: ignore[union-attr]
        ),
    )

    state.integration_handlers = IntegrationHandlers(
        secrets=state.secrets,
        on_credentials_saved=_make_on_credentials_saved(state),
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
        resolve_auth=state.get_auth_info,
        resolve_model=state.resolve_model,
        fire_and_forget=state.fire_and_forget,
        skill_invoker=_make_chat_skill_invoker(state),
        enrich_match=state.enrich_match,
        fallback_router=state.fallback_router,
    )

    state.history_handlers = HistoryHandlers(conversation_store=state.memory)

    state.training_handlers = TrainingHandlers(
        training_store=state.training_store,
        rule_store=state.rule_store,
        compiled_engines=state.compiled_engines,
        rebuild_rules_fn=state.chat_handlers.rebuild_rules,
    )

    state.health_handlers = HealthHandlers(
        tenants=state.tenants,
        secrets=state.secrets,
        skill_registry=state.skills,
        started_at=state.started_at,
        connection_count=lambda: state.push_client.connection_count,  # type: ignore[union-attr]
        get_stats=lambda: state.stats,
        get_ai_info=lambda: {
            "providers": state.ai.active_providers,  # type: ignore[union-attr]
            "model": state.resolve_model(
                type("T", (), {"settings": type("S", (), {"ai_model": DEFAULT_MODEL_ID})()})()
            )[1],
            "api_key_preview": "IAM role (no key)",
            "total_tokens": state.stats["total_tokens"],
        },
        platform=state.platform,
        stage=state.stage,
        default_tenant=state.default_tenant,
        connection_label="push_connections",
    )

    state.practice_handlers = PracticeHandlers(
        practices=state.practices,
        skills=state.skills,
        blobs=state.blobs,
        tenants=state.tenants,
        secrets=state.secrets,
        pending_store=state.pending_store,
        post_install_hook=_make_post_install_hook(state),
    )

    state.webhook_handlers = WebhookHandlers(
        ai=state.ai,
        memory=state.memory,
        bus=state.bus,
        skills=state.skills,
        stats=state.stats,
        compiled_engines=state.compiled_engines,
        fallback_router=state.fallback_router,
        resolve_model=state.resolve_model,
        resolve_teams_adapter=state.channel_resolvers.get_teams,
        resolve_telegram_adapter=state.channel_resolvers.get_telegram,
        resolve_whatsapp_adapter=state.channel_resolvers.get_whatsapp,
        resolve_tenant_by_channel=lambda ch, key: state.tenants.get_by_channel_id(ch, key),  # type: ignore[union-attr]
        log_training=state.chat_handlers.log_training,
        enrich_match_params=state.enrich_match,
        async_skill_handler=state.async_dispatch.dispatch_channel if state.async_dispatch else None,
        use_async_skills=state.use_async_skills,
        event_bus=state.event_bus,
        pending_store=state.pending_store,
        tenant_store=state.tenants,
    )


# ---------------------------------------------------------------------------
# Push transport
# ---------------------------------------------------------------------------


def _init_push_transport(state: ServerState) -> None:
    ws_endpoint = os.environ.get("WS_API_ENDPOINT", "")
    ws_management = os.environ.get(
        "WS_MANAGEMENT_ENDPOINT",
        ws_endpoint.replace("wss://", "https://") if ws_endpoint else "",
    )
    ws_table = os.environ.get("WS_CONNECTIONS_TABLE", "")

    if ws_management:
        ws = WebSocketConnectionManager(
            management_endpoint=ws_management,
            table_name=ws_table,
            region=state.aws_region,
        )
        state.push_client = ws
        state.ws_manager = ws
        state.sse_manager = None
        logger.info(f"Push transport: WebSocket (endpoint={ws_management[:40]}...)")
    else:
        sse = SSEConnectionManager()
        state.push_client = sse
        state.ws_manager = None
        state.sse_manager = sse
        logger.info("Push transport: SSE (no WS_MANAGEMENT_ENDPOINT configured)")


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


async def init() -> ServerState:
    """Construct and wire all runtime components. Returns the populated state."""
    state = ServerState(
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        bedrock_model_id=os.environ.get("BEDROCK_MODEL_ID", ""),
        cognito_user_pool_id=os.environ.get("COGNITO_USER_POOL_ID", ""),
        use_async_skills=os.environ.get("USE_ASYNC_SKILLS", "false").lower() == "true",
        platform=os.environ.get("T3NETS_PLATFORM", "aws"),
        stage=os.environ.get("T3NETS_STAGE", "dev"),
        build_number=_read_build_number(),
        started_at=time.time(),
    )
    _init_push_transport(state)
    await _init_adapters(state)
    await _init_practices(state)
    _init_dispatch(state)
    # _init_async_dispatch references chat_handlers via lambda; that closure
    # is evaluated at call time, so the order with _init_handlers is fine.
    _init_async_dispatch(state)
    await _init_state(state)
    _init_handlers(state)
    return state


def _read_build_number() -> str:
    path = Path(__file__).resolve().parent.parent.parent / "version.txt"
    return path.read_text().strip() if path.exists() else "0"
