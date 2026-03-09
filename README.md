<p align="center">
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="docs/logo-dark.png" />
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo-light.png" />
    <img alt="T3nets" src="docs/logo-light.png" width="400" />
  </picture>
</p>

<p align="center">
  <strong>Open-source, multi-tenant AI agent platform for teams.</strong><br/>
  Connect your tools. Talk through your channels. Cut AI costs by 60%.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License" /></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.12+-3776AB.svg?logo=python&logoColor=white" alt="Python 3.12+" /></a>
  <a href="docs/aws-infrastructure.md"><img src="https://img.shields.io/badge/cloud-AWS%20%7C%20GCP%20%7C%20Azure-FF9900.svg?logo=amazonaws&logoColor=white" alt="Multi-cloud" /></a>
</p>

---

T3nets is the layer between your team's communication channels and their productivity tools. Instead of switching between Jira, GitHub, email, and calendars — you ask a question and get an answer.

A **hybrid routing engine** handles the heavy lifting: known requests are matched locally for $0, while complex queries are routed to the AI model with full tool access. The result is an AI agent platform that's smart when it needs to be and free when it doesn't.

```
You (Dashboard / Teams / Telegram) → T3nets → AI Model → Your Tools → Answer
```

### See it in action

> **You:** What's the sprint status?
>
> **T3nets:** 🏃 **NOVA S12E4** — "Finish Lynx"
> 41% done, 5 days left. 2 blocked items. Risk: **HIGH**.
> Suggestion: Descope the test tickets and focus on getting blocked items through.

---

## Why T3nets

| | |
|---|---|
| **Cut AI costs 50-60%** | Hybrid routing handles known requests at $0 via regex, only escalating to the AI model when needed |
| **Multi-tenant from day one** | Shared compute, isolated data. Cognito auth, JWT, tenant onboarding wizard |
| **Cloud-agnostic core** | Business logic has zero cloud imports. Pluggable adapters for AWS, Azure, GCP |
| **Skills, not code** | Add capabilities with a `skill.yaml` + `worker.py` — no router changes needed |
| **Practices** *(coming soon)* | Team experience bundles: skills + custom pages + functionality, uploadable as ZIPs |
| **Any channel** | Dashboard, Teams, Telegram today. Slack, WhatsApp, SMS on the roadmap |

---

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
pip install -e ".[local,dev]"

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

See [AWS Infrastructure Guide](docs/aws-infrastructure.md) for full deployment details.

---

## Extend

### Add a Skill

A skill is a `skill.yaml` + `worker.py` pair. The YAML tells the router when to invoke it and what parameters the AI model should extract. The worker is pure business logic — no cloud imports, no framework knowledge, no awareness of how it was invoked.

```yaml
# agent/skills/my_skill/skill.yaml
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
# agent/skills/my_skill/worker.py
def execute(params: dict, secrets: dict) -> dict:
    # params: extracted by the AI model from the user's message
    # secrets: injected by the infrastructure layer (env locally, Secrets Manager on AWS)
    return {"result": "..."}
```

The router picks it up automatically — no registration needed. Trigger phrases feed the hybrid rule engine so common requests are handled at $0 without an AI call.

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
| 5 | Planned | Expand skills — GitHub Issues, Google Calendar, Email triage |
| 6 | Planned | Email delivery — SES invitations, tenant branding |
| 7 | Planned | Practices — team experience bundles (skills + pages as uploadable ZIPs) |
| 8 | Planned | Dashboard & UX — SPA, dark mode, mobile responsive |
| 9 | Planned | Long-term memory, more channels, public release |

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
