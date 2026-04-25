# T3nets — Roadmap & TODO

**Last Updated:** April 25, 2026 (Phase reorder: Practices → Phase 6, Dashboard → Phase 7, Multi-cloud → Phase 8, Expand Skills → Phase 9)

---

## Completed ✅

### Phase 0: Design & Prototype
- [x] Master architecture document
- [x] Project scaffolded (interfaces, models, channels, skills)
- [x] GitHub repo live (WildEllie/t3nets)
- [x] Sprint status skill (Jira integration)
- [x] Release notes skill (Jira integration) — routing, --raw support, future release handling, Jira API v3 migration
      ↳ ✅ See [handoff notes](../handoffs/001-fix-release-notes-skill.md)
- [x] Local adapters (Anthropic API, SQLite, env secrets, direct bus)
- [x] Dev server with chat UI
- [x] Hybrid routing (rules → Claude fallback)
- [x] `--raw` debug mode for skill output
- [x] Health dashboard with live stats
- [x] Shared nav bar across pages

### Phase 1: AWS Infrastructure (Terraform)
- [x] Terraform modules: VPC, API Gateway, ECS Fargate, DynamoDB, Secrets Manager, ECR
- [x] Bedrock AI provider adapter (`bedrock_provider.py`)
- [x] DynamoDB conversation store adapter (`dynamodb_conversation_store.py`)
- [x] DynamoDB tenant store adapter (`dynamodb_tenant_store.py`)
- [x] Secrets Manager adapter (`secrets_manager.py`)
- [x] AWS server entry point (`adapters/aws/server.py`)
- [x] Dockerfile for router container
- [x] Deploy scripts (`deploy.sh`, `seed.sh`)
- [x] Bedrock model access verified (Sonnet 4.5, Sonnet 4.6, Nova Pro/Lite/Micro, Llama 3.2 1B)
- [x] AI model pricing research (Claude vs Nova vs third-party options)
- [x] Project documentation (docs/ folder)
- [x] Geographic inference profiles for Bedrock (us/eu/apac prefixes)
- [x] Cross-region IAM for Bedrock inference (us-east-1, us-east-2, us-west-2)
- [x] Bedrock tool_use/tool_result message conversion fix

### Phase 1b: Deploy & Settings
- [x] `terraform apply` — deploy infrastructure to AWS
- [x] Centralized AI model registry (`agent/models/ai_models.py`)
- [x] Settings page with model selection UI
- [x] Chat history persistence across page navigation
- [x] Markdown rendering in chat (tables, code, lists)
- [x] Dynamic environment badges: platform (LOCAL/AWS) + stage (DEV/STAGING/PROD)
- [x] Staging and prod Terraform tfvars templates
- [x] Ping skill for lightweight model testing (no integrations needed)
- [x] Verified models: Sonnet 4.5, Sonnet 4.6, Nova Pro, Nova Lite, Nova Micro, Llama 3.2 1B
- [x] **Milestone:** Platform running on AWS with configurable AI models

### Phase 2: Multi-Tenancy
- [x] Cognito user pool + auth flow (PKCE, hosted UI, token exchange)
      ↳ ✅ Completed — see [handoff notes](../handoffs/002-cognito-auth-and-tenant-resolution.md)
- [x] Tenant resolution from JWT (`custom:tenant_id` claim, API Gateway JWT authorizer)
      ↳ ✅ Completed — see [handoff notes](../handoffs/002-cognito-auth-and-tenant-resolution.md)
- [x] Admin API (tenant CRUD — list, get, create, update) — TODO: role-based access
      ↳ ✅ Completed — see [handoff notes](../handoffs/002-cognito-auth-and-tenant-resolution.md)
- [x] Onboarding wizard (vanilla HTML + backend endpoints)
      ↳ ✅ Completed — see [handoff notes](../handoffs/003-onboarding-wizard.md)
- [x] DynamoDB GSI for cross-tenant user lookup by IdP sub (cognito-sub-lookup)
      ↳ ✅ Completed — see [handoff notes](../handoffs/004-idp-agnostic-auth-phase1.md)
- [x] Remove custom:tenant_id from JWT — DynamoDB is sole source of truth for user→tenant
      ↳ ✅ Completed — see [handoff notes](../handoffs/004-idp-agnostic-auth-phase1.md)
