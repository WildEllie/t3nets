# Handoff: Phase 7 — Server Slim, Step 6 (line-count target)

**Date:** 2026-05-05
**Status:** Done — Phase 7 closed
**Roadmap item:** Phase 7 (Server Slim — Wiring Layer Cleanup) — `docs/ROADMAP.md:341`
**Branch:** `main`

---

## What Was Done

Final extraction pass: pulled the remaining inline machinery (channel resolvers, webhook registration, module-level helpers, init/closures) out of both server entry points, and reorganised the route tables to use dotted-string delegation factories. AWS `server.py` and local `dev_server.py` are now thin wiring layers that hit their roadmap line-count targets.

| Step | Result |
|---|---|
| 6.1 — AWS channel resolvers | `adapters/aws/channel_resolvers.py` — `ChannelResolvers` class with `get_teams`, `get_telegram`, `get_whatsapp`. |
| 6.2 — AWS webhook registration | `adapters/aws/webhook_registration.py` — `register_telegram_webhook`, `register_whatsapp_webhook` as pure module functions. |
| 6.3 — AWS module helpers | `adapters/aws/server_helpers.py` — pure utilities (`resolve_model`, `get_auth_info`, `file_response`, `extract_user_key`, `enrich_match_params`, `get_lambda_deploy_config`, `bedrock_geo_prefix`, `WebSocketEventMiddleware`, `QueueBridge`). |
| 6.4 — Invitations into AdminAPI | `validate_invitation` and `accept_invitation` methods added to `adapters/aws/admin_api.py:AdminAPI` and `adapters/local/admin_api.py:LocalAdminAPI`. |
| 6.5 — AWS bootstrap | `adapters/aws/bootstrap.py` — `ServerState` dataclass + `init()` orchestrator. Holds all init helpers and the three closures (`_make_chat_skill_invoker`, `_make_on_credentials_saved`, `_make_post_install_hook`). Also hosts the heavier route-handler methods (`admin_dispatch`, `platform_dispatch`, `rules_admin`, `history`). |
| 6.6 — Local equivalent | `adapters/local/bootstrap.py` (`LocalServerState`) + `adapters/local/server_helpers.py`. Same pattern — state holds adapters, handlers, and the route handlers that need state-aware logic (`training_admin`, `rules_admin`, `blob_upload`, `blob_read`, `auth_me`, `history`). |

---

## Files Created

| File | Lines | Purpose |
|------|-------|---------|
| `adapters/aws/bootstrap.py` | 701 | `ServerState` + init machinery + closures + route-handler methods. |
| `adapters/aws/server_helpers.py` | 260 | Pure helpers + WebSocket middleware + Lambda config. |
| `adapters/aws/channel_resolvers.py` | 86 | `ChannelResolvers` class. |
| `adapters/aws/webhook_registration.py` | 66 | `register_telegram_webhook` / `register_whatsapp_webhook`. |
| `adapters/local/bootstrap.py` | 511 | `LocalServerState` + init machinery + route-handler methods. |
| `adapters/local/server_helpers.py` | 189 | Pure helpers + local channel resolvers. |

## Files Modified

| File | Change |
|------|--------|
| `adapters/aws/server.py` | **1,418 → 276 lines (−81%)**. Now holds: imports, `state` global, `_page` factory, `practice_page`, SSE endpoint, `_d`/`_da` route-handler factories (dotted-string `getattr` resolution), the route table, middleware list, `app`, `init()`, `main()`. No business logic. |
| `adapters/local/dev_server.py` | **1,132 → 294 lines (−74%)**. Same shape: imports, `state` global, `_page`/`_asset` factories, `practice_page`, SSE endpoint, `_d`/`_dt` factories, routes, `app`, `init()`, `main()`. |
| `adapters/aws/admin_api.py` | +82 lines: `validate_invitation` and `accept_invitation` methods. |
| `adapters/local/admin_api.py` | +87 lines: `validate_invitation` and `accept_invitation` methods. |

---

## Key Design Decisions

