# Handoff: Onboarding Wizard

**Date:** 2026-02-24
**Status:** Completed
**Roadmap Item:** Phase 2 — Onboarding wizard

## What Was Done

Built a complete onboarding wizard that guides new users through tenant creation, Jira integration, AI model selection, and tenant activation. The wizard is a single-file vanilla HTML page (Bootstrap 5 CDN) served at `/onboard`, with backend endpoints added to both the AWS and local dev servers. A React app was scaffolded in `dashboard/onboard/` for future Phase 5 SPA conversion but cannot be built yet (npm unavailable in dev environment). Tests cover all backend endpoints with 17 passing tests.

## Key Files Changed

| File | What Changed |
|------|-------------|
| `adapters/aws/admin_api.py` | Relaxed auth for tenant creation (onboarding users without `tenant_id`), admin user creation with tenant, tenant ID validation regex, `_activate_tenant` method, `_update_tenant` method, PATCH routing |
| `adapters/aws/server.py` | Routes for `/onboard`, `/api/integrations/{name}`, `/api/integrations/{name}/test`, `/api/auth/assign-tenant`, `do_PATCH`, CORS updates, integration test helpers, enhanced `/api/auth/me` with `tenant_status` |
| `adapters/local/dev_server.py` | Matching routes for local dev: tenant CRUD, integration endpoints, activate, assign-tenant (no-op), `do_PUT`/`do_PATCH`, CORS, `/api/auth/me` with `tenant_status` |
| `adapters/local/onboard.html` | **New.** 4-step onboarding wizard: Create Team, Connect Jira, Choose AI Model, Done. Bootstrap 5.3.3 dark theme, vanilla JS, full API integration |
| `adapters/local/chat.html` | Added redirect to `/onboard` when user has no tenant or tenant status is "onboarding" (calls `/api/auth/me` on load) |
| `tests/test_onboarding.py` | **New.** 17 unit tests covering tenant creation, admin user, ID validation, activation, integration storage, API routing, and model lifecycle. Mocks boto3 to avoid import chain issues |
| `dashboard/onboard/` | **New.** React app scaffolding (Vite + Bootstrap) — not buildable yet, preserved for Phase 5 SPA conversion |

## Architecture & Design Decisions

**Vanilla HTML over React SPA**: npm was completely blocked in the dev environment (403 on all packages). Since existing pages (chat.html, settings.html) are vanilla HTML with Bootstrap CDN, the onboarding wizard follows the same pattern. This keeps the codebase consistent and avoids a build step. The React scaffolding in `dashboard/onboard/` is preserved for Phase 5 when the project converts to an SPA.

**Auth relaxation for onboarding**: New Cognito users don't have `custom:tenant_id` in their JWT yet, so `extract_auth()` raises `AuthError(403)`. The fix routes `POST /api/admin/tenants` *before* the auth check in `handle_request()`. All other admin routes still require full auth. The `/api/auth/me` endpoint also handles the 403 gracefully, returning `tenant_status: "onboarding"` so chat.html can redirect.

**Tenant lifecycle**: `status` field on Tenant goes `"onboarding"` → `"active"` via `PATCH /api/admin/tenants/{id}/activate`. The activation step is the last action in the wizard, followed by `POST /api/auth/assign-tenant` which sets `custom:tenant_id` on the Cognito user (so subsequent JWTs include the claim).

**Tenant ID validation**: Must be 3+ chars, lowercase alphanumeric with hyphens, no leading/trailing hyphens. Regex: `^[a-z0-9][a-z0-9-]*[a-z0-9]$`.

**Admin user creation**: Bundled with tenant creation via optional `admin_user` field in the POST body. Creates a `TenantUser` with `role: "admin"` and `cognito_sub` for JWT binding.

## Current State

- **What works:** Full onboarding flow — create team (with auto-slug from name), connect Jira (with test connection), choose AI model (loads from `/api/settings`), finalize (activate + assign tenant). Chat.html redirects unauthenticated/onboarding users to `/onboard`. All 17 backend tests pass.
- **What doesn't yet:** The React app in `dashboard/onboard/` can't be built (needs `npm install`). The `POST /api/auth/assign-tenant` on local dev is a no-op (returns `{"ok": true}`) since there's no Cognito locally.
- **Known issues:** None critical. The AWS `assign-tenant` endpoint uses `boto3.client("cognito-idp")` to set `custom:tenant_id` — this hasn't been tested against a live Cognito pool yet.

## How to Pick Up From Here

1. **Seed a second tenant** and verify data isolation (next Phase 2 roadmap item)
2. **Test against live AWS**: Deploy and verify the full onboarding → chat flow with real Cognito
3. **Phase 5 SPA conversion**: When ready, run `cd dashboard/onboard && npm install && npm run build` to get the React version working. The components are fully scaffolded with Bootstrap classes.
4. **Role-based access**: The Admin API TODO notes that role-based access beyond admin/member is needed

## Dependencies & Gotchas

- **boto3 import chain**: `adapters/aws/__init__.py` eagerly imports `BedrockProvider` which requires boto3. Tests mock it with `sys.modules["boto3"] = MagicMock()` before importing. If you restructure imports, keep this in mind.
- **JWT parsing in onboard.html**: The wizard reads `localStorage.getItem("id_token")` and decodes the JWT payload to pre-fill email/sub. This assumes the Cognito auth flow in chat.html has already stored the token.
- **CORS headers**: Both servers now allow `GET, POST, PUT, PATCH, OPTIONS` with `Content-Type, Authorization` headers. If adding new methods, update the CORS config in both `server.py` files.
- **Jira test connection**: Uses `urllib.request` to hit `{jira_url}/rest/api/3/myself` with basic auth. The `_test_jira` helper is duplicated in both servers — consider extracting to a shared module.
- **`/api/settings` endpoint**: The model selection step loads models from this existing endpoint. It must return `{ models: [...], ai_model: "default-id" }`.