- [x] In-app login/signup/verify (replace Cognito Hosted UI with server-side auth endpoints)
      ↳ ✅ Completed — see [handoff notes](../handoffs/004-idp-agnostic-auth-phase1.md)
- [x] Fix API Gateway auth routes (login/signup/confirm/refresh were blocked by JWT authorizer)
      ↳ ✅ Completed — see [handoff notes](../handoffs/006-fix-auth-api-gateway-routes.md)
- [x] Password reset flow (forgot password → code → new password)
      ↳ ✅ Completed — see [handoff notes](../handoffs/007-password-reset-flow.md)
- [x] Add avatar_url to TenantUser model + DynamoDB/SQLite persistence
      ↳ ✅ Completed — see [handoff notes](../handoffs/004-idp-agnostic-auth-phase1.md)
- [x] Seed a second tenant, verify isolation
      ↳ ✅ Completed — see [handoff notes](../handoffs/005-second-tenant-seed-and-isolation.md)
- [x] **Milestone:** Two teams onboarded, data fully isolated

### Phase 2b: Tenant Management & Settings
- [x] Show tenant name in nav bar across all pages
      ↳ ✅ Completed — see [handoff notes](../handoffs/008-tenant-name-in-navbar.md)
- [x] Extend settings API to expose full TenantSettings (model, skills, integrations)
      ↳ ✅ Completed — see [handoff notes](../handoffs/009-settings-api-and-skill-toggles.md)
- [x] Skill toggle per tenant — enable/disable skills from settings page
      ↳ ✅ Completed — see [handoff notes](../handoffs/009-settings-api-and-skill-toggles.md)
- [x] Per-skill integration config — edit/store integration credentials (e.g., Jira) from settings dashboard
      ↳ ✅ Completed — see [handoff notes](../handoffs/010-tabbed-settings-integration-config.md)
- [x] **Milestone:** Admins can fully manage tenant settings, skills, and integrations from the dashboard

### Phase 3: External Channels
- [x] Teams channel adapter (Azure Bot → webhook)
      ↳ ✅ Completed — see [handoff notes](../handoffs/011-teams-channel-adapter.md)
- [x] Telegram channel adapter (BotFather → webhook)
      ↳ ✅ Completed — see [handoff notes](../handoffs/012-telegram-channel-adapter.md)
- [x] Settings UI — Channels tab with setup guides and connection testing
      ↳ ✅ Completed — see [handoff notes](../handoffs/012-telegram-channel-adapter.md)
- [x] **Milestone:** Team member asks sprint status in Teams or Telegram, gets answer

### Phase 3b: Async Skill Execution (EventBridge + Lambda + SQS)
      ↳ 📋 Design reviewed and revised — see [handoff notes](../handoffs/013-phase-3b-design-review.md)
      ↳ 📐 Full implementation plan: [plan-phase-3b-async-skills.md](plan-phase-3b-async-skills.md)

Replace the synchronous DirectBus with an event-driven architecture. The router container stays stateless and horizontally scalable; skills run as Lambda functions. Dashboard receives async results via SSE.

**Architecture:**
- Router container (ECS Fargate) handles webhooks, runs Tier 1/2/3 routing, owns all channel adapters
- Skills execute as a single Lambda function, invoked via EventBridge events
- Responses flow back through SQS → router container → channel adapter (SSE for dashboard, API push for Teams/Telegram)
- All state lives in DynamoDB — router containers are fully stateless and can scale horizontally
- Lambda idempotency via DynamoDB status check (safe for EventBridge retries)
- Lazy-loaded skill dependencies (no provisioned concurrency needed)

**Implementation tasks:**
- [x] SSE endpoint (`GET /api/events`) for dashboard async results — AWS + local servers + dashboard JS
      ↳ ✅ Completed — `agent/sse.py`, both servers, dashboard JS updated
- [x] `EventBridgeBus` adapter implementing the `EventBus` interface
      ↳ ✅ Completed — `adapters/aws/event_bridge_bus.py`
- [x] Lambda skill handler with idempotency check and lazy-loading
      ↳ ✅ Completed — `adapters/aws/lambda_handler.py`
- [x] SQS poller background thread in router (WaitTimeSeconds=20)
      ↳ ✅ Completed — `adapters/aws/sqs_poller.py`