- **Dotted-string `_d`/`_da`/`_dt` factories.** Route handlers now look like `Route("/api/chat", _d("chat_handlers.handle_chat"), methods=["POST"])`. The factory builds an `async def` that does `getattr(state, "chat_handlers").handle_chat(request)` lazily at request time. This solves two problems at once:
  - The route table is built at module-import time, before `init()` populates state. Lazy lookup defers the access until traffic arrives.
  - mypy stops complaining about Optional handler attributes — `getattr(state, ...)` returns `Any`, sidestepping the union-attr noise that would otherwise need 30+ `# type: ignore` comments.
- **`_da` (auth-required) and `_dt` (default-tenant)**: AWS uses `_da("settings_handlers.get_settings")` which resolves auth to a `tenant_id` and passes it through; local uses `_dt("settings_handlers.get_settings")` which always passes `state.default_tenant`. Both follow the same dotted-path API.
- **State-aware methods on the dataclass** (`state.admin_dispatch`, `state.platform_dispatch`, `state.rules_admin`, `state.blob_upload`, etc.) keep the route table entries one line each. The methods touch state directly, which is what they're for.
- **Closures lifted to factory functions** (`_make_chat_skill_invoker(state) -> Callable`). The lambda inside still captures `state` by reference, so deferred lookup of late-init handlers (e.g. `state.async_dispatch.dispatch_chat`) still works.
- **`global` declarations stay**: each server still has a single `state` module global that's reassigned inside `init()`. Refactoring to explicit DI was out of scope per the original plan.

---

## Verification

| Gate | Result |
|------|--------|
| `ruff check adapters/` | Clean |
| `ruff format --check` | Clean |
| `mypy adapters/` | 11 errors, all pre-existing variance/type issues (s3_blob_store `Any` return, MultiAIProvider dict invariance, AsyncResultRouter PushClient/AIProvider arg types, EventBus `get_result` lookup, three unused `# type: ignore` in shared modules). No new errors from this refactor. |
| `pytest tests/` | 360 passed |
| Local boot — `python -m adapters.local.dev_server` | Boots cleanly. Both `local`/`acme` tenants load, rule engines hydrate (3 + 2 skills, v2 + v1 generations), `/api/health` + `/api/auth/me` + `/api/admin/tenants/local/users` + `/api/platform/tenants` all return correct data. |
| AWS dev deploy | `./scripts/deploy.sh` — built, pushed to ECR, ECS service updated. 161s build/deploy. New task started 2026-05-05 13:25:01 UTC. |
| AWS smoke test | `/api/health` returns `default` tenant + 5 skills under Bedrock; `/api/auth/config` returns full Cognito config; `/api/auth/me` returns 401 without token; `/api/channels/whatsapp/webhook/{token_hash}` and `/api/channels/telegram/webhook/{token_hash}` return 401 (channel resolver path verified). |

---

## Final Line Counts

| File | Before | After | Δ |
|------|-------:|------:|--:|
| `adapters/aws/server.py` | 1,418 | **276** | −81% |
| `adapters/local/dev_server.py` | 1,132 | **294** | −74% |
| Total (across all Phase 7 work, vs original) | 3,491 (server.py 2,033 + dev_server.py 1,458) | 570 | −84% |

Roadmap targets (`server.py ≤ 400`, `dev_server.py ≤ 300`) **met**.

---

## Notes for Future Work

- **The dotted-string factory pattern** could be lifted to `adapters/shared/server_utils.py` if multi-cloud (Phase 11) adds Azure or GCP entry points that share the same shape. Don't do it speculatively.
- **`_compiled_engines` is shared by reference** between `state.compiled_engines`, `state.chat_handlers.compiled_engines`, `state.training_handlers.compiled_engines`, etc. — they all hold the same dict. Same was true before; the refactor preserved the shared identity.
- **mypy `union-attr` noise**: most thin wrappers used `# type: ignore[union-attr]` because `state.X` was Optional. The `_d`/`_da` factories eliminate this. If you ever add a new wrapper that accesses `state.X.method` directly, the `assert state.X is not None` pattern keeps mypy happy.

Phase 7 is closed. Next roadmap item: Phase 8 (Dashboard & UX) or Phase 10 (Expand Skills) — both have open backlog items in `docs/ROADMAP.md`.
