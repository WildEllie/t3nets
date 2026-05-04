# Handoff: Phase 7 — Server Slim, Steps 1–5

**Date:** 2026-05-05
**Status:** In progress — steps 1–5 shipped to dev and verified, step 6 remaining
**Roadmap item:** Phase 7 (Server Slim — Wiring Layer Cleanup) — `docs/ROADMAP.md:341`
**Branch:** `main`

---

## What Was Done

Mechanical lift-and-shift of inline logic from the two monolithic server files into focused, independently testable modules. The original two-bullet roadmap was decomposed into six steps; the first five are done, the last (final cleanup + line-count target) is queued.

| Step | Result |
|---|---|
| 1. Extract AWS auth handlers | New `adapters/aws/auth_api.py` — `AuthAPI` class (config/me/login/signup/confirm/refresh/forgot-password/confirm-reset). Server gets 8 thin delegating wrappers. |
| 2. Move WhatsApp webhook into shared `WebhookHandlers` | WhatsApp now sits next to Teams/Telegram in `adapters/shared/handlers/webhooks.py`; uses the same `_route_channel_message` Tier 1/2/3 logic. AWS server is the only deployment that supplies the resolver — local passes `None` and 401s on the route. |
| 3. Extract async skill dispatch | New `adapters/aws/async_dispatch.py` — `AsyncSkillDispatcher` class with `dispatch_chat()` (returns Response for SSE/WS) and `dispatch_channel()` (fire-and-forget for webhooks). |
| 4. Extract local admin/platform tenant CRUD | New `adapters/local/admin_api.py` (`LocalAdminAPI`) and `adapters/local/platform_api.py` (`LocalPlatformAPI`), parallel to the AWS classes. Server gets 5 thin delegating wrappers. |
| 5. Decompose `init()` in both servers | AWS init is now an orchestrator that calls `_init_aws_adapters → _init_aws_practices → _init_aws_dispatch → _init_aws_async_dispatch → _init_aws_state → _init_aws_handlers`. The 3 closures (`_chat_skill_invoker`, `_on_credentials_saved`, `_post_install_hook`) lifted to module level. Local init mirrors the same shape with 5 helpers. |

---

## Files Created

| File | Lines | Purpose |
|------|-------|---------|
| `adapters/aws/auth_api.py` | 314 | Cognito-backed auth handlers (Step 1). |
| `adapters/aws/async_dispatch.py` | 156 | EventBridge → Lambda → SQS dispatcher (Step 3). |
| `adapters/local/admin_api.py` | 229 | Local SQLite-backed admin tenant CRUD (Step 4). |
| `adapters/local/platform_api.py` | 166 | Local SQLite-backed platform tenant CRUD (Step 4). |

## Files Modified

| File | Change |
|------|--------|
| `adapters/aws/server.py` | **2,033 → 1,418 lines (−30%)**. Auth/WhatsApp/async-dispatch bodies removed; replaced with thin wrappers. `init()` decomposed into 6 helpers; closures lifted to module scope. |
| `adapters/local/dev_server.py` | **1,458 → 1,132 lines (−22%)**. Admin/platform tenant CRUD moved out. `init()` decomposed into 5 helpers. |
| `adapters/shared/handlers/webhooks.py` | +119 lines: WhatsApp webhook + message handler added; `WhatsAppResolverT` type alias; prefix mapping in `_route_channel_message` accepts `wa`. Module docstring updated to reflect three channels. |

---

## Key Decisions

- **Lift-and-shift only.** Step 5 is decomposition for clarity — not size reduction. AWS server actually grew slightly (`+26` lines) from helper docstrings + `global` declarations. The line-count target (`≤400` AWS, `≤300` local) is intentionally deferred to step 6, which will move route wiring + remaining stragglers (channel resolvers, webhook registration helpers, page-handler shims) into separate modules.
- **WhatsApp resolver is optional.** `WebhookHandlers.resolve_whatsapp_adapter` defaults to `None`; AWS passes `_get_whatsapp_adapter`, local passes nothing and the route 401s. No need to fake a local WhatsApp resolver.
- **Closures referencing `chat_handlers` are evaluated at call time.** `_init_aws_async_dispatch` constructs `AsyncSkillDispatcher` with `lambda *a, **kw: chat_handlers.log_training(*a, **kw)`. The lambda binds `chat_handlers` lexically, so the handler order (`_init_aws_async_dispatch` before `_init_aws_handlers`) is fine — no skill is dispatched until after both have run.
- **Global declarations stay.** Helpers use `global` to assign module state, mirroring the existing pattern. Refactoring to explicit dependency injection is a bigger architectural change that's out of scope for Phase 7.

---

## Verification

| Gate | Result |
|------|--------|
| `ruff check adapters/` | Clean |
| `ruff format --check` | Clean |
| `mypy adapters/aws/auth_api.py adapters/aws/async_dispatch.py` | Clean (the 10 pre-existing errors elsewhere — `s3_blob_store.py`, MultiAIProvider variance, etc. — are unchanged) |
| `pytest tests/` | 360 passed |
| Local boot — `python -m adapters.local.dev_server` | Boots cleanly, both `local`/`acme` tenants load, rule engines hydrate, `/api/health` + `/api/platform/tenants` + `/api/admin/tenants/local/users` return correct data |
| AWS dev deploy (after step 4) | ECS rev updated, 159s build/deploy. `/api/health`, `/api/auth/config`, `/api/auth/me` (401 path), `/api/channels/whatsapp/webhook/{token}` (401 path) all confirmed |
| AWS dev deploy (after step 5) | Same 4 endpoints re-verified post-init-refactor; uptime 176s, default tenant + 5 skills hydrated |