- [x] Pending requests DynamoDB table (includes Teams service_url, status for idempotency)
      ↳ ✅ Completed — `adapters/aws/pending_requests.py` + Terraform
- [x] Router code changes: feature flag, async result handling, remove in-memory state
      ↳ ✅ Completed — `adapters/aws/server.py` + `adapters/aws/result_router.py`
- [x] Terraform modules: Lambda + IAM, EventBridge bus + rule + DLQ, SQS queue + DLQ, pending-requests table, SSE API Gateway route
      ↳ ✅ Completed — all modules in `infra/aws/modules/`
- [x] Local development parity (DirectBus stays, SSE works locally)
      ↳ ✅ Completed — `adapters/local/dev_server.py` updated with SSE
- [x] API Gateway WebSocket API for real-time push (replaces SSE on AWS)
      ↳ ✅ Completed — see [handoff notes](../handoffs/015-websocket-api-gateway.md)
- [x] Deploy and verify end-to-end (`terraform apply` + `deploy.sh` with `USE_ASYNC_SKILLS=true`)
      ↳ ✅ Completed — Terraform infra already applied, `deploy.sh` with `USE_ASYNC_SKILLS=true` deployed router + skill Lambdas, verified via dashboard
- [x] Verify horizontal scaling: run 2+ ECS tasks, confirm no message loss or duplicate responses
      ↳ ✅ Completed — DynamoDB-backed WebSocket registry (`ws_connections.py`); 2 tasks verified: connections tracked in `t3nets-dev-ws-connections` table, async results delivered across tasks, disconnect cleans up row
- [x] **Milestone:** Skills run on Lambda, router is stateless, container scales horizontally

      ↳ 📋 Implementation — see [handoff notes](../handoffs/014-phase-3b-implementation.md)
      ↳ 📋 WebSocket push — see [handoff notes](../handoffs/015-websocket-api-gateway.md)

**Phase 3c: Developer Experience**
- [x] Migrate both servers to uvicorn ASGI + Starlette: persistent event loop, true async concurrency — no `asyncio.run()` per request
      ↳ ✅ `adapters/local/dev_server.py`, `adapters/aws/server.py`; `base_handler.py` deleted
- [x] Auto-reload dev server — uvicorn `--reload` flag available now that both servers use uvicorn
- [x] Strict mypy compliance — 0 errors across all 60 source files in `agent/` + `adapters/` (284 fixed)
- [x] License compliance — `THIRD_PARTY_LICENSES` with BSD-3-Clause attribution for uvicorn
- [x] Unit test suite for router, rule engine, skills (tenant isolation, release notes, error handler)

### Phase 4: Invitation Flow
      ↳ 📐 Full plan: [plan-invitation-signup-flow.md](plan-invitation-signup-flow.md)

Two-path signup: existing flow creates a new tenant; invited users join an existing tenant and skip onboarding.

**Phase 4a — Backend**
- [x] Add `Invitation` dataclass to `agent/models/tenant.py` (code, tenant_id, email, role, status, TTL)
- [x] DynamoDB invitation storage: `pk=INVITE#{code}`, `sk=META`, TTL attribute for auto-cleanup
- [x] Terraform: enable DynamoDB TTL on tenants table (`infra/aws/modules/data/main.tf`)
- [x] `POST /api/admin/tenants/{id}/invitations` — create invitation, return code + URL (admin only)
- [x] `GET /api/admin/tenants/{id}/invitations` — list pending invitations (admin only)
- [x] `DELETE /api/admin/tenants/{id}/invitations/{code}` — revoke invitation (admin only)
- [x] `GET /api/invitations/validate?code=xxx` — public; return tenant name + email if valid
- [x] `POST /api/invitations/accept` — validate JWT, match email, link user to tenant, mark accepted
- [x] Terraform: add public API Gateway routes for validate + accept (`infra/aws/modules/api/main.tf`)
- [x] Local dev: mock invitation endpoints in `dev_server.py`, SQLite storage in `sqlite_tenant_store.py`

