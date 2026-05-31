# Interim Roadmap: Sherpii ↔ t3nets Alignment

**Status:** Active, time-bound — folds back into `docs/ROADMAP.md` once Phase 2.1 lands.
**Scope:** Sherpii's adoption of t3nets as a platform, plus the platform changes Sherpii needs in return.
**Last updated:** 2026-05-31

---

## Phase 1 — Sherpii repo alignment (short-term compatibility)

Goal: conform the Sherpii practice repo to the **current** t3nets-sdk practice contract so it deploys and tests through standard t3nets tooling. Treat this as bridge work — most of it becomes optional once Phase 2.1 ships a language-neutral contract.

Owner: **Sherpii**. The t3nets repo is unaffected by these items.

| # | Item | Repo |
|---|---|---|
| 1.1 | Fix `practice.yaml` schema drift — rename `page_type` → `type`; validate with `t3nets practice validate` | sherpii |
| 1.2 | Add repo metadata + test config — `pyproject.toml` with `t3nets-sdk>=0.1,<0.2`, pytest/ruff/mypy aligned with t3nets conventions | sherpii |
| 1.3 | Replace local test doubles with `t3nets_sdk.testing.Mock*` (SecretsProvider, BlobStore, EventBus, ConversationStore) | sherpii |
| 1.4 | Replace the custom ZIP script with `t3nets practice package` | sherpii |
| 1.5 | Document the dev loop — `t3nets practice validate` → `pytest` → `t3nets practice run-local` | sherpii |

**Exit criteria:** Sherpii's practice ZIP installs into a t3nets tenant via the standard upload flow, all tests pass under `pytest`, and the dev README walks a new contributor through validate→test→run-local without referencing any custom Sherpii tooling.

---

## Phase 2 — t3nets platform capability requests

Goal: pull recurring needs that Sherpii keeps hitting into t3nets as platform features so every consuming practice gets them for free. Each item is a cross-workstream issue, **not** hidden inside a Sherpii implementation task.

Owner: **t3nets**. Sherpii is the first consumer; APIs must be language-neutral and tenant-generic.

| # | Item | Depends on | Notes |
|---|---|---|---|
| 2.1 | **Language-neutral practice contract** — Protocol Buffers / gRPC for skill invocation, result envelopes, packaging metadata, conformance tests. SDK becomes a convenience layer over the wire contract. | — (foundational) | **Transport decision: gRPC.** Most items below assume this exists. |
| 2.2 | **Worker-side model interface** — portable way for workers to request structured LLM analysis without importing provider SDKs (Anthropic / OpenAI / Bedrock). | 2.1 | Extends the contract; same shape across languages. |
| 2.3 | **Page helper SDK** — browser-side helpers for auth headers, `invokeSkill`, toasts/errors, practice nav injection, callback/result handling. | — | Decouples practice pages from re-implementing identical dashboard glue. Can ship inside current SDK. |
| 2.4 | **Practice-level integration secrets** — credentials/config shared by all skills in a practice instead of duplicated per skill. | — | Schema change in `practice.yaml` + tenant secrets layout. Sherpii's voice/clinical creds are the driver. |
| 2.5 | **Blob metadata/listing API** — richer list/query support for tenant-scoped blobs so practices avoid ad-hoc index files. | — | Add to `BlobStore` interface; both `FileStore` (local) and `S3BlobStore` (AWS) implementations. |
| 2.6 | **Practice CLI scaffolds** — `t3nets practice add-skill`, `t3nets practice add-page`, complementing `practice init`. | — | Small, ships independently. Useful for any practice author. |
| 2.7 | **Audit/event API** — stable way to emit events. **Taxonomy is practice-declared**, not a t3nets-side enum — t3nets owns only the transport and storage. Practices register their event types via `practice.yaml`. | EventBus surface clarified | Avoids healthcare-specific events leaking into the platform; preserves flexibility across domains. |
| 2.8 | **Data lifecycle API** — practice-scoped export/delete flows including blob cleanup + audit markers. | 2.5, 2.7 | Closes the GDPR / data-retention loop. |

---

## Locked-in design decisions

| # | Decision | Why |
|---|---|---|
| 1 | **gRPC** for the worker contract transport (2.1) | Strong typing across languages, streaming support for long-running synthesis, generated client/server stubs in every target language. |
| 2 | **Practice-declared** audit event taxonomy (2.7) | Platform stays domain-agnostic. Healthcare-specific event types belong in Sherpii; t3nets ships transport + storage + query surface only. |

---

## Cross-cutting notes

- **Parallelizable**: Phase 1 (Sherpii alignment) and Phase 2.1 (contract design) can run concurrently in different repos.
- **Critical path**: 2.1 → 2.2 → (2.7 → 2.8). Other items (2.3, 2.4, 2.5, 2.6) are low-coupling and can ship inside the current SDK without waiting for the contract redesign — flag as "quick wins."
- **Multi-language runtime infrastructure**: today's `scripts/build_lambda_base.sh` is Python-only. Real multi-language support (Phase 2.1 endgame) means container Lambdas or per-language base images — meaningful infra work that should be scoped explicitly under 2.1.

---

## Open questions

1. **gRPC transport details** — bidirectional streaming for synthesis, server-streaming for events, or unary for everything? Worth a quick spike before locking the `.proto`.
2. **Conformance test framework** — how do non-Python practice authors run the same conformance suite? Containerized test runner that speaks gRPC? Lives under 2.1.
3. **Practice repo locations** — where does the Sherpii repo live? Phase 1 items can't be assigned to a worktree without it.
