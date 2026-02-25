# Handoff: Second Tenant Seed & Isolation Verification

**Date:** 2026-02-25
**Status:** Completed
**Roadmap Item:** Phase 2 — Seed a second tenant, verify isolation

## What Was Done

Seeded a second tenant ("acme") in both local development and AWS environments, then rewrote and expanded `test_tenant_isolation.py` from 7 tests (broken after Phase I auth changes) to 27 passing tests that comprehensively verify multi-tenant data isolation across conversations, tenant settings, users, auth middleware, channel mappings, and the seed process itself. This completes the Phase 2 milestone: "Two teams onboarded, data fully isolated."

## Key Files Changed

| File | What Changed |
|------|-------------|
| `adapters/local/dev_server.py` | Added second `seed_default_tenant()` call for "acme" tenant in `init()` |
| `scripts/seed.sh` | Added admin user seed for the second tenant (DynamoDB `USER#admin` record) |
| `tests/test_tenant_isolation.py` | Full rewrite: 27 tests across 7 test classes (was 7 tests, 3 broken) |

## Architecture & Design Decisions

**Local dev seeds two tenants on every startup:**
- `local` — all skills enabled, admin@local.dev
- `acme` — only `sprint_status` + `ping`, admin@acme.dev

This mirrors the AWS seed.sh pattern which already seeded two tenants but was missing the admin user for the second one.

**Auth tests fixed for IdP-agnostic model:** The old tests referenced `auth.tenant_id` on `AuthContext` and `custom:tenant_id` in JWTs — both removed in Phase I. The new tests verify that `AuthContext` only has `user_id` + `email` (no `tenant_id` attribute), matching the DynamoDB-only resolution approach.

**Import workaround for boto3:** `adapters/aws/__init__.py` eagerly imports boto3-dependent modules. Tests use `importlib.util.spec_from_file_location()` to load `auth_middleware.py` directly by file path, bypassing the package `__init__`.

## Current State

- **What works:**
  - Local dev server seeds both "local" and "acme" tenants on startup
  - AWS seed.sh creates both tenants with admin users
  - 27 isolation tests + 35 onboarding tests = 62 tests all passing
  - Conversations, settings, users, channel mappings all verified isolated

- **What doesn't yet:**
  - No UI to switch between tenants in local dev (dashboard hardcodes `DEFAULT_TENANT = "local"`)
  - No way to log in as an acme user from the dashboard — local dev bypasses auth entirely

- **Known issues:**
  - `test_error_handler.py`, `test_release_notes.py` fail to import due to missing `pytest` module — pre-existing, unrelated
  - Git HEAD.lock from previous session may still need manual cleanup

## How to Pick Up From Here

Phase 2 is now **fully complete**. Next phases in order:

1. **Phase 3: First External Channel** — Teams channel adapter, async skill execution
2. **Phase 4: Expand Skills** — Meeting prep, email triage, skill marketplace
3. **Phase 5: Practices** — Skill bundles, per-tenant practice selection, custom practices

Optional enhancements before moving on:
- Add a tenant switcher dropdown to local dev dashboard (for testing acme tenant)
- Deploy Phase I + seed changes to AWS (`terraform apply` + `deploy.sh` + `seed.sh`)

## Dependencies & Gotchas

- **seed_default_tenant() is idempotent:** Calling it twice for the same tenant_id only updates `enabled_skills` — doesn't duplicate the tenant or admin user. This is by design for dev server restarts.
- **AWS seed.sh uses env vars:** `SECOND_TENANT_ID` (default: `acme`), `SECOND_TENANT_NAME` (default: `Acme Corp`), `SECOND_TENANT_EMAIL` (default: `admin@acme.dev`)
- **Test import trick:** If `adapters/aws/__init__.py` changes its imports, the `spec_from_file_location` import in `test_tenant_isolation.py` still works — it bypasses `__init__.py` entirely.

## Test Coverage Summary

| Test Class | Tests | What It Covers |
|-----------|-------|---------------|
| `TestConversationIsolation` | 4 | Cross-tenant conversation separation, clear isolation |
| `TestTenantStoreIsolation` | 4 | Independent settings, skill lists, not-found errors |
| `TestUserIsolation` | 7 | User scoping, same-ID independence, delete isolation, cognito_sub lookup, email scoping |
| `TestAuthMiddleware` | 5 | JWT parsing, no tenant_id, missing bearer/sub, malformed JWT |
| `TestSecondTenantSeed` | 5 | Two-tenant seed, admin users, skill independence, idempotency, skill updates |
| `TestChannelMappingIsolation` | 2 | Channel→tenant resolution, unmapped channel errors |