**Phase 4b — UI**
- [x] `/join?code=xxx` page — validate code, show invitation panel (tenant name, email, login or signup)
- [x] Signup panel: invitation-aware — email pre-filled + locked, call accept after verify, redirect to `/chat`
- [x] Login panel: invitation-aware — call accept after login, redirect to `/chat`
- [x] Settings → Team tab: current members table, pending invitations table (copy link / revoke), invite form
- [x] `GET /api/admin/tenants/{id}/users` endpoint for Team tab member list

- [x] **Milestone:** Admin can invite users by link; invited users join the correct tenant and land in chat


### Phase 4.5: Session Management
- [x] Decode JWT `exp` client-side; track mouse/keyboard activity
- [x] Active user: silent token refresh 5 min before expiry via `/api/auth/refresh`
- [x] Idle user: "Session Expired" modal at expiry → OK navigates to `/login`
- [x] Apply to `chat.html`, `settings.html`, and `health.html`
- [x] Role-based access: members redirected away from settings page
- [x] **Milestone:** No silent 401 failures; idle sessions expire gracefully


### Phase 4.6: Platform Admin
- [x] `adapters/aws/platform_api.py` — new API class gated to default-tenant admins only
- [x] `GET /api/platform/tenants` — list all tenants with user counts
- [x] `POST /api/platform/tenants` — create tenant with server-side slugify + admin invitation
- [x] `PATCH /api/platform/tenants/{id}/suspend|activate` — lifecycle management
- [x] `DELETE /api/platform/tenants/{id}` — tombstone delete (preserves user records)
- [x] `adapters/local/platform.html` — tenant management UI (table, status badges, create dialog, invite copy)
- [x] Platform nav link injected dynamically for default-tenant admins on all nav pages
- [x] Terraform: `GET /platform` public API Gateway route
      ↳ ✅ Completed — see [handoff notes](../handoffs/016-platform-admin-tenant-management.md)
- [x] **Milestone:** Platform admins can create, suspend, and delete tenants from the dashboard

### Phase 5: AI-Generated Rule Engine
      ↳ 📐 Full plan: [plan-ai-generated-rule-engine.md](plan-ai-generated-rule-engine.md)

Replace hand-maintained regex patterns (170+ rules in `rule_router.py`) with AI-generated, per-tenant rule sets. The core idea: keep $0 regex routing for known requests, but have AI generate and maintain the rules automatically when skills are enabled/disabled.

**Phase 5a — Core Rule Engine**
      ↳ ✅ Completed — see [handoff notes](../handoffs/020-phase-5a-ai-generated-rule-engine.md)
- [x] Skill trigger storage in DynamoDB (triggers, actions, action_descriptions per skill)
- [x] Rule Engine Builder service — AI generates optimized regex rules from skill metadata + tenant's enabled skill combination
- [x] Compiled Rule Engine — in-memory compiled regex matching per tenant, loaded from DynamoDB
- [x] Router integration — compiled engine as Tier 1 ($0, <1ms), Claude as Tier 2 with disabled-skill awareness
- [x] Disabled skill detection — if user requests a disabled skill, respond with "contact your admin" instead of confusion
- [x] Rule persistence (SQLite for local, DynamoDB for AWS)
- [x] Auto-regeneration — rebuild rules when tenant enables/disables skills
- [x] Freeform chat fallback — when no skill matches, AI responds conversationally (general knowledge, small talk)
- [x] Training data logging — save Tier 2 unmatched requests for future rule improvement
- [x] **Milestone:** Majority of skill-routable messages handled at $0 via AI-generated rules; no hand-maintained regex

**Phase 5b — Admin Training Tools**
      ↳ ✅ Completed
- [x] Training data API endpoints (list, annotate, delete) — `GET/PATCH/DELETE /api/admin/training`
- [x] Admin maps unmatched messages to skills from dashboard
- [x] Rule recalculation endpoint — `POST /api/admin/rules/rebuild` incorporates annotated training data
- [x] Dashboard UI — `training.html`: viewer, skill mapping dropdown, rebuild button, rules status
- [x] **Milestone:** Admins can review unmatched messages and improve routing accuracy over time

**Phase 5c — Ollama Integration (Free AI Models)**
      ↳ 📐 Full plan: [plan-ollama-integration.md](plan-ollama-integration.md)
      ↳ ✅ Completed — see [handoff notes](../handoffs/021-phase-5c-ollama-integration.md)

