# Handoff: Server Refactor — Extract Shared Handler Logic

**Project:** T3nets  
**Codebase:** `/Users/ellieportugali/projects/t3nets`  
**Goal:** Eliminate the duplication between `adapters/aws/server.py` (2,997 lines) and `adapters/local/dev_server.py` (2,373 lines) by extracting ~1,400 lines of shared handler logic into `adapters/shared/handlers/`. Both servers become thin wiring layers.

---

## Background

The two server files are legitimately different at the infrastructure layer:

| Concern | AWS (`server.py`) | Local (`dev_server.py`) |
|---------|-------------------|------------------------|
| Auth | Cognito JWT + DynamoDB | Env vars / hardcoded |
| Skill execution | Async via EventBridge → Lambda → SQS | Synchronous DirectBus |
| Tenant lookup | Multi-tenant, DynamoDB channel mapping | Single DEFAULT_TENANT |
| Storage | DynamoDB | SQLite |
| Secrets | Secrets Manager | Env file |

But ~29 handler functions (≈1,400 lines) are 85–95% identical across both files. The only difference is usually how `tenant_id` is resolved (1–2 lines). Everything else is duplicated.

---

## What Already Exists

`adapters/shared/server_utils.py` (199 lines) already contains:
- `INTEGRATION_SCHEMAS` — integration config form definitions
- `_format_raw_json()` — JSON formatting helper  
- `_strip_metadata()` — strips metadata from messages before AI
- `_uptime_human()` — human-readable uptime string

These are already imported by both servers.

---

## What to Build

### New directory: `adapters/shared/handlers/`

Each file is a class that receives all dependencies via `__init__()`. No imports of AWS or local modules — only `agent/` interfaces.

```
adapters/shared/handlers/
├── __init__.py
├── settings.py          # SettingsHandlers
├── integrations.py      # IntegrationHandlers  
├── chat.py              # ChatHandlers
├── history.py           # HistoryHandlers
├── training.py          # TrainingHandlers
├── health.py            # HealthHandlers
├── practices.py         # PracticeHandlers
└── webhooks.py          # WebhookHandlers (Teams, Telegram, WhatsApp shared dispatch)
```

### Constructor injection pattern

Each handler class takes only `agent/` interfaces:

```python
# Example: adapters/shared/handlers/settings.py
from agent.interfaces.tenant_store import TenantStore
from agent.interfaces.secrets_provider import SecretsProvider
from agent.router.compiled_engine import CompiledRuleEngine
from starlette.requests import Request
from starlette.responses import JSONResponse

class SettingsHandlers:
    def __init__(
        self,
        tenants: TenantStore,
        secrets: SecretsProvider,
        skill_registry,      # agent.skills.registry.SkillRegistry
        rule_engine: CompiledRuleEngine,
    ):
        self._tenants = tenants
        self._secrets = secrets
        self._skills = skill_registry
        self._engine = rule_engine

    async def get_settings(self, request: Request, tenant_id: str) -> JSONResponse:
        """GET /api/settings — identical logic for both servers"""
        ...

    async def post_settings(self, request: Request, tenant_id: str) -> JSONResponse:
        """POST /api/settings — identical logic for both servers"""
        ...
```

The `tenant_id` parameter is resolved by each server's auth layer before calling the handler. This is the only divergence point.

---

## Handlers to Extract (29 functions)

### Group 1: `settings.py` — ~150 lines

| Handler | Endpoint | Duplicated lines |
|---------|----------|-----------------|
| `get_settings` | GET `/api/settings` | ~34 lines |
| `post_settings` | POST `/api/settings` | ~90 lines |

**Key dependencies:** `TenantStore`, `SecretsProvider`, `SkillRegistry`, `AIModelRegistry`

---

### Group 2: `integrations.py` — ~200 lines

| Handler | Endpoint | Duplicated lines |
|---------|----------|-----------------|
| `list_integrations` | GET `/api/integrations` | ~16 lines |
| `get_integration` | GET `/api/integrations/{name}` | ~32 lines |
| `post_integration` | POST `/api/integrations/{name}` | ~30 lines |
| `test_integration` | POST `/api/integrations/{name}/test` | ~160 lines |

