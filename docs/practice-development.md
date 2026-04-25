# Practice Development

This guide is for **practice authors** — you want to bundle skills and pages
into a t3nets practice that lives in its own repo and ships to any t3nets
deployment as a single ZIP.

A practice is the unit of customization in t3nets:

- **Skills** are individual capabilities the agent can call (look up a sprint,
  generate release notes, publish to a destination).
- **Pages** are dashboard tabs your tenant gets when they install the practice
  (a sprint board, a runbook viewer).
- **Integrations** declare the secrets your skills need (`jira`, `github`).

The platform stays general; the practice is the team's flavor.

## Quickstart

```bash
pip install t3nets-sdk
t3nets practice init my-practice
cd my-practice
t3nets practice validate
pytest tests/
```

If the test passes, your scaffold is ready to extend. The four bundled
sub-commands are:

| Command | What it does |
|---|---|
| `t3nets practice init NAME` | Scaffold a new practice repo with one example skill, one passing test, and a working manifest. |
| `t3nets practice validate` | Run the same checks the platform runs at install time — pydantic schema, file existence, name patterns. |
| `t3nets practice package` | Validate, then build `dist/practice.zip` (manifest at the archive root, dev files excluded). |
| `t3nets practice run-local` | Boot the platform's local dev server with this practice loaded. Requires the t3nets platform installed in the same venv. |

## What `init` gives you

```
my-practice/
├── practice.yaml           # bundle manifest
├── skills/
│   └── example/
│       ├── skill.yaml      # this skill's manifest
│       ├── worker.py       # the actual code
│       └── __init__.py
├── tests/
│   └── test_example.py     # exercises worker through the typed contract
└── README.md
```

Everything in this layout is the minimum the platform expects. You can add
more skills, pages, hooks, integrations — but the practice is valid the
moment `init` finishes.

## Writing a skill

A **skill** is a `skill.yaml` describing when to call it and what parameters
it takes, plus a `worker.py` that implements it. The worker contract is the
only thing you really need to internalize:

```python
from t3nets_sdk.contracts import SkillContext, SkillResult


async def execute(ctx: SkillContext, params: dict) -> SkillResult:
    return SkillResult.ok({"echo": params["message"]})
```

### `SkillContext`

A typed bundle of runtime handles the platform passes to every invocation:

| Field | Type | Notes |
|---|---|---|
| `tenant_id` | `str` | The tenant this invocation belongs to. Pass it to any tenant-scoped store. |
| `secrets` | `dict[str, Any]` | Resolved secret bundle for the skill's `requires_integration` (e.g. `{"url": "...", "token": "..."}`). Empty dict if the skill declared no integration. |
| `logger` | `logging.Logger` | A stdlib logger scoped to the skill. Use this — don't roll your own. |
| `blob_store` | `BlobStore \| None` | For persisting artifacts (audio, screenshots, exported files). `None` when the host hasn't wired one up (some unit tests, simple deployments). |
| `raw` | `bool` | The user appended `--raw` to their request. If your worker renders its own user-facing text, skip the rendering and let the router JSON-dump the data. |
| `extras` | `dict[str, Any]` | Freeform host-specific extensions. Don't rely on specific keys — anything portable should live in `secrets` / `blob_store` / `tenant_id`. |

### `SkillResult`

The envelope you return. Construct via classmethods rather than the raw
`__init__`:

```python
# Happy path — structured data only
return SkillResult.ok({"sprint": sprint, "issues": issues})

# Or with kwargs
return SkillResult.ok(sprint=sprint, issues=issues)

# With your own user-facing text — skip the router's AI formatter
return SkillResult.ok(
    {"sprint": sprint},
    text=f"Sprint {sprint['name']} is on track — {pct}% done.",
)

# Or steer the router's AI formatter without invoking it yourself
return SkillResult.ok(
    {"sprint": sprint},
    render_prompt="Format this as a standup-friendly sprint summary, lead with the end date, then blockers.",
)

# Failure
return SkillResult.fail("Jira returned 401 — check your token.")
```