Add Ollama as a third AI provider, enabling zero-cost local development and free model selection for any tenant. Ollama runs as a separate container/process and exposes an OpenAI-compatible API with tool use support.

- [x] `OllamaProvider` adapter implementing `AIProvider` (OpenAI-compatible API, `urllib` only)
- [x] Model registry entries: Llama 3.2 3B, Llama 3.1 8B, Mistral 7B, Qwen 2.5 7B
- [x] Dev server wiring: `OLLAMA_API_URL` env var, fallback when no `ANTHROPIC_API_KEY`
- [x] AWS server wiring: `OLLAMA_API_URL` env var, ECS sidecar container support
- [x] Settings UI: Ollama models with "Free" badge in model selector
- [x] Docker Compose: Ollama sidecar with persistent volume + `docker-compose.ollama.yml` override for automatic wiring
- [x] Terraform: Ollama sidecar in ECS task definition (`use_ollama` feature flag, `ollama_model`, `ollama_memory_mb`)
- [x] Security: ECS sidecar shares task network namespace (localhost) — no SG changes needed; Docker Compose uses internal DNS
- [x] Tier 1 formatting with free model: optional `tier1_formatting_model` in `TenantSettings`
- [x] Unit tests for OllamaProvider (mock HTTP, tool call mapping)
- [x] **Milestone:** Free models selectable for any tenant; zero-cost local dev without API key

---

## Up Next 🔜

### Phase 5d: Server Architecture Refactor — Internals Cleanup

Handler logic is already extracted from the two monolithic server files into `adapters/shared/handlers/`. What remains in this phase is the smaller-blast-radius internal cleanup that doesn't touch the request entry points. The riskier server slim-down — moving the route wiring itself — is sequenced separately as Phase 7, after the SDK lands on PyPI.

- [x] Create `adapters/shared/handlers/` — settings, integrations, chat, history, training, health, practices, webhooks
      ↳ ✅ all 8 handler modules live under `adapters/shared/handlers/`
- [x] Extract `SettingsHandlers` — `GET/POST /api/settings`
- [x] Extract `IntegrationHandlers` — list/get/post/test integrations
- [x] Extract `ChatHandlers` — Tier 0/1/2 routing, skill dispatch, AI fallback (`skill_invoker` callable threads user_message + model metadata for async dispatch)
- [x] Extract `HistoryHandlers`, `TrainingHandlers`, `HealthHandlers`
- [x] Extract `PracticeHandlers` + `WebhookHandlers` (Teams/Telegram/WhatsApp dispatch logic)
- [ ] Split `agent/practices/registry.py` (643 lines) → `registry` + `installer` + `deployer` + `assets`
- [ ] Fix `adapters/aws/admin_api.py`: replace manual path parsing + remove `asyncio.run()` calls
- [ ] **Milestone:** `practices/registry.py` split into focused modules; `admin_api.py` modernized

      ↳ 📋 Full handoff: `.claude/plans/server-refactor-handoff.md`

### Phase 6: Practices — Team Experience Bundles
      ↳ 📐 Full plan: [plan-practices-team-experience-bundles.md](plan-practices-team-experience-bundles.md)

Practices are complete team experience bundles: skills + custom console pages + functionality, uploadable as ZIPs. Pages interact with data through skills (same async EventBus flow as chat). One primary practice per tenant + add-on skills/pages from other practices.

**Phase 6a — Core Practice Framework**
- [x] Practice data models (`PracticeDefinition`, `PracticePage`) and `PracticeRegistry`
      ↳ ✅ `agent/models/practice.py`, `agent/practices/registry.py`
- [x] Built-in engineering practice: move existing skills into `agent/practices/engineering/`
      ↳ ✅ `agent/practices/dev-jira/` with `practice.yaml` and skills
- [x] Practice manifest format (`practice.yaml`: skills, pages, integrations, system_prompt_addon)
      ↳ ✅ `agent/practices/dev-jira/practice.yaml`
- [x] `POST /api/skill/{name}` — async skill invocation for pages (returns 202, result via WebSocket/SSE)
      ↳ ✅ Both servers: `aws/server.py:2573`, `local/dev_server.py:2037`
- [x] `GET /api/practices/pages` — pages for tenant's dynamic nav injection
      ↳ ✅ Both servers: `aws/server.py:2676`, `local/dev_server.py:2093`