Two ECS rollouts gated this work: one after step 4 (auth + WhatsApp + async dispatch + local CRUD), one after step 5 (init decomposition).

---

## What's Next: Step 6

Final cleanup to hit the line-count target. Currently:

- `adapters/aws/server.py`: **1,418 lines** → target ≤400
- `adapters/local/dev_server.py`: **1,132 lines** → target ≤300

Largest remaining chunks in `adapters/aws/server.py`:

| Range | Section | LOC |
|---|---|---|
| 1–230 | imports + module-level globals + env constants + push_client setup | ~230 |
| 320–415 | helper functions (`_resolve_model`, `_get_auth_info`, `_file_response`, `_extract_user_key`, `_get_engine`, `_enrich_match_params`, WS dispatch helpers) | ~95 |
| 417–595 | page handlers + thin wrappers (mostly already minimal) | ~180 |
| 596–710 | invitation handlers (`handle_invitation_validate`, `handle_invitation_accept`) — could fold into `AdminAPI` | ~115 |
| 716–745 | `handle_admin` / `handle_platform` dispatchers — already thin | ~30 |
| 750–860 | channel adapter resolvers (`_get_teams_adapter`, `_get_telegram_adapter`, `_get_whatsapp_adapter`) | ~110 |
| 866–880 | `practice_page` + section comments | ~15 |
| 880–1010 | Telegram + WhatsApp webhook registration helpers | ~140 |
| 1010–1410 | init() orchestrator + module-level closures + helper functions | ~400 |
| 1410–1455 | Routes table + middleware + `main()` | ~50 |

Recommended step 6 plan:

1. **Move channel adapter resolvers** (`_get_teams_adapter`, `_get_telegram_adapter`, `_get_whatsapp_adapter`) into a new `adapters/aws/channel_resolvers.py` (~110 lines out).
2. **Move webhook registration helpers** (`_register_telegram_webhook`, `_register_whatsapp_webhook`) into a new `adapters/aws/webhook_registration.py` (~140 lines out).
3. **Move init helpers + module-level closures** into a new `adapters/aws/bootstrap.py`. The `init()` orchestrator stays in `server.py` and calls `bootstrap.run(...)`. Returns a `ServerState` dataclass that holds the shared handler instances; `server.py` thin-wrappers pull from `state.chat_handlers` etc. (~400 lines out, but adds ~50 lines of orchestration glue).
4. **Fold invitation handlers into `AdminAPI`** so the dispatch goes through the existing `/api/admin/tenants/.../invitations` shape — the routes already overlap. (~115 lines out.)
5. **Move module-level helper functions** (`_resolve_model`, `_get_auth_info`, etc.) into `adapters/aws/server_helpers.py`. (~95 lines out.)
6. Same treatment for `adapters/local/dev_server.py`: split helpers into `adapters/local/bootstrap.py` and inline channel resolvers / sync skill helper.

After step 6, expected counts:
- `adapters/aws/server.py`: ~430 lines (imports, globals, route table, thin wrappers, `main()`)
- `adapters/local/dev_server.py`: ~280 lines

Each substep is a separate ECS deploy gate.

---

## Test Plan for Step 6

1. After each sub-extraction, `pytest tests/` should stay at 360 passed.
2. After channel resolvers + webhook registration + bootstrap moves: deploy to dev, verify `/api/health`, `/api/auth/me`, a Telegram webhook 401 (proves channel resolver path), and `/api/integrations/{name}` POST → channel mapping (proves credential-saved hook still wires).
3. After invitation fold-in: confirm `/api/invitations/validate?invite=...` and `/api/invitations/accept` still work end-to-end via the platform tenant flow.
4. After the local-side cleanup: boot the local server, hit `/api/admin/tenants/local/users`, confirm `Acme` rule engine hydrates from SQLite cache.

---

## Commit Sequence (Steps 1–5)

Pending — this handoff is being written before the commit. Suggested grouping for review:

| Commit | Subject |
|---|---|
| `feat: extract AWS auth handlers into AuthAPI (Phase 7 step 1)` | `auth_api.py` + server.py wrappers |
| `feat: move WhatsApp webhook into shared WebhookHandlers (Phase 7 step 2)` | webhooks.py + server.py |
| `feat: extract async skill dispatcher (Phase 7 step 3)` | `async_dispatch.py` + server.py |
| `feat: extract local admin/platform tenant CRUD (Phase 7 step 4)` | `local/admin_api.py` + `local/platform_api.py` + dev_server.py |
| `refactor: decompose init() in both servers (Phase 7 step 5)` | server.py + dev_server.py only |

If you'd rather one bundled commit (Ellie has previously preferred bundled commits for refactors of this shape), title it something like `refactor: server slim — Phase 7 steps 1-5`.