**Key dependencies:** `TenantStore`, `SecretsProvider`, `INTEGRATION_SCHEMAS`

**Note:** `test_integration` is the most complex — it runs actual connectivity tests for Jira, GitHub, Teams, Telegram, WhatsApp, VoiceHer. Logic is 95% identical between files.

---

### Group 3: `chat.py` — ~250 lines

| Handler | Endpoint | Duplicated lines |
|---------|----------|-----------------|
| `handle_chat` | POST `/api/chat` | ~200 lines (core tier routing) |
| `handle_clear` | POST `/api/clear` | ~8 lines |
| `_rebuild_rules` | (internal) | ~30 lines |
| `_log_training` | (internal) | ~36 lines |

**Key dependencies:** `ConversationStore`, `TenantStore`, `AIProvider`, `SkillRegistry`, `CompiledRuleEngine`, `TrainingStore`

**Divergence point:** After tier routing decides to invoke a skill:
- AWS: calls `_handle_async_skill()` (EventBridge dispatch, returns request_id)
- Local: calls `_handle_sync_skill()` (DirectBus, waits for result)

**Pattern:** Accept a `skill_invoker` callable injected at construction time:
```python
class ChatHandlers:
    def __init__(self, ..., skill_invoker: Callable):
        # AWS passes: async_skill_invoker (EventBridge)
        # Local passes: sync_skill_invoker (DirectBus)
        self._skill_invoker = skill_invoker
```

---

### Group 4: `history.py` — ~60 lines

| Handler | Endpoint | Duplicated lines |
|---------|----------|-----------------|
| `get_history` | GET `/api/history` | ~7 lines |

**Key dependencies:** `ConversationStore`

---

### Group 5: `training.py` — ~60 lines

| Handler | Endpoint | Duplicated lines |
|---------|----------|-----------------|
| `list_training` | GET `/api/admin/training` | ~50 lines |

**Key dependencies:** `TrainingStore`

---

### Group 6: `health.py` — ~80 lines

| Handler | Endpoint | Duplicated lines |
|---------|----------|-----------------|
| `get_health` | GET `/api/health` | ~78 lines |

**Key dependencies:** `TenantStore`, `SecretsProvider`, `SkillRegistry`, `SSEConnectionManager`

---

### Group 7: `practices.py` — ~120 lines

| Handler | Endpoint | Duplicated lines |
|---------|----------|-----------------|
| `list_practices` | GET `/api/practices` | ~20 lines |
| `list_practice_pages` | GET `/api/practices/pages` | ~8 lines |
| `upload_practice` | POST `/api/practices/upload` | ~46 lines |
| `invoke_skill` | POST `/api/skill/{name}` | ~30 lines |
| `handle_callback` | POST `/api/callback/{request_id}` | ~100 lines |

**Key dependencies:** `PracticeRegistry`, `SkillRegistry`, `BlobStore`, `PendingRequestStore`, `ConversationStore`

---

### Group 8: `webhooks.py` — ~250 lines

The webhook **dispatch logic** is shared; the **adapter lookup** differs.

| Handler | Endpoint | Shared part | Divergent part |
|---------|----------|-------------|----------------|
| `handle_teams_webhook` | POST `/api/channels/teams/webhook` | Activity type dispatch | Adapter lookup (DynamoDB vs env) |
| `handle_telegram_webhook` | POST `/api/channels/telegram/webhook/{token_hash}` | Update dispatch | Adapter lookup |
| `handle_whatsapp_webhook` | POST `/api/channels/whatsapp/webhook` | Event routing | Adapter lookup |
| `_handle_teams_message` | (internal) | Routing logic | Tenant resolution |
| `_handle_telegram_message` | (internal) | Routing logic | Tenant resolution |
| `_handle_whatsapp_message` | (internal) | Routing logic | Tenant resolution |