- [x] `GET /api/practices` — list installed practices
      ↳ ✅ Both servers: `aws/server.py:2606`, `local/dev_server.py:2071`
- [x] Practice page serving: `/p/{practice}/{page}` (local: FileResponse; AWS: S3 + CloudFront CDN)
      ↳ ✅ Both servers: `aws/server.py:2686`, `local/dev_server.py:253`
- [x] Dynamic nav: practice page links injected after `checkAuth()` in all HTML pages
      ↳ ✅ `settings.html:881-882` and other pages
- [x] TenantSettings: `primary_practice`, `addon_skills`, `addon_pages`
      ↳ ✅ `agent/models/tenant.py:29-31`
- [x] Server startup: `PracticeRegistry.load_builtin()` replaces direct `SkillRegistry.load_from_directory()`
      ↳ ✅ Both servers call it on init
- [x] **Milestone:** Built-in engineering practice works with pages served at `/p/engineering/sprint`

**Phase 6b — Practice Upload & Management**
- [x] `POST /api/practices/upload` — ZIP upload, validation (structure, safety, name uniqueness), extraction
      ↳ ✅ Both servers: `aws/server.py:2628`, `local/dev_server.py:2103`
- [x] Settings UI: Practices tab (select primary, upload, manage add-ons)
      ↳ ✅ `adapters/local/settings.html:972` (tab), lines 1119-1348 (upload UI)
- [x] Practice persistence (DynamoDB for AWS, SQLite for local)
      ↳ ✅ Registry loads from `data/practices/`; stored in tenant settings
- [x] S3-backed practice storage for uploaded ZIPs (pages + skills)
      ↳ ✅ `adapters/aws/s3_blob_store.py`; ZIPs stored at `practices/{name}/practice.zip`
- [x] Skill versioning and rollback
      ↳ ✅ `PracticeRegistry.install_zip()` version checking at lines 154-168
- [x] **Milestone:** Admin can upload a practice ZIP and activate it for their tenant

**Phase 6c — AWS Deployment**
- [ ] `deploy.sh`: sync built-in practice pages to S3 under `p/` prefix
      ↳ ⚠️ deploy.sh only syncs HTML/CSS/JS/PNG — practice assets not included
- [ ] CloudFront: `/p/*` cache behavior pointing to S3 origin
      ↳ ⚠️ `infra/aws/modules/cdn/main.tf` has no `/p/*` behavior — practice pages will 404 on AWS
- [ ] ECS task role: S3 GetObject/PutObject for `practices/*` prefix
- [x] Lambda hot-reload — pull uploaded practice skills from S3 on cold start
      ↳ ✅ `adapters/aws/lambda_handler.py:83-115` loads practices via `PracticeRegistry`
- [x] `t3nets-sdk` bundled into Docker image and Lambda ZIPs so uploaded practices resolve `t3nets_sdk` on cold start
      ↳ ✅ step 7.1 — see commit `69dd002`
- [ ] **Milestone:** Practices work end-to-end on AWS

**Phase 6d — Practices SDK (external practice authors)**

A standalone `t3nets-sdk` package (under `sdk/`) so practices can live in their own repos, import a stable typed contract, and ship without pulling the platform repo as a dependency.

- [x] `t3nets_sdk.models` + `t3nets_sdk.interfaces` — cloud-free core (Step 1)
- [x] `t3nets_sdk.testing` — `MockSecretsProvider`, `MockBlobStore`, `MockEventBus`, `MockConversationStore` (Step 2)
- [x] `t3nets_sdk.manifest` — pydantic validators for `practice.yaml` / `skill.yaml` (Step 3)
- [x] `t3nets` CLI — `practice init`, `practice validate`, `practice package` (Step 4)
      ↳ ✅ `sdk/t3nets_sdk/cli/`
- [x] `t3nets_sdk.contracts` — typed `SkillContext` / `SkillResult` worker contract (Step 5)
      ↳ ✅ platform migrated — old `dict → dict` worker shape retired