The rendering rules are deliberate:

- `text` set → router sends it verbatim. No Bedrock round-trip. Honor `ctx.raw`
  and skip rendering when it's true.
- `text` None, `render_prompt` set → router calls its AI formatter with your
  prompt + the structured `data`. Use this when you want to steer formatting
  without pulling Bedrock into the skill itself.
- Both None → router falls back to a generic "format this clearly" prompt.
  Fine for prototypes; not great for polished output.

### `skill.yaml`

```yaml
name: sprint_status
description: >
  Reports on the current sprint — progress, blockers, large unstarted tickets.
  Triggered by "sprint status", "how's the sprint", "blockers", etc.

triggers:
  - "sprint status"
  - "how's the sprint"
  - "blockers"

requires_integration: jira  # secrets bundle is fetched from this integration
supports_raw: true          # the worker honors ctx.raw

action_descriptions:
  status: "Show sprint progress and blockers"
  blockers: "Show only blocked tickets"

parameters:
  type: object
  properties:
    action:
      type: string
      enum: [status, blockers]
      default: status
  required: []
```

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Lowercase letters, digits, dashes, underscores. Must start alphanumerically. |
| `description` | yes | Used by the AI router to decide when to call the skill. Be specific. |
| `triggers` | no | Phrases that should route to this skill before the AI router gets involved. |
| `requires_integration` | no | Name of the integration whose secrets the platform should resolve and pass via `ctx.secrets`. Skills without an integration receive an empty dict. |
| `supports_raw` | no | Set to `true` if your worker checks `ctx.raw` and renders accordingly. |
| `action_descriptions` | no | Documentation for individual action values, surfaced in admin UIs. |
| `parameters` | no | JSON Schema for the params dict. The AI router uses it to extract parameters from natural-language requests. |

## Writing the practice manifest

```yaml
name: dev-jira
display_name: "Dev — Jira"
description: "Sprint management and release tracking for engineering teams."
version: "1.0.0"
icon: "gear"

integrations:
  - jira

skills:
  - sprint_status
  - release_notes

pages:
  - slug: sprint
    title: "Sprint Board"
    file: pages/sprint.html
    nav_label: "Sprint"
    nav_order: 10
```

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Same name pattern as skills. |
| `display_name` | no | Shown in dashboard tabs. Defaults to `name`. |
| `description` | no | Free-form description. |
| `version` | no | Semver-ish. Used for upload conflict detection — uploading the same name+version is a no-op. |
| `icon` | no | Icon hint for the dashboard. |
| `integrations` | no | List of integration names the practice's skills need. |
| `skills` | yes-ish | Names of skills bundled in `skills/<name>/`. |
| `pages` | no | Static HTML pages the practice ships. The platform serves them at `/p/<practice>/<slug>`. |
| `assets` | no | Files referenced by your pages (CSS, JS, images). Bundled in the upload but not directly routed. |
| `hooks` | no | `{"on_install": "hooks/on_install.py"}` — runs once when the practice installs. |
| `system_prompt_addon` | no | Text appended to the AI router's system prompt when this practice is active. Use this to give the router domain-specific tone or instructions. |

## Pages (briefly)

Pages are dashboard tabs your practice ships. Each entry needs at minimum:

```yaml
pages:
  - slug: sprint
    title: "Sprint Board"
    file: pages/sprint.html
```

The platform serves them at `https://<your-host>/p/<practice-name>/<slug>`.
Pages can talk to skills via the platform's async dispatch endpoint — see the
existing [`agent/practices/dev-jira/`](../agent/practices/dev-jira/) practice
in the t3nets monorepo for a concrete example.

## Testing

The scaffolded test exercises the worker through its real contract:

