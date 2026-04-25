<p align="center">
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="docs/logo-dark.png" />
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo-light.png" />
    <img alt="T3nets" src="docs/logo-light.png" width="400" />
  </picture>
</p>
# The open-source enterprise app platform with AI in the foundation, not bolted on.

**Multi-tenant. Cloud-native. Safe to run at org scale from day one.**

[MIT License] [Python 3.12+] [AWS · GCP · Azure]

---

T3nets is the open-source foundation for organizations that want AI on their own terms — deployed in **your** cloud, shaped around **your** workflows, and safe to run at org scale without a six-month platform build first.

Every team today faces the same question: *how do we actually get AI into the work?* The answers on offer are all bad.

| The choice today | What you get | What it costs you |
| --- | --- | --- |
| **Rent closed SaaS** | Safe-ish, fast to start | Your data lives in their tenant. Priced per seat. Can't shape it to your org. Lock-in by design. |
| **Build it yourself** | Full control | Six months of senior platform work — multi-tenancy, auth, async execution, isolation, cost controls, secrets, audit — before the first useful agent response. Most teams don't have that runway. |
| **Let it happen organically** | Momentum | Agents running in permissive modes on dev laptops. Browser tools with full session access. MCP servers wired straight to prod. Shadow tooling nobody approved. The CFO finds out when the bill arrives; security finds out when something leaks. |

**T3nets is the fourth option.** The responsible path, made cheap enough that nobody has to pick the unsafe one out of desperation.

## What you don't have to build

The undifferentiated platform work is already done. Clone it, deploy it, move on.

| | |
| --- | --- |
| 🏢 **Multi-tenancy, first-class** | Shared compute, isolated data. Cognito auth, JWT, tenant onboarding wizard. Not bolted on — baked in from commit zero. |
| 🔐 **Safe by construction** | Scoped secrets per tenant. Async execution boundaries between the router and skills. Audit trails. No dev laptops holding prod credentials. No YOLO-mode agents. |
| 💸 **Cost-aware routing** | Hybrid routing handles known requests at $0 via a tenant-specific regex engine the AI regenerates itself. Only escalates to the model when it has to. 50–60% off your AI bill, measured. |
| ☁️ **Cloud-agnostic core** | Business logic has zero cloud imports. AWS is the reference; GCP and Azure adapters are in flight. You own the infra. No lock-in, ever. |
| ⚡ **Async from day one** | EventBridge → Lambda → SQS → WebSocket. The router never blocks on a skill. Retries, DLQs, idempotency — handled. |
| 🧩 **Practices, not code** | The unit of customization (see below). Shape the platform without touching the platform. |

## Practices: the part that's yours

Everything above is the foundation. **Practices are what make a t3nets deployment *yours*.**

A Practice is a bundle — skills, dashboards, workflows, behaviors — that teaches a tenant how to work. Your sales org's Practice looks nothing like your SRE team's, and neither looks like the one a hospital network deploys. All three run on the same platform. All three stay isolated. All three get the same cost guarantees, the same safety boundaries, the same portability.

You don't fork t3nets to customize it. You write a Practice, drop it in, and the platform picks it up. That's the seam. That's where your domain knowledge lives. Everything else is ours to maintain.