- [x] Built-in skills migrated to the new contract (`ping`, `sprint_status`, `release_notes`)
- [x] `t3nets practice run-local` + dev server `--extra-practice-dir` for out-of-tree development loops (Step 7)
- [x] Skill-owned rendering — skills set `text=` (verbatim) or `render_prompt=` (router AI formatter) on `SkillResult`; reserved transport keys survive SQS/Lambda
      ↳ ✅ `sdk/t3nets_sdk/contracts.py` + `adapters/shared/handlers/chat.py` + `adapters/aws/result_router.py`
- [x] Bundle SDK with Docker + Lambda deployments (Step 7.1)
- [ ] Publish `t3nets-sdk` to PyPI (pending: verify AWS deploy against the new contract end-to-end)
- [ ] **Milestone:** `pip install t3nets-sdk` + `t3nets practice init` gets a contributor from zero to a passing local test run

### Phase 7: Server Slim — Wiring Layer Cleanup

Now that handler logic is extracted (Phase 5d) and Practices + the SDK have landed on PyPI (Phase 6), collapse the route-wiring layer in both server entry points. Mostly mechanical moves, but they touch live entry points on AWS and local — gate on a smoke-test pass after each.

- [ ] Slim `adapters/aws/server.py` to ~400 lines (currently ~2,000 — handlers extracted, route wiring still inline)
- [ ] Slim `adapters/local/dev_server.py` to ~300 lines (currently ~1,460 — same reason)
- [ ] **Milestone:** No handler logic duplicated across servers; each handler file independently testable; both server files are thin wiring layers

### Phase 8: Dashboard & UX
- [x] Markdown rendering in chat responses
- [x] S3 + CloudFront CDN module: private bucket (OAC), path-based routing (`/api/*` → API GW, `/*` → S3), 5-min TTL
      ↳ ✅ Completed — `infra/aws/modules/cdn/`
- [x] CloudFront Function: extensionless path rewriting (`/chat` → `/chat.html`) at viewer-request stage
- [x] `deploy.sh`: HTML sync to S3 + CloudFront invalidation (`/*`) after ECS stabilises
- [x] Custom domain support — set `root_domain` in tfvars; Terraform provisions Route53 zone (optional), ACM cert in us-east-1, CloudFront aliases for apex + www, and a root-path 302 to `/chat`
      ↳ ✅ `infra/aws/modules/dns/` + `infra/aws/modules/cdn/` — see commit `b583747`
- [x] Dashboard theming — dark mode toggle
      ↳ ✅ `adapters/local/theme.js`; `data-theme` attribute in HTML pages
- [x] Skill configuration UI
      ↳ ✅ Settings page references `/api/skill/voice_config` and per-skill config panels
- [ ] Make the console/dashboard a full SPA with client-side routing
- [ ] Mobile-responsive layout
      ↳ ⚠️ No `@media` queries in chat.html — not responsive
- [ ] Conversation history browser
      ↳ ⚠️ History loads inline in chat via `/api/history`; no dedicated browsing UI

### Phase 9: Multi-cloud
- [ ] Set up another cloud (Azure or GCP)
- [ ] Update terraform to support deployment for another cloud
- [ ] Deploy and test


### Phase 10: Expand Skills
- [ ] Meeting prep skill (Google Calendar / Outlook)
- [ ] Email triage skill (Gmail / Outlook)
- [ ] Skill marketplace page in dashboard
- [ ] **Milestone:** 3+ skills across 2+ channels


### Phase 11: Email Delivery
- [ ] SES domain verification + IAM in Terraform
- [ ] HTML invite email template with tenant branding
- [ ] Call SES from create-invitation endpoint (copy-link stays as fallback)
- [ ] Call SES from platform create-tenant endpoint (same fallback pattern)
- [ ] **Milestone:** Invitations delivered by email; copy-link remains as fallback

### Phase 12: Long-Term Memory & Polish
- [ ] S3-based conversation summarization
- [ ] Additional channels (Slack, ~~WhatsApp~~)
      ↳ ✅ WhatsApp adapter completed via Whapi.cloud — see [handoff](../handoffs/022-whatsapp-channel-telegram-fixes.md)
- [ ] OSS contributor guides
- [ ] **Milestone:** Public release

### Phase 13: Expanded Developer Experience
- [ ] CLI tool for scaffolding new skills (beyond `t3nets practice init`)
- [ ] Local development docker-compose with hot reload
- [ ] Integration test harness
- [ ] **Milestone:** A contributor can scaffold, run, and integration-test a new skill end-to-end with one-command tooling

