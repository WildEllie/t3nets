# T3nets — Roadmap & TODO

**Last Updated:** February 21, 2026

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
- [x] Bedrock model access verified (Sonnet 4.5, Haiku 4.5, Sonnet 4)
- [x] AI model pricing research (Claude vs Nova options)
- [x] Project documentation (docs/ folder)

---

## Up Next 🔜

### Phase 1b: Deploy & Settings (Current)
- [x] `terraform apply` — deploy infrastructure to AWS
- [ ] Simplify AI model references into a single location if possible
- [ ] Settings page in dashboard (model selection per routing tier)
- [ ] Server restart from dashboard (dev_server hot reload)
- [ ] Per-tier model configuration (conversational / formatting / routing models)
- [ ] Test Nova models for formatting tier
- [ ] **Milestone:** Platform running on AWS with configurable AI models

### Phase 2: Multi-Tenancy
- [ ] Cognito user pool + auth flow
- [ ] Tenant resolution from JWT
- [ ] Admin Lambda (tenant/team/integration CRUD)
- [ ] Onboarding wizard (React)
- [ ] Seed a second tenant, verify isolation
- [ ] **Milestone:** Two teams onboarded, data fully isolated

### Phase 3: First External Channel
- [ ] Teams channel adapter (Azure Bot → webhook)
- [ ] Async skill execution via EventBridge → SQS → response handler
- [ ] **Milestone:** Team member asks sprint status in Teams, gets answer

### Phase 4: Expand Skills
- [ ] Meeting prep skill (Google Calendar / Outlook)
- [ ] Email triage skill (Gmail / Outlook)
- [ ] Skill marketplace page in dashboard
- [ ] **Milestone:** 3+ skills across 2+ channels

### Phase 5: Long-Term Memory & Polish
- [ ] S3-based conversation summarization
- [ ] Additional channels (Slack, WhatsApp)
- [ ] OSS contributor guides
- [ ] **Milestone:** Public release

---

## Backlog 📋

### Dashboard & UX
- [ ] Dashboard theming — polished design system (dark mode, consistent components)
- [ ] Mobile-responsive layout
- [ ] Markdown rendering in chat responses
- [ ] Conversation history browser
- [ ] Skill configuration UI

### AI & Models
- [ ] Per-tenant model selection
- [ ] Bedrock Intelligent Prompt Routing evaluation
- [ ] Token usage tracking per tenant
- [ ] Streaming responses (SSE or WebSocket)
- [ ] Nova 2 Lite/Pro evaluation for tool use

### Developer Experience
- [ ] Auto-reload dev server (watchdog / uvicorn)
- [ ] CLI tool for scaffolding new skills
- [ ] Local development docker-compose with hot reload
- [ ] Unit test suite for router, rule engine, skills
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