*Practices are live today. Build yours with the [`t3nets-sdk`](sdk/) package and the `t3nets practice` CLI — scaffold, validate, run locally against a dev server, package. See [Extend](#extend) below.*

## Where this is going

T3nets started as an AI agent platform because that's the part of an enterprise stack that's hardest to build responsibly today. But the foundation underneath — multi-tenancy, async execution, scoped secrets, audit, cloud portability — is the same foundation an enterprise *application* platform needs. So that's where it's heading.

The next phase brings the pieces that close the gap: **tenant-owned data** (entities and schemas a Practice can declare and skills can read and write), **non-AI invocation paths** (scheduled triggers, webhooks, dashboard buttons — the same skill machinery, called from anywhere), and the groundwork for **Practice-defined pages** so the platform that runs your AI agents also runs the apps those agents work on.

The wedge is simple: every incumbent in this category is bolting AI onto a platform designed before LLMs existed. T3nets gets to design the data model, the workflow engine, and the page system *knowing* an AI agent is a first-class caller of all three. That's the difference you can feel.

---

## See it in action

> **You:** What's the sprint status?
>
> **T3nets:** 🏃 **NOVA S12E4** — "Finish Lynx"
> 41% done, 5 days left. 2 blocked items. Risk: **HIGH**.
> Suggestion: Descope the test tickets and focus on getting blocked items through.

One message. Tenant-scoped. Routed at $0 because the regex engine already knew what "sprint status" meant for this workspace. No API call to the model. No credentials on a laptop. No shadow tooling. This is the baseline, not the ceiling.

## Architecture

### Hybrid Routing Engine

The routing engine is the core differentiator. Every message passes through two tiers, stopping as soon as a decision can be made.

**Tier 1 — AI-generated regex rules ($0, <1ms)**

Each tenant gets a compiled regex engine built specifically for their combination of enabled skills. When a tenant enables or disables a skill, the AI regenerates the rules automatically — no hand-maintained patterns. The AI understands the full skill set and generates patterns that minimise cross-skill ambiguity: when `sprint_status` and `release_notes` are both active, it knows "release status" is ambiguous and disambiguates accordingly. If Tier 1 matches, the skill is invoked directly — no AI call.

**Tier 2 — AI reasoning (~$0.01-0.05, 1 API call)**

If Tier 1 finds no match, the AI model receives the message alongside tool definitions for every enabled skill and descriptions of every disabled skill. It makes one of three decisions:

- **Enabled skill matched** → extract parameters and invoke the skill
- **Disabled skill matched** → respond: "that feature isn't enabled for your workspace, contact your admin" (no skill executed)
- **No skill is the right choice** → respond conversationally, with full awareness of what the platform can do — answers general questions, and can steer the user toward a relevant skill

Every Tier 2 interaction is logged as training data. When enough examples accumulate, an admin can trigger a rule engine rebuild that incorporates them, gradually pushing more traffic into the $0 Tier 1 path.

---

## Quick Start

```bash
git clone https://github.com/WildEllie/t3nets.git
cd t3nets

python3 -m venv venv && source venv/bin/activate
pip install -e ".[local,dev]"   # pulls t3nets-sdk from PyPI

cp .env.example .env        # Add your Anthropic API key
python -m adapters.local.dev_server
```

Open **http://localhost:8080** — you're chatting with your agent.

Or with Docker:

```bash
docker compose up
```

---

## AI Providers

T3nets supports multiple AI providers simultaneously. You choose which ones to run — on local you can use Anthropic's API, Ollama, or both. On AWS you can use Bedrock, Ollama as an ECS sidecar, or both.

The settings page exposes all models available for your active providers. Per-tenant model selection is stored and resolved at request time, with graceful fallback if the selected model isn't available.

### Local development

| Config | Provider |
|--------|----------|
| `ANTHROPIC_API_KEY` set in `.env` | Anthropic direct API |
| `OLLAMA_API_URL` set in `.env` | Ollama (OpenAI-compatible) |
| Both set | Both run simultaneously |

**Anthropic** (default):

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
```

**Ollama** (free, no API key):

```bash
# .env
OLLAMA_API_URL=http://localhost:11434
```

Run Ollama separately (`ollama serve`), or use the Docker Compose override which wires everything automatically:

```bash
docker compose -f docker-compose.yml -f docker-compose.ollama.yml up

# First run — pull the model inside the container
docker compose -f docker-compose.yml -f docker-compose.ollama.yml exec ollama ollama pull llama3.2:3b
```

The override sets `OLLAMA_API_URL=http://ollama:11434` (Docker internal DNS) and waits for Ollama to pass its health check before starting the router.

### AWS

| Config | Provider |
|--------|----------|
| `BEDROCK_MODEL_ID` env var set | AWS Bedrock (Converse API, IAM auth) |
| `OLLAMA_API_URL` env var set | Ollama sidecar in the same ECS task |
| Both set | Both run simultaneously |

**Bedrock** is configured via Terraform and `BEDROCK_MODEL_ID` (e.g. `us.amazon.nova-lite-v1:0`).

**Ollama sidecar** is enabled via Terraform feature flags in your `.tfvars`:

```hcl
use_ollama       = true
ollama_model     = "llama3.2:3b"
ollama_memory_mb = 4096
```

ECS uses `awsvpc` networking — both containers share `localhost`, so the router reaches Ollama at `http://localhost:11434` with no security group changes needed. The Ollama container pulls the model on startup (allow up to 3 minutes on first deploy). The router waits for Ollama's health check to pass before accepting traffic.

### Available models

| Model | Providers | Notes |
|-------|-----------|-------|
| Claude Sonnet 4.6 | anthropic, bedrock | Latest Claude |
| Claude Sonnet 4.5 | anthropic, bedrock | |
| Amazon Nova Pro | bedrock | |
| Amazon Nova Lite | bedrock | |
| Amazon Nova Micro | bedrock | |
| Llama 3.2 3B | ollama | Free, fits in 2 GB RAM |

See [AI Models & Pricing](docs/ai-models-pricing.md) for cost comparison and tiered strategy.

---

## Deploy to the Cloud

Production-grade Terraform infrastructure: VPC, ECS Fargate, API Gateway, DynamoDB, Bedrock, Secrets Manager.

```bash
cd infra/aws
terraform init && terraform apply -var-file=environments/dev.tfvars

# Seed data and deploy container
./scripts/seed.sh
./scripts/deploy.sh
```

AWS is the reference implementation. GCP and Azure adapters are in progress — see [adapters/gcp/](adapters/gcp/) and [adapters/azure/](adapters/azure/).

### Custom Domain

Set `root_domain` in your tfvars and Terraform handles the rest — Route53 zone (optional, skip if DNS is managed elsewhere), ACM certificate in us-east-1 with DNS validation, CloudFront aliases for both the apex and `www`, and a root-path 302 to `/chat`. Cognito callback URLs and CORS origins auto-derive from `root_domain`, so one variable covers the chain.

```hcl
# environments/dev.tfvars
root_domain = "t3nets.dev"
```

See [AWS Infrastructure Guide](docs/aws-infrastructure.md) for full deployment details.

---

## Extend

### Add a Skill

A skill is a `skill.yaml` + `worker.py` pair. The YAML tells the router when to invoke it and what parameters the AI model should extract. The worker is pure business logic — no cloud imports, no framework knowledge, no awareness of how it was invoked. It receives a typed `SkillContext` and returns a `SkillResult` from the [`t3nets-sdk`](sdk/) package.

```yaml
# skill.yaml
name: my_skill
description: >
  What this skill does and when the AI model should use it.
  Be specific — this becomes part of the AI model's tool definition.

triggers:
  - "phrase that routes here directly"   # feeds the $0 rule engine
  - "another trigger phrase"

requires_integration: some_service   # credential keys injected at runtime

parameters:
  type: object
  properties:
    action:
      type: string
      enum: [summary, detail]
  required: [action]
```

```python
# worker.py
from t3nets_sdk.contracts import SkillContext, SkillResult


async def execute(ctx: SkillContext, params: dict) -> SkillResult:
    # ctx.secrets     — integration credentials for this tenant
    # ctx.blob_store  — scoped blob handle (optional)
    # ctx.raw         — True when the user appended --raw (skip rendering)
    data = {"count": 42, "items": [...]}

    # Three rendering modes — skills decide how their output reaches the user:
    #   SkillResult.ok(data)                             # router falls back to a generic format prompt
    #   SkillResult.ok(data, text="42 items")            # verbatim, zero AI tokens
    #   SkillResult.ok(data, render_prompt="Lead with ...")  # router's AI formatter uses your prompt
    return SkillResult.ok(data, render_prompt="Summarise as a bulleted list with bold labels.")
```

The router picks it up automatically — no registration needed. Trigger phrases feed the hybrid rule engine so common requests are handled at $0 without an AI call.

### Build a Practice

A Practice bundles skills + pages + prompts into a single uploadable ZIP. Practices typically live in their own repo — `t3nets-sdk` is the only t3nets dependency they need.

```bash
pip install t3nets-sdk

t3nets practice init my-practice       # scaffold a new practice repo
cd my-practice
t3nets practice validate               # lint practice.yaml + skill.yaml against pydantic schema
t3nets practice run-local              # spin up a dev server with this practice wired in
t3nets practice package                # produce a ZIP ready for the Practices tab in settings
```

`run-local` uses the platform's dev server with `--extra-practice-dir` pointing at your repo, so the in-tree built-ins and your external practice both register. The SDK also ships `Mock*` doubles (`MockSecretsProvider`, `MockBlobStore`, `MockEventBus`, `MockConversationStore`) for unit-testing workers without spinning up cloud services.

See [sdk/README.md](sdk/README.md) for the full SDK surface.

**How a skill executes in the cloud:**

On AWS, skill execution is fully async and decoupled from the router:

```
User message → Router (ECS Fargate)
  → Tier 1: AI-generated regex engine ($0, <1ms)
      → match → invoke skill directly
      → no match ↓
  → Tier 2: AI model + enabled skill tools + disabled skill context
      → enabled skill matched → invoke skill
          → EventBridge → Lambda (loads worker, fetches secrets)
          → Lambda → SQS → Router SQS poller
          → AI model formats result → user (WebSocket / Teams / Telegram)
      → disabled skill matched → "not enabled, contact your admin"
      → no skill matched → freeform response (general knowledge, skill suggestions)
          → interaction logged as training data to improve Tier 1 over time
```

The router returns immediately after publishing to EventBridge — it never blocks on skill execution. Lambda handles retries, and a DLQ catches failures after 3 attempts. An idempotency check in the Lambda handler (via DynamoDB) ensures each skill runs exactly once even if EventBridge retries the event.

**Locally**, the same worker runs synchronously via `DirectBus` — no Lambda, no SQS, no EventBridge. The `execute()` function is just called inline.

See [agent/skills/sprint_status/](agent/skills/sprint_status/) for a full working example.

### Add a Channel

Implement `ChannelAdapter` — 5 methods. See [agent/channels/](agent/channels/).

### Add a Cloud Provider

Implement 5 interfaces: `AIProvider`, `ConversationStore`, `EventBus`, `SecretsProvider`, `BlobStore`. See [adapters/](adapters/) for the local and AWS implementations.

---

## Project Structure

```
t3nets/
├── agent/                    # Cloud-agnostic core (zero cloud imports)
│   ├── router/               # Hybrid routing engine (3-tier)
│   ├── skills/               # Skill definitions + workers
│   ├── channels/             # Channel adapters (dashboard, Teams, Telegram)
│   ├── memory/               # Conversation history management
│   ├── interfaces/           # Abstract contracts
│   └── models/               # Shared data models
├── adapters/                 # Cloud-specific implementations
│   ├── local/                # Anthropic API, SQLite, .env, dev server + UI
│   ├── aws/                  # Bedrock, DynamoDB, Secrets Manager, ECS server
│   ├── gcp/                  # GCP (in progress)
│   └── azure/                # Azure (in progress)
├── infra/aws/                # Terraform modules
├── scripts/                  # Deploy & seed scripts
└── docs/                     # Architecture docs, decision log, roadmap
```

---

## Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| 0 | Done | Design, prototype, hybrid routing, dev server |
| 1 | Done | AWS infrastructure — Terraform, Bedrock, DynamoDB |
| 1b | Done | Deploy to AWS, settings page, AI model registry |
| 2 | Done | Multi-tenancy — Cognito auth, tenant isolation, onboarding wizard |
| 2b | Done | Tenant management — skill toggles, integration config from dashboard |
| 3 | Done | External channels — Teams, Telegram adapters |
| 3b | Done | Async skills — EventBridge + Lambda + SQS + WebSocket |
| 4 | Done | Invitation flow — link-based invites, join page, team management |
| 4.5 | Done | Session management — silent refresh, idle expiry, role-based access |
| 4.6 | Done | Platform admin — tenant lifecycle (create, suspend, delete) |
| 5 | Done | AI-generated rule engine — per-tenant rules, admin training tools, Ollama free models |
| 5d | In progress | Server refactor — shared handlers extracted; `practices/registry` split + `admin_api` modernization next |
| 6 | In progress | Practices — team bundles with SDK, CLI, skill-owned rendering (6a/6b/6d done; 6c AWS asset sync + PyPI publish pending) |
| 7 | Planned | Server slim — collapse route wiring in both server entry points (gated on Phase 6 SDK publish) |
| 8 | In progress | Dashboard & UX — CloudFront/S3, custom domain, dark mode done; SPA + mobile pending |
| 9 | Planned | Email delivery — SES invitations, tenant branding |
| 10 | Planned | Expand skills — meeting prep, email triage |
| 11 | Planned | Multi-cloud — Azure / GCP adapters |
| 12 | Planned | Long-term memory, more channels, public release |
| 13 | Planned | Expanded developer experience — skill scaffolding CLI, hot-reload compose, integration test harness |

Full roadmap: [docs/ROADMAP.md](docs/ROADMAP.md)

---

## Documentation

| Guide | Description |
|-------|-------------|
| [Local Development](docs/local-development.md) | Quick start, dev server, local adapters |
| [AWS Infrastructure](docs/aws-infrastructure.md) | Terraform, deployment, Bedrock, DynamoDB |
| [Hybrid Routing](docs/hybrid-routing.md) | Three-tier routing engine, cost analysis |
| [AI Models & Pricing](docs/ai-models-pricing.md) | Model options and tiered strategy |
| [DynamoDB Schema](docs/dynamodb-schema.md) | Table design, key patterns |
| [Decision Log](docs/decision-log.md) | Architecture Decision Records (13 ADRs) |
| [Roadmap](docs/ROADMAP.md) | Phases, backlog, what's next |

---

## Contributing

Contributions welcome. Open an issue to discuss what you'd like to change, or submit a PR.

## License

[MIT](LICENSE)

---

## The Meaning Behind t3nets

**t3nets = Trusted, Tooling, Tenant Networks**

At its core, t3nets represents the convergence of three foundational layers:

### 1. Tenant

Multi-tenant by design. t3nets is built to serve multiple teams within an organisation, multiple organisations within a SaaS environment, and isolated environments — local, containerised, or cloud. Tenancy isn't an afterthought; it's a first-class architectural principle.

### 2. Tools

A platform to manage, orchestrate, and govern tools, skills, capabilities, and AI-augmented workflows. t3nets becomes the control plane for organisational intelligence.

### 3. Trust

Open source. Deploy anywhere. Cloud-agnostic. Infrastructure as Code.

Trust comes from transparency (OSS), portability (local → container → AWS/GCP/Azure), infrastructure ownership (no lock-in), and reproducibility. When AI is orchestrating skills and tools across tenants, trust isn't optional.

---

### Why "Networks"?

t3nets is a mesh of networked, connected intelligence:

- Networks of tenants
- Networks of tools
- Networks of AI-driven skills
- Networks of environments
- Networks across clouds

Not just a platform — an orchestrated mesh of capabilities.

### Why the "3"?

The "3" signals structured architecture and intentional depth:

- 3 layers — Tenant / Tooling / Trust
- 3 deployment modes — Local / Container / Cloud
- 3 major cloud targets — AWS / GCP / Azure
- 3 operational domains — People / Tools / AI

Not just a name — a system philosophy.

## Who's behind this

T3nets is built by [Ellie Portugali](https://github.com/WildEllie) — 18+ years designing hybrid and cloud-native architectures for regulated and high-stakes environments, including medical devices, genomic analysis pipelines, IoT and resource management consoles, and hosting platforms. Much of that work involved getting compliance right the first time under GDPR, HIPAA, ISO 27001, and ECB frameworks — because the second time is usually a rewrite. The patterns t3nets encodes as defaults (tenant isolation, scoped secrets, audit trails, async execution boundaries, cost ceilings) are the ones that kept showing up across those projects. I couldn't find an open-source project that had distilled them into a deployable platform layer, so t3nets is that project.

In recent years, working as a fractional VP R&D / CTO across several organizations, including access control technology at [Outlocks](https://outlocks.com).