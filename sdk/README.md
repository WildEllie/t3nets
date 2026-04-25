# t3nets-sdk

Public SDK for building [t3nets](https://github.com/WildEllie/t3nets) practices.

A practice is a bundle of skills, pages, and prompts that customizes a t3nets
deployment for a specific domain (engineering, therapy, ops, etc.). Practices
typically live in their own repos — this SDK is the only t3nets dependency
they need.

## What's in here

- **Models** — `RequestContext`, `Tenant`, `TenantUser`, `TenantSettings`,
  `InboundMessage`, `OutboundMessage`, channel types.
- **Interfaces** — `BlobStore`, `SecretsProvider`, `EventBus`,
  `ConversationStore`. Abstract ports a skill worker may receive at runtime.

Future additions (see `docs/plan-practices-sdk.md` in the platform repo):

- `t3nets_sdk.contracts` — typed `SkillContext` / `SkillResult` worker contract
- `t3nets_sdk.manifest` — pydantic validators for `practice.yaml` / `skill.yaml`
- `t3nets_sdk.testing` — in-memory `Mock*` doubles for local skill tests
- `t3nets` CLI — `init`, `validate`, `package`, `run-local`

## Design rules

- **Zero cloud SDKs.** No `boto3`, no `anthropic`, no `starlette`. The SDK must
  stay light enough to drop into any practice repo.
- **Stable surface.** SemVer strict. Practices pin a major.
- **Stdlib dataclasses on the hot path.** Pydantic only for manifest parsing
  (when added) — keeps Lambda cold starts lean.

## Installation

```bash
pip install t3nets-sdk
```

For local development against the t3nets monorepo:

```bash
pip install -e ./sdk
```