```python
import asyncio
import importlib.util
from pathlib import Path

from t3nets_sdk.contracts import SkillContext, SkillResult


def _load_worker():
    path = Path(__file__).resolve().parent.parent / "skills" / "example" / "worker.py"
    spec = importlib.util.spec_from_file_location("example_worker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_echoes_message() -> None:
    worker = _load_worker()
    ctx = SkillContext(tenant_id="test-tenant")
    result: SkillResult = asyncio.run(worker.execute(ctx, {"message": "hello"}))
    assert result.success
    assert result.data == {"echo": "hello"}
```

The platform itself loads workers by file path, so this is the same code
path your worker hits in production.

For tests that need to fake out the platform's runtime stores, the SDK ships
in-memory `Mock*` doubles:

```python
from t3nets_sdk.testing import (
    MockBlobStore,
    MockSecretsProvider,
    MockEventBus,
    MockConversationStore,
    make_test_context,
)

def test_skill_with_secrets():
    ctx = SkillContext(
        tenant_id="acme",
        secrets={"url": "https://acme.atlassian.net", "token": "fake-token"},
        blob_store=MockBlobStore(),
    )
    # ...
```

`MockSecretsProvider`, `MockBlobStore`, `MockEventBus`, and
`MockConversationStore` mirror the interfaces a skill might be handed in a
fuller integration test. Use the `Mock*` naming convention — never `Fake*`.

## Local development loop

`t3nets practice run-local` boots the platform's local dev server with your
practice mounted as an extra source:

```bash
# In your practice repo, with the platform installed in the same venv
t3nets practice run-local
```

This requires `pip install -e <path-to-t3nets-monorepo>` (or `pip install
t3nets`, once that ships separately). The dev server validates your practice
first, then mounts it next to the built-in practices so you can iterate
against the real router.

If you only want to validate your manifest without booting a server,
`t3nets practice validate` runs the same checks `install_zip()` runs at
install time and exits non-zero on failure.

## Packaging and uploading

```bash
t3nets practice package
# Packaged 6 files -> dist/practice.zip
```

The package step **validates first**, then zips with `practice.yaml` at the
archive root. Dev-only files are excluded:
`tests/`, `.git/`, `.venv/`, `node_modules/`, `__pycache__/`, `.pytest_cache/`,
`.mypy_cache/`, `.ruff_cache/`, `dist/`, `build/`, `.github/`, plus `.pyc` /
`.DS_Store`.

Upload the resulting `practice.zip` through the platform's settings page. On
upload, the platform:

1. Validates the manifest against the same pydantic schema you ran locally.
2. Stores the ZIP (S3 on AWS, the data directory locally).
3. Loads each declared skill into the runtime registry.
4. Publishes any pages to the static origin (CloudFront on AWS).
5. Runs `hooks.on_install` if declared.

Re-uploading the same `name` and `version` is a no-op. To roll out an
update, bump the practice's `version` field in `practice.yaml`.

## Versioning

The SDK follows strict semver. Pin a major:

```toml
# In your practice repo's pyproject.toml or requirements.txt
t3nets-sdk>=0.1,<0.2
```

When the SDK ships a major bump, the platform updates its own pin in lockstep.
Practices that pin a major will keep working until you choose to upgrade.

## Where to go next

- The [t3nets-sdk source](../sdk/t3nets_sdk/) — every module has a docstring
  describing its purpose. Start with `contracts.py` and `manifest.py`.
- The [`dev-jira` built-in practice](../agent/practices/dev-jira/) — a real
  practice that ships sprint status and release notes skills against Jira.
  Same shape `init` produces, just with real workers.
- [`docs/decision-log.md`](decision-log.md) — the architecture decisions
  behind the SDK / practices split.

If you hit something the SDK doesn't expose, open an issue at
[github.com/WildEllie/t3nets/issues](https://github.com/WildEllie/t3nets/issues)
— the public surface is intentionally minimal, and the bar for adding to it
is "a real practice needs it".