**Pattern:** Accept adapter resolver callables:
```python
class WebhookHandlers:
    def __init__(
        self,
        ...,
        resolve_teams_adapter: Callable[[str], TeamsAdapter | None],
        resolve_telegram_adapter: Callable[[str], TelegramAdapter | None],
        resolve_whatsapp_adapter: Callable[[str], WhatsAppAdapter | None],
    ):
        ...
```

---

### Static page handlers (trivial, stay in each server)

These are 1–3 line async functions that just return `FileResponse`. Not worth extracting.

---

## After Extraction: What Each Server Becomes

### `adapters/aws/server.py` (~400 lines remaining)
```python
# Owns exclusively:
# - Cognito auth endpoints (login, signup, confirm, refresh, forgot-password)
# - _get_auth_info() — JWT extraction + DynamoDB tenant mapping
# - WebSocket handlers (_dispatch_ws_event, _handle_ws_connect, _handle_ws_disconnect)
# - _handle_async_skill() — EventBridge dispatch
# - _get_teams_adapter() / _get_telegram_adapter() / _get_whatsapp_adapter() — DynamoDB channel mapping
# - AdminAPI + PlatformAPI delegation
# - init() — DynamoDB, EventBridge, SQS poller, WebSocket manager setup
# - Route registration (wires all shared handlers + cloud-specific routes)
```

### `adapters/local/dev_server.py` (~300 lines remaining)
```python
# Owns exclusively:
# - handle_auth_me() / handle_auth_config() — returns hardcoded local user
# - Admin + Platform route handlers (local multi-tenant admin)
# - Blob upload/download endpoints (FileStore)
# - _handle_sync_skill() — DirectBus execution
# - _get_*_adapter_local() — env var / SQLite adapter lookup
# - init() — SQLite, FileStore, Anthropic provider setup
# - Route registration (wires all shared handlers + local-specific routes)
```

---

## Execution Order (Critical — Dependencies Between Groups)

```
Step 1: Create adapters/shared/handlers/__init__.py (empty)
Step 2: [PARALLEL] Extract Groups 1, 2, 4, 5, 6, 7  ← no interdependencies
Step 3: Extract Group 3 (chat.py) ← depends on understanding skill_invoker pattern
Step 4: Extract Group 8 (webhooks.py) ← depends on understanding adapter resolver pattern  
Step 5: Update adapters/aws/server.py to import + wire shared handlers
Step 6: Update adapters/local/dev_server.py to import + wire shared handlers
Step 7: Run tests + linting + type checking
```

Steps 2 can be parallelized across 6 agents. Steps 5 and 6 can be parallelized. Steps 3, 4 should be sequential or done carefully if parallel.

---

## Verification Checklist

After each handler group extraction:
- [ ] `pytest` passes
- [ ] `ruff check .` clean
- [ ] `mypy agent/ adapters/` clean

End-to-end smoke tests (local):
```bash
python -m adapters.local.dev_server
# Then test:
curl http://localhost:8080/api/health
curl -X POST http://localhost:8080/api/chat -d '{"message": "hello"}'
curl http://localhost:8080/api/settings
curl http://localhost:8080/api/integrations
```

---

## Key Files to Read Before Starting

1. `adapters/aws/server.py` — full source (3000 lines)
2. `adapters/local/dev_server.py` — full source (2373 lines)
3. `adapters/shared/server_utils.py` — existing shared utilities
4. `agent/interfaces/tenant_store.py` — TenantStore interface
5. `agent/interfaces/ai_provider.py` — AIProvider interface
6. `agent/skills/registry.py` — SkillRegistry

---

## Secondary Refactors (Lower Priority)

1. **`agent/practices/registry.py`** (643 lines) — Split into:
   - `PracticeRegistry` (discovery/registration, ~200 lines)
   - `PracticeInstaller` (ZIP validation + extraction, ~180 lines)
   - `PracticeLambdaDeployer` (Lambda deploy, ~190 lines)
   - `PracticeAssetManager` (BlobStore, ~60 lines)

2. **`adapters/aws/admin_api.py`** (433 lines) — Fix:
   - Replace manual `if method == "POST" and path.startswith(...)` string parsing with Starlette routes
   - Replace all `asyncio.run(...)` calls with `async def` methods (risk of deadlock in event loop)
