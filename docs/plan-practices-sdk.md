# Plan: t3nets-sdk — Practice Developer SDK

**Project:** T3nets
**Codebase:** `/Users/ellieportugali/projects/t3nets`
**Author:** Ellie Portugali
**Goal:** Give practice developers (private repos) a clean, lightweight way to import t3nets contracts, test locally, and package/distribute practices — without depending on the full platform or its cloud SDKs.

---

## Background

T3nets is an open-source platform. Orgs deploy it and then add **practices** — bundles of skills, pages, and prompts — to customize the agent for their domain (engineering, CBT, etc.). Practices are expected to live in **private repos**, maintained independently of the t3nets platform release cycle.

Today a practice author has no clean developer experience:

- No published Python package to `pip install` — they either vendor types or reach into `agent/models` and `agent/interfaces`, which drags cloud deps along with them.
- Worker contract is untyped: `execute(params: dict, secrets: dict) -> dict`. No autocomplete, no validation, no logger, no storage handles.
- No test doubles — practice authors have to roll their own stubs for `BlobStore`, `EventBus`, etc.
- `practice.yaml` / `skill.yaml` validation only happens at install time inside `agent/practices/registry.py::install_zip()`. Authors find errors late.
- No packaging CLI — they have to know how `install_zip()` expects the ZIP laid out.
- No discovery path — no public or private registry of available practices.

The current practice model already supports filesystem-loaded workers (`agent/practices/registry.py` uses `importlib.util.spec_from_file_location`), and `install_zip()` already accepts a ZIP containing a `practice.yaml`, skills, pages, and hooks. This plan builds on top of that — it does **not** replace the install path.

---

## The answer: carve out `t3nets-sdk`

A separate, pip-installable Python package that is the **only** dependency a private practice repo needs. Zero cloud SDKs, semver-stable, published independently.

### What the SDK contains

- **Contracts** — stable protocols/types practice authors code against:
  - `SkillContext` — exposes `secrets`, `logger`, scoped `BlobStore`, scoped `EventBus`, `tenant_id`, HTTP client with retry
  - `SkillResult` — typed result envelope
  - `Worker` protocol: `async def execute(ctx: SkillContext, params: TParams) -> SkillResult`
  - `InstallHookContext` — what `on_install` hooks receive
- **Models** — read-side dataclasses: `RequestContext`, `Tenant`, `TenantUser`, `TenantSettings`, `InboundMessage`, `OutboundMessage`. **Canonical definitions live in the SDK**; `agent/models/` becomes a thin re-export shim.
- **Interfaces** — abstract ports: `BlobStore`, `SecretsProvider`, `EventBus`, `ConversationStore`. Same rule: canonical in SDK, `agent/interfaces/` re-exports.
- **Manifest validators** — pydantic schemas for `practice.yaml` and `skill.yaml`, so authors get clear errors at dev time instead of install time. Replaces the hand-rolled checks in `install_zip()`.
- **Test doubles** (`t3nets_sdk.testing`) — `MockBlobStore`, `MockSecretsProvider`, `MockEventBus`, `MockConversationStore`, `make_test_context()`. **Naming convention: `Mock*`, not `Fake*`.**
- **CLI** (`t3nets` command):
  - `t3nets practice init` — scaffold a new practice repo
  - `t3nets practice validate` — lint `practice.yaml`, check skill files resolve, check page files exist
  - `t3nets practice package` — produce the `practice.zip` that `install_zip()` already knows how to consume
  - `t3nets practice run-local` — boot the local dev server with the current directory mounted as a practice

### Dependencies

Only: `pyyaml`, `pydantic` (for manifest validation), `httpx` (for the HTTP helper). **No boto3, anthropic, starlette, uvicorn.** The whole point is that a practice repo stays light.

---

## Answered design questions

These came out of the initial design discussion:

| Question | Decision |
|---|---|
| Pydantic vs stdlib dataclasses | **Pydantic for manifest validation** (best author-facing errors). **Stdlib dataclasses for hot-path models** like `RequestContext` (keep Lambda cold starts lean). |
| Distribution channel for the SDK itself | Start with **GitHub install** (`pip install git+https://github.com/outlocks/t3nets-sdk.git@v0.1.0`) during early iteration. Move to PyPI once the surface stabilizes. |
| Marketplace index — in-repo or separate? | **Separate repo** (`t3nets-marketplace` or a branch of `t3nets.dev`). Different release cadence, different trust model. |
| Thin "practice runner" for the Lambda path? | Nice-to-have, **not in v1**. Add once there are real practices being shipped. |
| Test double naming | **`Mock*`**, not `Fake*` (`MockEventBus`, `MockBlobStore`, `MockSecretsProvider`, etc.). |

