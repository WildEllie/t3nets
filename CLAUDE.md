# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is T3nets

Multi-tenant AI agent platform that connects teams to productivity tools (Jira, GitHub, etc.) via Claude. Messages flow through a hybrid routing engine that minimizes AI costs by handling simple requests locally.

## Commands

```bash
# Install for local development
pip install -e ".[local,dev]"

# Run local dev server (serves dashboard at http://localhost:8080)
python -m adapters.local.dev_server

# Run with Docker
docker compose up

# Linting
ruff check .
ruff format --check .

# Type checking
mypy agent/ adapters/

# Tests
pytest
pytest tests/path/to/test_file.py        # single file
pytest tests/path/to/test_file.py::test_name  # single test

# AWS deployment
cd infra/aws && terraform init && terraform apply -var-file=environments/dev.tfvars
./scripts/seed.sh     # populate DynamoDB + Secrets Manager from .env
./scripts/deploy.sh   # build container → push to ECR → update ECS
```

## Architecture

### Cloud-Agnostic Core (`agent/`)

All business logic lives in `agent/` with **zero cloud imports**. Cloud-specific code lives in `adapters/`.

- **`agent/router/`** — Hybrid routing engine with three cost tiers:
  - Tier 1: Regex pattern matching for conversational messages ($0, <1ms)
  - Tier 2: Rule-matched skill triggers without full AI call (~$0.01)
  - Tier 3: Full Claude with tools for complex queries (~$0.02-0.05)
- **`agent/skills/`** — Each skill is a directory with `skill.yaml` (metadata, triggers, parameters) + `worker.py` (execute function). `registry.py` loads skills and provides tool definitions for Claude.
- **`agent/interfaces/`** — Abstract contracts: `AIProvider`, `ConversationStore`, `TenantStore`, `EventBus`, `SecretsProvider`, `BlobStore`, `ChannelAdapter`
- **`agent/models/`** — Shared dataclasses: `Tenant`, `TenantUser`, `TenantSettings`, `InboundMessage`, `OutboundMessage`, `RequestContext`
- **`agent/memory/`** — Conversation history management
- **`agent/channels/`** — Channel adapters (dashboard, future: Teams/Slack)

### Adapters (`adapters/`)

Each adapter directory wires the interfaces to a specific environment:

- **`adapters/local/`** — Anthropic direct API, SQLite stores, .env secrets, `dev_server.py` serves dashboard + API
- **`adapters/aws/`** — Bedrock (Converse API), DynamoDB (single-table design), Secrets Manager, ECS Fargate server
- **`adapters/azure/`**, **`adapters/gcp/`** — Stubs for future cloud support

### Infrastructure (`infra/aws/`)

Terraform modules: networking (VPC, NAT), data (DynamoDB), secrets, ecr, compute (ECS Fargate), api (API Gateway HTTP v2). Dev cost ~$35-50/month (NAT Gateway is the largest cost).

### Request Flow

```
Inbound message → ChannelAdapter.parse_inbound()
  → Router.handle_message()
    → Resolve tenant + user from channel identity
    → Build RequestContext, load conversation history
    → RuleBasedRouter: try Tier 1 → Tier 2 → Tier 3
      → If Tier 3: AIProvider.chat() with skill tool definitions
      → Skill workers execute via EventBus
    → Save turn to ConversationStore
  → ChannelAdapter.send_response()
```

## Code Style

- Python 3.12+, async/await throughout
- Ruff: line-length 100, rules E/F/I/N/W
- MyPy: strict mode enabled
- Pytest: asyncio_mode = "auto", test directory is `tests/`

## Key Design Decisions

- **Hybrid routing** cuts AI costs 50-60% vs routing everything through Claude (see `docs/hybrid-routing.md`)
- **Single-table DynamoDB design** for tenants; conversations table uses composite keys `{tenant}#{channel}#{user}` (see `docs/dynamodb-schema.md`)
- **ECS Fargate over Lambda** for the router (persistent connections, simpler local dev parity)
- **Bedrock Converse API** (not invoke_model) for structured tool use
- **Debug mode**: append `--raw` to any chat message to skip Claude formatting and see raw skill output
- Full decision log in `docs/decision-log.md` (13 ADRs)

## Documentation

| Doc | Purpose |
|-----|---------|
| `docs/README.md` | Docs index, architecture overview, project status |
| `docs/local-development.md` | Quick start, dev server, local adapters |
| `docs/aws-infrastructure.md` | Terraform, deployment, DynamoDB, Bedrock |
| `docs/hybrid-routing.md` | Three-tier routing, rule engine, cost comparison |
| `docs/ai-models-pricing.md` | Model options, tiered strategy |
| `docs/dynamodb-schema.md` | Table schemas, key patterns |
| `docs/decision-log.md` | Architecture Decision Records |
| `docs/ROADMAP.md` | Phases, backlog, TODO |