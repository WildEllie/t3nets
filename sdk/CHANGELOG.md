# Changelog

All notable changes to `t3nets-sdk` are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-04-25

Initial public release. Practice authors can now build practices in a
separate repo, depending on `t3nets-sdk` as their only t3nets-side
dependency.

### Added

- `t3nets_sdk.models` — `RequestContext`, `Tenant`, `TenantUser`,
  `TenantSettings`, `InboundMessage`, `OutboundMessage`, channel types.
  Stdlib dataclasses, kept off the request hot path.
- `t3nets_sdk.interfaces` — abstract ports a skill worker may receive
  at runtime: `BlobStore`, `SecretsProvider`, `EventBus`,
  `ConversationStore`.
- `t3nets_sdk.contracts` — typed `SkillContext` / `SkillResult` worker
  contract. Workers receive a `SkillContext` and return a `SkillResult`
  carrying either `text` (verbatim) or `render_prompt` (router AI
  formatter); reserved transport keys survive SQS/Lambda boundaries.
- `t3nets_sdk.manifest` — pydantic validators for `practice.yaml` and
  `skill.yaml`, surfaced through the CLI.
- `t3nets_sdk.testing` — in-memory `MockSecretsProvider`,
  `MockBlobStore`, `MockEventBus`, `MockConversationStore` for offline
  skill tests.
- `t3nets` CLI — `practice init`, `practice validate`,
  `practice package`, `practice run-local` (boots the local dev server
  with the current directory mounted as an extra practices source).

### Notes

- Zero cloud SDKs in dependencies. Practice repos depend on
  `t3nets-sdk` and nothing else from the platform.
- `pydantic` is used only for manifest parsing — never on the request
  hot path.
- API surface is considered stable starting with this release. Breaking
  changes will require a major version bump.
