# T3nets — Roadmap & TODO

**Last Updated:** February 25, 2026

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
- [x] Add avatar_url to TenantUser model + DynamoDB/SQLite persistence
      ↳ ✅ Completed — see [handoff notes](../handoffs/004-idp-agnostic-auth-phase1.md)
- [x] Seed a second tenant, verify isolation
      ↳ ✅ Completed — see [handoff notes](../handoffs/005-second-tenant-seed-and-isolation.md)
- [x] **Milestone:** Two teams onboarded, data fully isolated

### Phase 3: First External Channel
- [ ] Teams channel adapter (Azure Bot → webhook)
- [ ] Async skill execution via EventBridge → SQS → response handler
- [ ] **Milestone:** Team member asks sprint status in Teams, gets answer

### Phase 4: Expand Skills
- [x] Release notes skill — routing, --raw support, future release handling, Jira API v3 migration
      ↳ ✅ Completed — see [handoff notes](../handoffs/001-fix-release-notes-skill.md)
- [ ] Meeting prep skill (Google Calendar / Outlook)
- [ ] Email triage skill (Gmail / Outlook)
- [ ] Skill marketplace page in dashboard
- [ ] **Milestone:** 3+ skills across 2+ channels


### Phase 5: Practices — Skill Bundles & Customization
- [ ] Define Practice model (name, description, list of skill IDs)
- [ ] Bundle existing skills into default practices (e.g. "Engineering", "Project Management")
- [ ] Per-tenant practice selection (assign a practice to a tenant)
- [ ] Custom practices — allow tenants to create their own practice by selecting skills to add or remove
- [ ] Practice management UI in dashboard (browse, select, customize, save)
- [ ] Persist custom practices in DynamoDB / SQLite tenant settings
- [ ] **Milestone:** Tenants can pick a practice or build a custom one from the skill catalog

### Phase 6: Dashboard & UX
- [x] Markdown rendering in chat responses
- [ ] Dashboard theming — polished design system (dark mode, consistent components)
- [ ] Make the console/dashboard an SPA, serve static HTML from CDN with pure AJAX
- [ ] Mobile-responsive layout
- [ ] Conversation history browser
- [ ] Skill configuration UI

### Phase 7: Long-Term Memory & Polish
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
- [ ] Streaming responses (SSE or WebSocket)

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
- [ ] Telegram adapter
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