---

## Proposed monorepo layout

The SDK lives in this repo as its own package so its contract ships in lockstep with the platform, but it's a separate installable:

```
t3nets/
  sdk/                              ← NEW: t3nets-sdk package (own pyproject.toml)
    pyproject.toml
    t3nets_sdk/
      __init__.py
      contracts.py                  # SkillContext, SkillResult, Worker protocol
      models.py                     # canonical dataclasses (moved from agent/models/)
      interfaces.py                 # canonical ABCs (moved from agent/interfaces/)
      manifest.py                   # pydantic validators for practice.yaml / skill.yaml
      http.py                       # retrying httpx wrapper
      testing/
        __init__.py
        mock_blob_store.py
        mock_event_bus.py
        mock_secrets_provider.py
        mock_conversation_store.py
        context.py                  # make_test_context() builder
      cli/
        __init__.py
        main.py                     # `t3nets` entry point
        practice_init.py
        practice_validate.py
        practice_package.py
        practice_run_local.py
    tests/
  agent/
    models/__init__.py              ← re-exports from t3nets_sdk.models
    interfaces/__init__.py          ← re-exports from t3nets_sdk.interfaces
  adapters/
  pyproject.toml                    ← platform; depends on "t3nets-sdk @ file:./sdk" in dev
```

CI publishes `t3nets-sdk` on tag. The platform pins `t3nets-sdk>=X.Y,<X+1` so any accidental contract break gets caught in platform CI before it reaches practice authors.

---

## What a private practice repo looks like

```
acme-engineering-practice/
  pyproject.toml                    # dev dep: t3nets-sdk
  practice.yaml
  skills/
    sprint_status/
      skill.yaml
      worker.py
      schema.py                     # pydantic params model
  pages/
    sprint.html
  assets/
  hooks/
    on_install.py
  tests/
    test_sprint_status.py           # uses MockBlobStore, make_test_context()
  Makefile                          # make dev / make package / make release
  .github/workflows/release.yml     # build + upload practice.zip to GH Release
```

A worker using the new typed contract:

```python
# skills/sprint_status/worker.py
from t3nets_sdk import SkillContext, SkillResult
from .schema import SprintStatusParams

async def execute(ctx: SkillContext, params: SprintStatusParams) -> SkillResult:
    jira = ctx.secrets.get("jira")
    async with ctx.http.client() as http:
        resp = await http.get(f"{jira['url']}/rest/agile/1.0/sprint/{params.sprint_id}")
        resp.raise_for_status()
    ctx.logger.info("fetched sprint", sprint_id=params.sprint_id)
    return SkillResult.ok({"sprint": resp.json()})
```

And a test using the SDK's mocks:

```python
# tests/test_sprint_status.py
from t3nets_sdk.testing import make_test_context, MockSecretsProvider
from skills.sprint_status.worker import execute
from skills.sprint_status.schema import SprintStatusParams

async def test_fetches_sprint():
    ctx = make_test_context(
        secrets=MockSecretsProvider({"jira": {"url": "https://fake.atlassian.net", "token": "x"}}),
    )
    result = await execute(ctx, SprintStatusParams(sprint_id=42))
    assert result.ok
```

---

## Stable worker contract — migration

Today: `execute(params: dict, secrets: dict) -> dict` (see `agent/skills/ping/worker.py`).
Target: `async def execute(ctx: SkillContext, params: TParams) -> SkillResult`.

`agent/skills/registry.py::get_worker()` resolves workers dynamically. To introduce the new calling convention **without breaking existing skills**:

1. When the registry loads a worker, inspect its signature.
2. If it takes `(ctx, params)` → call directly with a `SkillContext` built from the runtime environment.
3. If it takes `(params, secrets)` (legacy) → wrap it in a thin adapter that unpacks `ctx.secrets` and forwards.
4. Existing built-in skills (`ping`, `release_notes`, `sprint_status`) continue to work unchanged. Migrate them to the new contract opportunistically, not as a prerequisite.

---

## Discovery / distribution

Two tiers, both reusing `install_zip()`:

1. **Public marketplace** — JSON index file hosted separately (e.g. `t3nets.dev/practices.json`). Each entry:
   ```json
   {
     "name": "engineering",
     "display_name": "Engineering",
     "description": "...",
     "repo_url": "https://github.com/outlocks/t3nets-practice-engineering",
     "latest_version": "1.2.0",
     "artifact_url": "https://github.com/.../releases/download/v1.2.0/practice.zip",
     "sha256": "abc...",
     "sdk_version": "^1.0"
   }
   ```
   The dashboard gets a "Marketplace" tab that fetches this index, shows cards, and on install downloads the artifact, verifies the sha256, and calls the existing `install_zip()` flow.

