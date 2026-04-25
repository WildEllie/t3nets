# Handoff: Phase 6c + 6d Close — practices live on AWS, SDK on PyPI

**Date:** 2026-04-26
**Status:** Complete
**Roadmap items:** Phase 6c (AWS Deployment), Phase 6d (Practices SDK), plus a Phase 5d regression fix
**Commits:** `12eb030 → 8e7d8e2` (7 commits on `main`)
**PyPI artifact:** [`t3nets-sdk 0.1.1`](https://pypi.org/project/t3nets-sdk/) — first public release

---

## What Was Done

Closed the two outstanding practices-related phases end-to-end on `dev`:

1. **Phase 6c** — `terraform apply` + smoke test of the CDN+S3 practice page deploy that landed in `cd6388a`. ECS task definition picked up the new `CLOUDFRONT_DISTRIBUTION_ID` env var, IAM gained `cloudfront:CreateInvalidation`, CloudFront `/p/*` cache behavior went live. Smoke-tested the route by uploading a throwaway HTML and confirming the rewrite_html function resolved both `/p/_smoke6c/hello` and `/p/_smoke6c/hello.html` to 200s.
2. **Phase 5d regression fix** — first AWS deploy after the Phase 5d split surfaced an `ImportModuleError` in all three skill Lambdas: `cannot import name 'assets' from 'agent.practices'`. Root cause: when `agent/practices/registry.py` was split into `registry/installer/deployer/assets`, the Lambda packaging scripts kept copying only `registry.py`. Fixed `scripts/build_lambda_base.sh` and `scripts/deploy.sh` to glob all top-level `*.py` and added the new modules to the terraform trigger list.
3. **Outlocks scrub** — Ellie pushed back on stale `Outlocks` references that a prior author had seeded throughout the codebase (author fields in pyproject.toml, hypothetical `github.com/outlocks/*` URLs in docs and SDK README, `"outlocks"` as a placeholder tenant_id in `dynamodb-schema.md` and `tenant_store.py`). Replaced all 13 references — author metadata now `Ellie Portugali <ellieportugali@gmail.com>`, repo URLs point to `WildEllie/t3nets`, placeholder tenant became `acme`/`Acme`. The README bio mention (`...access control technology at [Outlocks](https://outlocks.com)`) is a separate company Ellie actually worked with and stays as-is.
4. **Phase 6d (PyPI publish + platform pin + public guide)** — published `t3nets-sdk 0.1.0` and `0.1.1` to PyPI via OIDC trusted publishing, pinned the platform to `t3nets-sdk>=0.1,<0.2`, fixed an init-scaffold contract bug, and shipped `docs/practice-development.md` for external practice authors. End-to-end milestone (`pip install t3nets-sdk` → `t3nets practice init` → passing local test run) verified live.

---

## Files Created

| File | Purpose |
|------|---------|
| `.github/workflows/publish-sdk.yml` | OIDC trusted-publishing flow triggered by `sdk/v*` tags. Three jobs (test → build+twine-check → publish) gated on the `pypi` GitHub Environment for manual approval. |
| `sdk/CHANGELOG.md` | Keep-a-Changelog format; entries for 0.1.0 (initial release) and 0.1.1 (init scaffold contract fix). |
| `docs/practice-development.md` | External-author guide covering `pip install`, `t3nets practice init` walk-through, `SkillContext`/`SkillResult` contract, `practice.yaml` + `skill.yaml` reference, testing patterns with the SDK Mock doubles, run-local loop, packaging + uploading. |

## Files Modified

| File | Change |
|------|--------|
| `pyproject.toml` | Added `t3nets-sdk>=0.1,<0.2` dependency. Removed the editable-install comment. Reworded the `mypy_path = "sdk"` comment for the editable-override dev workflow. Author metadata now Ellie. |
| `sdk/pyproject.toml` | Added `[project.urls]` (Homepage, Repository, Issues, Changelog) — must sit *after* `dependencies =` because TOML otherwise scopes subsequent keys under `[project.urls]`. Bumped version `0.1.0 → 0.1.1`. Author metadata now Ellie. |
| `sdk/README.md` | Dropped stale "Future additions" list (those modules already shipped). Documents the v0.1.1 public surface: models, interfaces, contracts, manifest, testing, CLI. |
| `sdk/t3nets_sdk/cli/init.py` | Scaffolded worker now uses the typed v0.1.0 `Worker` contract (`async def execute(ctx: SkillContext, params: dict[str, Any]) -> SkillResult`). The 0.1.0 scaffold shipped the legacy `(params, secrets) -> dict` shape, which raised `TypeError` on first invocation by the platform. The scaffolded test exercises the worker through its real contract using `importlib.util` (no pytest config needed). |
| `tests/test_sdk_cli.py` | Added `TestInit::test_scaffolded_worker_matches_typed_contract` so future template regressions surface here. |
| `Dockerfile` | Removed `COPY sdk/` + `pip install ./sdk`. Pulls `t3nets-sdk` from PyPI alongside the other platform deps. |
| `scripts/build_lambda_base.sh` | (a) Glob every `agent/practices/*.py` instead of copying only `registry.py` (Phase 5d fix). (b) Replace `pip install ${PROJECT_ROOT}/sdk` with `pip install "t3nets-sdk>=0.1,<0.2"`, kept `--no-deps` so the manylinux pyyaml/pydantic from the previous step survive. |
| `scripts/deploy.sh` | Same two changes as `build_lambda_base.sh`, applied to the per-skill Lambda packaging loop. |
| `infra/aws/modules/compute/lambda.tf` | Added `installer.py`, `deployer.py`, `assets.py` to `lambda_source_files` so future edits to those modules trigger a Lambda rebuild. |
| `agent/interfaces/tenant_store.py` | Docstring example tenant_id `"outlocks" → "acme"`. |
| `docs/dynamodb-schema.md` | Replaced six `TENANT#outlocks` examples and one `"Outlocks"` display name with `acme`/`Acme`. |
| `docs/plan-practices-sdk.md` | Hypothetical `github.com/outlocks/*` URLs replaced with `WildEllie/t3nets` (with `#subdirectory=sdk` for the GitHub install variant) and `acme/...` for the marketplace example. |
| `CLAUDE.md` | Install instructions document the editable-SDK override for monorepo dev. Docs table picks up `practice-development.md`. |
| `README.md` | Install line annotated `# pulls t3nets-sdk from PyPI`. Docs table picks up `practice-development.md`. |
| `docs/README.md` | Quick-links table picks up `practice-development.md`. |
| `docs/ROADMAP.md` | Phase 6c milestone checked off (12eb030); Phase 6d's three remaining boxes (PyPI publish, platform pin, public guide) and the milestone itself checked off (8e7d8e2). |

---

## Commit Sequence

| Commit | Subject |
|---|---|
| `12eb030` | docs: close Phase 6c milestone — practices live on AWS |
| `609aec2` | fix: include all agent/practices/*.py in Lambda packaging |
| `ede15c5` | chore: remove Outlocks references from project |
| `6dcd18d` | feat: prep t3nets-sdk for PyPI publish (Phase 6d Step 6) |
| `e997db8` | feat: pin platform to t3nets-sdk from PyPI (Phase 6d Step 6) |
| `e3ab9fd` | fix: t3nets practice init scaffolds the typed worker contract (sdk 0.1.1) |
| `8e7d8e2` | docs: practice development guide + close Phase 6d milestone |

Plus tags: `sdk/v0.1.0`, `sdk/v0.1.1`.

---

## Operational Footprint

- **AWS dev environment** redeployed twice during the session:
  1. After `12eb030`/`609aec2` to pick up Phase 6c CDN+S3 + the Phase 5d Lambda packaging fix.
  2. After `e997db8` to validate the PyPI-sourced SDK install path through the real Docker + Lambda packaging.
  ECS rev settled at `28`. Smoke tests post-deploy: dashboard 200, `/api/health` 200, `/p/*` rewrite serving from S3, ping Lambda cold-start `loaded skills: ['ping', 'release_notes', 'sprint_status', 'voice_config', 'voice_say']` → `skill ping completed in 1.12s`.
- **PyPI**: pending publisher pre-registered for project `t3nets-sdk` against `WildEllie/t3nets` workflow `publish-sdk.yml`. First `0.1.0` upload claimed the name. `0.1.1` shipped same flow.
- **GitHub Environment `pypi`**: created with required reviewer (Ellie) so every publish needs explicit approval from the Actions tab.

---

## What's Next

Phase 7 (Server Slim — Wiring Layer Cleanup) is the next item in the roadmap. Both `adapters/aws/server.py` (~2,000 lines) and `adapters/local/dev_server.py` (~1,460 lines) still contain inline route wiring even though Phase 5d extracted the handler logic to `adapters/shared/handlers/`. The work is mostly mechanical moves but touches live entry points on AWS and local — gate on a smoke-test pass after each.

Backlog noted during the session:
- The `t3nets` console entry point isn't installed in the dev venv (`venv/bin/t3nets` doesn't exist). Workaround used: `python -m t3nets_sdk.cli.main`. Likely a stale editable install — `pip install --force-reinstall -e ./sdk` should fix it. Worth a follow-up so docs that say `t3nets practice ...` actually work copy-paste from the dev venv.
- `docs/README.md` still says **"Current phase: 1b"** in the Project Status section — out of date by many phases. Cleanup pass needed.
- `agent/practices/dev-jira/practice.yaml` still has `pages: []`. The CDN+S3 path is in place but no built-in practice exercises it; the first uploaded practice ZIP that ships pages will be the real end-to-end test.
