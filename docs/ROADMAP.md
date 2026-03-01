# T3nets — Roadmap & TODO

**Last Updated:** March 2, 2026

---

## Completed ✅

### Phase 0: Design & Prototype
- [x] Master architecture document
- [x] Project scaffolded (interfaces, models, channels, skills)
- [x] GitHub repo live (WildEllie/t3nets)
- [x] Sprint status skill (Jira integration)
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

---

## Up Next 🔜

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
- [ ] Verify horizontal scaling: run 2+ ECS tasks, confirm no message loss or duplicate responses
- [x] **Milestone:** Skills run on Lambda, router is stateless, container scales horizontally

      ↳ 📋 Implementation — see [handoff notes](../handoffs/014-phase-3b-implementation.md)
      ↳ 📋 WebSocket push — see [handoff notes](../handoffs/015-websocket-api-gateway.md)

### Phase 4: Invitation Flow
      ↳ 📐 Full plan: [plan-invitation-signup-flow.md](plan-invitation-signup-flow.md)

Two-path signup: existing flow creates a new tenant; invited users join an existing tenant and skip onboarding.

**Phase 4a — Backend**
- [ ] Add `Invitation` dataclass to `agent/models/tenant.py` (code, tenant_id, email, role, status, TTL)
- [ ] DynamoDB invitation storage: `pk=INVITE#{code}`, `sk=META`, TTL attribute for auto-cleanup
- [ ] Terraform: enable DynamoDB TTL on tenants table (`infra/aws/modules/data/main.tf`)
- [ ] `POST /api/admin/tenants/{id}/invitations` — create invitation, return code + URL (admin only)
- [ ] `GET /api/admin/tenants/{id}/invitations` — list pending invitations (admin only)
- [ ] `DELETE /api/admin/tenants/{id}/invitations/{code}` — revoke invitation (admin only)
- [ ] `GET /api/invitations/validate?code=xxx` — public; return tenant name + email if valid
- [ ] `POST /api/invitations/accept` — validate JWT, match email, link user to tenant, mark accepted
- [ ] Terraform: add public API Gateway routes for validate + accept (`infra/aws/modules/api/main.tf`)
- [ ] Local dev: mock invitation endpoints in `dev_server.py`, SQLite storage in `sqlite_tenant_store.py`

**Phase 4b — UI**
- [ ] `/join?code=xxx` page — validate code, show invitation panel (tenant name, email, login or signup)
- [ ] Signup panel: invitation-aware — email pre-filled + locked, call accept after verify, redirect to `/chat`
- [ ] Login panel: invitation-aware — call accept after login, redirect to `/chat`
- [ ] Settings → Team tab: current members table, pending invitations table (copy link / revoke), invite form
- [ ] `GET /api/admin/tenants/{id}/users` endpoint for Team tab member list

**Phase 4c — Future: Email Delivery**
- [ ] SES domain verification + IAM in Terraform
- [ ] HTML invite email template with tenant branding
- [ ] Call SES from create-invitation endpoint (copy-link stays as fallback)

- [ ] **Milestone:** Admin can invite users by link; invited users join the correct tenant and land in chat


### Phase 5: Expand Skills
- [x] Release notes skill — routing, --raw support, future release handling, Jira API v3 migration
      ↳ ✅ Completed — see [handoff notes](../handoffs/001-fix-release-notes-skill.md)
- [ ] Meeting prep skill (Google Calendar / Outlook)
- [ ] Email triage skill (Gmail / Outlook)
- [ ] Skill marketplace page in dashboard
- [ ] **Milestone:** 3+ skills across 2+ channels


### Phase 6: Practices — Skill Bundles & Customization
- [ ] Define Practice model (name, description, list of skill IDs)
- [ ] Bundle existing skills into default practices (e.g. "Engineering", "Project Management")
- [ ] Per-tenant practice selection (assign a practice to a tenant)
- [ ] Custom practices — allow tenants to create their own practice by selecting skills to add or remove
- [ ] Practice management UI in dashboard (browse, select, customize, save)
- [ ] Persist custom practices in DynamoDB / SQLite tenant settings
- [ ] `POST /api/skills/upload` — accept a skill ZIP (worker.py + skill.yaml), validate, create Lambda + EventBridge rule
- [ ] `POST /api/practices/upload` — accept a practice bundle ZIP (multiple skill ZIPs), deploy all skills
- [ ] S3-backed skill storage for uploaded ZIPs
- [ ] Lambda hot-reload — pull skills from S3 on cold start
- [ ] Skill versioning and rollback
- [ ] **Milestone:** Tenants can pick a practice or build a custom one from the skill catalog

### Phase 7: Dashboard & UX
- [x] Markdown rendering in chat responses
- [ ] Dashboard theming — polished design system (dark mode, consistent components)
- [ ] Make the console/dashboard an SPA, serve static HTML from CDN with pure AJAX
- [ ] Mobile-responsive layout
- [ ] Conversation history browser
- [ ] Skill configuration UI

### Phase 8: Long-Term Memory & Polish
- [ ] S3-based conversation summarization
- [ ] Additional channels (Slack, WhatsApp)
- [ ] OSS contributor guides
- [ ] **Milestone:** Public release

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
- [ ] Per-tenant model selection
- [ ] Per-tier model configuration (conversational / formatting / routing models)
- [ ] Bedrock Intelligent Prompt Routing evaluation
- [ ] Token usage tracking per tenant
- [ ] Streaming responses (WebSocket transport ready — needs Bedrock streaming integration)

### Developer Experience
- [ ] Auto-reload dev server (watchdog / uvicorn)
- [ ] CLI tool for scaffolding new skills
- [ ] Local development docker-compose with hot reload
- [x] Unit test suite for router, rule engine, skills (tenant isolation, release notes, error handler)
- [ ] Integration test harness

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
- [ ] WhatsApp adapter
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

---

*Built with Bedrock, Terraform, and a lot of coffee.*