2. **Private practices** — orgs install by direct URL (GitHub Release asset, S3 presigned URL) or by uploading a zip in the dashboard. Same `install_zip()` path.

### Version compatibility

`practice.yaml` gains an `sdk_version` field. The platform refuses installs whose `sdk_version` is outside the range the running platform supports, with a clear error message. This is the knob that lets the SDK evolve without silently breaking old practices.

---

## Staged rollout

Smallest-useful-chunks first. Each step ships independently and leaves the system working.

### Step 1 — Stand up `sdk/` as a package (load-bearing refactor)
- Create `sdk/` with `pyproject.toml` and empty `t3nets_sdk/` package.
- Move `agent/models/*` → `t3nets_sdk/models.py`.
- Move `agent/interfaces/*` → `t3nets_sdk/interfaces.py`.
- Replace `agent/models/__init__.py` and `agent/interfaces/__init__.py` with re-export shims (`from t3nets_sdk.models import *`).
- Add `"t3nets-sdk @ file:./sdk"` as a dev dep in the platform `pyproject.toml`.
- Run full test suite — **zero behavior change expected**.

### Step 2 — `t3nets_sdk.testing` with Mock doubles
- `MockBlobStore`, `MockSecretsProvider`, `MockEventBus`, `MockConversationStore` — all in-memory.
- `make_test_context()` builder.
- Unit tests for the mocks themselves.
- Unlocks local practice dev immediately, even before the new worker contract lands.

### Step 3 — `t3nets_sdk.manifest` pydantic validators
- Pydantic models for `practice.yaml` and `skill.yaml`.
- Swap `agent/practices/registry.py::install_zip()` to use them for validation, delete the hand-rolled checks.
- Clearer install errors as a free side effect.

### Step 4 — `t3nets` CLI: `init`, `validate`, `package`
- `init` — scaffolds a practice from a template directory bundled in the SDK.
- `validate` — runs the same pydantic checks `install_zip()` uses, plus filesystem checks (skill files present, page files present).
- `package` — zips the current directory into a `practice.zip` laid out the way `install_zip()` expects.
- `run-local` deferred — needs platform coupling, easier to add after steps 5–6.

### Step 5 — New `SkillContext` / `SkillResult` worker contract
- Add the types to `t3nets_sdk.contracts`.
- Update `agent/skills/registry.py::get_worker()` to detect signature and route accordingly.
- Add an adapter for legacy `(params, secrets) -> dict` workers.
- Migrate one built-in skill (`ping`) as a reference implementation.

### Step 6 — Publish SDK to GitHub, then PyPI
- Tag `sdk/v0.1.0`, set up GitHub Actions to build + publish.
- Pin platform to `t3nets-sdk>=0.1,<0.2`.
- Write `docs/practice-development.md` — the public-facing guide for practice authors.

### Step 7 — `t3nets practice run-local`
- Boot `adapters/local/dev_server.py` with the current directory mounted as an extra practices source.
- Needs a small dev-server flag: `--extra-practice-dir=<path>`.

### Step 8 — Marketplace index + dashboard tab
- Create the index file repo.
- Add a "Marketplace" tab to the dashboard that fetches and renders it.
- Install flow: download → verify sha256 → `install_zip()`.
- Deliberately last: it's the least load-bearing and the hardest to get right without real practices to list.

---

## Out of scope (for this plan)

- Rewriting existing built-in skills to the new contract (opportunistic migration only).
- A formal private registry server — start with GitHub Releases + direct URL install.
- A signing/provenance system for practice artifacts (sha256 is the v1 integrity check).
- Multi-language practices (only Python supported, matching current state).
- Lambda-side practice runner helper — revisit once real practices ship.

---

## Open risks / watch items

- **Hot-path models in pydantic would slow Lambda cold starts.** Mitigation: keep `RequestContext`, `Tenant`, etc. as stdlib dataclasses; pydantic is only for manifest parsing at install time.
- **Re-export shims in `agent/models/__init__.py` and `agent/interfaces/__init__.py` must preserve `__all__` and any type re-exports** so existing imports keep working. Verify with mypy --strict after step 1.
- **Lambda bundles must include the SDK.** Practice skill Lambdas deployed via `PracticeRegistry.deploy_skill_lambdas()` need the SDK bundled into the zip. Step 5 needs to address this or the Lambda path will break.
- **The JSON marketplace index is uncurated by default.** Plan a lightweight review process before advertising the Marketplace tab publicly.