---

## Backlog 📋

### IdP-Agnostic Auth (Phase II)
- [ ] Define IdentityProvider interface (`agent/interfaces/identity_provider.py`)
- [ ] Cognito adapter implementing IdentityProvider
- [ ] Authentik adapter implementing IdentityProvider (standard OIDC)
- [ ] Inject IdentityProvider into AWS and local servers (replace direct boto3 calls)
- [ ] Docker Compose with Authentik for local dev (real auth instead of hardcoded local-admin)
- [ ] Authentik bootstrap script (`scripts/setup_authentik.py`)
- [ ] Token refresh endpoint + frontend refresh logic
- [ ] Documentation: ADR for IdP abstraction, local dev guide update

### AI & Models
- [x] Centralized model registry with per-provider resolution
- [x] Ollama / OpenAI-compatible provider for free local models (see Phase 5c)
- [ ] Per-tenant model selection
- [ ] Per-tier model configuration (conversational / formatting / routing models)
- [ ] Bedrock Intelligent Prompt Routing evaluation
- [ ] Token usage tracking per tenant
- [ ] Streaming responses (WebSocket transport ready — needs Bedrock streaming integration)

### Developer Experience
- [x] Migrate both servers to uvicorn ASGI + Starlette: persistent event loop, true async concurrency — no `asyncio.run()` per request
      ↳ ✅ `adapters/local/dev_server.py`, `adapters/aws/server.py`; `base_handler.py` deleted
- [x] Auto-reload dev server — uvicorn `--reload` flag available now that both servers use uvicorn
- [x] Strict mypy compliance — 0 errors across all 60 source files in `agent/` + `adapters/` (284 fixed)
- [x] License compliance — `THIRD_PARTY_LICENSES` with BSD-3-Clause attribution for uvicorn
- [ ] CLI tool for scaffolding new skills
- [ ] Local development docker-compose with hot reload
- [x] Unit test suite for router, rule engine, skills (tenant isolation, release notes, error handler)
- [ ] Integration test harness

### Settings & Integrations
- [ ] Partial save for integration secrets — null fields preserve existing DB values, blank/space intentionally clears. UI: existing hidden values show asterisk (no placeholder tooltip), empty fields show placeholder tooltip.
- [ ] Practice-level integration secrets — shared credentials (e.g., Chatterbox URL/token) scoped to a practice, accessible by all its skills. Replaces per-skill duplication.

### Platform
- [ ] Rate limiting per tenant
- [ ] Usage analytics / token tracking per tenant
- [ ] Billing / payment integration
- [ ] Audit log viewer
- [ ] Custom skill upload (tenant-provided skills)
- [ ] Role-based access beyond admin/member
- [ ] Notification preferences
- [ ] End-to-end encryption option
- [ ] SOC2 / compliance features

### Channels
- [ ] Slack adapter
- [x] WhatsApp adapter (Whapi.cloud) — [handoff](../handoffs/022-whatsapp-channel-telegram-fixes.md)
- [ ] SMS adapter (Twilio)
- [ ] Voice adapter (Twilio / Amazon Connect)
- [x] Telegram adapter
- [ ] Email adapter (SES)
- [ ] Discord adapter

### Cost Optimization
- [ ] Replace NAT Gateway with NAT instance (~$28/mo savings)
- [ ] Evaluate placing Fargate in public subnets (eliminate NAT entirely)
- [ ] Bedrock batch inference for non-real-time processing
- [ ] DynamoDB DAX caching if read-heavy patterns emerge

---

## Documentation

All project docs in `docs/`:

| Document | Description |
|----------|-------------|
| `ai-models-pricing.md` | Model options, pricing tables, tiered strategy |
| `aws-infrastructure.md` | Terraform modules, deployment steps, cost estimate |
| `local-development.md` | Quick start, prerequisites, dev server guide |
| `decision-log.md` | Architecture Decision Records (ADRs) |
| `dynamodb-schema.md` | Table schemas, key patterns, query examples |
| `hybrid-routing.md` | Three-tier routing architecture deep-dive |
| `plan-ollama-integration.md` | Ollama free AI model integration plan |

---

*Built with Bedrock, Terraform, and a lot of coffee.*
