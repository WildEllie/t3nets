# t3nets-sdk

Public SDK for building [t3nets](https://github.com/WildEllie/t3nets) practices.

A practice is a bundle of skills, pages, and prompts that customizes a t3nets
deployment for a specific domain (engineering, therapy, ops, etc.). Practices
typically live in their own repos — this SDK is the only t3nets dependency
they need.

## What's in here

- **Models** (`t3nets_sdk.models`) — `RequestContext`, `Tenant`, `TenantUser`,
  `TenantSettings`, `InboundMessage`, `OutboundMessage`, channel types.
- **Interfaces** (`t3nets_sdk.interfaces`) — `BlobStore`, `SecretsProvider`,
  `EventBus`, `ConversationStore`. Abstract ports a skill worker may receive
  at runtime.
- **Contracts** (`t3nets_sdk.contracts`) — typed `SkillContext` /
  `SkillResult` worker contract. Workers receive a `SkillContext` and return
  a `SkillResult` carrying either `text` (verbatim) or `render_prompt`
  (router AI formatter).
- **Manifest validators** (`t3nets_sdk.manifest`) — pydantic validators for
  `practice.yaml` / `skill.yaml`.
- **Test doubles** (`t3nets_sdk.testing`) — in-memory `Mock*` implementations
  of every interface, for offline skill tests.
- **CLI** (`t3nets`) — `practice init`, `practice validate`,
  `practice package`, `practice run-local`.

## Design rules

- **Zero cloud SDKs.** No `boto3`, no `anthropic`, no `starlette`. The SDK must
  stay light enough to drop into any practice repo.
- **Stable surface.** SemVer strict. Practices pin a major.
- **Stdlib dataclasses on the hot path.** Pydantic only for manifest parsing —
  keeps Lambda cold starts lean.

## Installation

```bash
pip install t3nets-sdk
```

For local development against the t3nets monorepo:

```bash
pip install -e ./sdk
```
