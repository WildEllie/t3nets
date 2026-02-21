# T3nets Documentation

**Last Updated:** February 21, 2026

Welcome to the T3nets project documentation. These docs capture accumulated architecture knowledge, deployment guides, and decision rationale.

## Quick Links

| I want to... | Read this |
|--------------|-----------|
| Run the project locally | [Local Development Guide](local-development.md) |
| Deploy to AWS | [AWS Infrastructure Reference](aws-infrastructure.md) |
| Understand how routing works | [Hybrid Routing Architecture](hybrid-routing.md) |
| Choose an AI model | [AI Models & Pricing Guide](ai-models-pricing.md) |
| Understand the DynamoDB schema | [DynamoDB Schema Reference](dynamodb-schema.md) |
| See why we made a decision | [Architecture Decision Log](decision-log.md) |
| See what's next | [Roadmap & TODO](ROADMAP.md) |

## Project Status

**Current phase:** 1b (Deploy to AWS + Settings page)

**What's working:**
- Local dev server with chat UI and health dashboard
- Hybrid routing (conversational → rule-matched → AI fallback)
- Sprint status skill (Jira integration)
- `--raw` debug mode
- AWS Terraform modules (ready to apply)
- AWS adapters (Bedrock, DynamoDB, Secrets Manager)

**What's next:**
- `terraform apply` to deploy AWS infrastructure
- Settings page in dashboard (model selection, server restart)
- Multi-tenancy (Phase 2)

## Architecture Overview

```
                    ┌─────────────────┐
                    │   Chat UI       │
                    │   Health UI     │
                    │   Settings UI   │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │   HTTP Server   │
                    │   (dev_server   │
                    │    or Fargate)  │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  Hybrid Router  │
                    │                 │
                    │  Tier 1: Canned │──▶ Instant response ($0)
                    │  Tier 2: Rules  │──▶ Skill → Claude format ($0.01)
                    │  Tier 3: Claude │──▶ Claude picks tool ($0.02+)
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌──────────┐  ┌──────────┐  ┌──────────┐
        │ Anthropic │  │ Bedrock  │  │  Skills  │
        │   API     │  │  API     │  │  (Jira)  │
        │  (local)  │  │  (AWS)   │  │          │
        └──────────┘  └──────────┘  └──────────┘
```

## Repository Structure

```
t3nets/
├── agent/              # Portable business logic (no cloud imports)
│   ├── router/         # Hybrid routing (rule_router.py) + Router
│   ├── skills/         # Skill definitions + workers
│   ├── channels/       # Channel adapters (dashboard, future: Teams/Slack)
│   ├── memory/         # Conversation history management
│   ├── interfaces/     # Abstract base classes
│   └── models/         # Shared dataclasses
├── adapters/
│   ├── local/          # Local dev (Anthropic, SQLite, .env, chat/health UI)
│   └── aws/            # AWS (Bedrock, DynamoDB, Secrets Manager)
├── infra/aws/          # Terraform modules
├── scripts/            # Deploy, seed scripts
├── docs/               # ← You are here
└── Dockerfile
```
