# Handoff: Cognito Authentication & JWT Tenant Resolution

**Date:** 2026-02-23 (retroactive — work was done in a prior session without handoff)
**Status:** Completed
**Roadmap Item:** Phase 2: Multi-Tenancy — Cognito user pool + auth flow, tenant resolution from JWT

## What Was Done

Implemented end-to-end Cognito authentication with JWT-based multi-tenant isolation. The full OAuth 2.0 PKCE flow runs from frontend login through API Gateway JWT validation to server-side tenant resolution. Also built admin API endpoints for tenant CRUD operations.

## Key Files Changed

| File | What Changed |
|------|-------------|
| `infra/aws/modules/cognito/main.tf` | User Pool with email sign-in, `custom:tenant_id` attribute, PKCE app client, hosted UI domain |
| `infra/aws/modules/cognito/variables.tf` | Callback/logout URLs, token validity, password policy config |
| `infra/aws/modules/cognito/outputs.tf` | Exports pool ID, endpoint, client ID, auth domain for downstream modules |
| `infra/aws/modules/api/main.tf` | JWT Authorizer on API Gateway with public routes for `/login`, `/callback`, `/api/auth/config` |
| `adapters/aws/auth_middleware.py` | `AuthContext` dataclass + `extract_auth()` — decodes JWT payload for `sub`, `custom:tenant_id`, `email` |
| `adapters/aws/server.py` | `/api/auth/config` and `/api/auth/me` endpoints; `_get_auth_tenant()` for tenant-scoped requests |
| `adapters/aws/admin_api.py` | `AdminAPI` class with CRUD: list/get/create/update tenants, all behind auth |
| `adapters/local/login.html` | Login page — loads Cognito config, redirects to hosted UI |
| `adapters/local/callback.html` | OAuth callback — exchanges auth code for tokens, stores in localStorage |
| `adapters/local/chat.html` | Attaches `Authorization: Bearer {id_token}` to all API calls |
| `adapters/local/health.html` | Same auth header attachment |
| `adapters/local/settings.html` | Same auth header attachment |
| `agent/models/tenant.py` | `TenantUser` model with `cognito_sub` and `last_login` fields |
| `tests/test_tenant_isolation.py` | Tests for `extract_auth()` — valid JWT, missing header, missing tenant claim |

## Architecture & Design Decisions

**PKCE flow over implicit grant** — No client secret exposed to the browser. Cognito hosted UI handles credentials so the app server never sees passwords. (ADR-016)

**API Gateway JWT validation** — Token signature is verified at the API Gateway level, so the Python middleware only needs to base64-decode the payload to read claims. No crypto libraries needed in the container. (ADR-017)

**`custom:tenant_id` claim** — Custom Cognito attribute links each user to a DynamoDB tenant. Set when a user is created/assigned to a tenant. This is the core of multi-tenant isolation — every API request extracts `tenant_id` from the JWT and scopes all data access.

**Client-side JWT decoding for user email** — The frontend decodes the `id_token` payload to display the logged-in user's email. No extra `/api/auth/me` round-trip needed. (ADR-017)

**User email in message metadata** — `user_email` is stored in the conversation turn metadata so chat messages show who sent them. `_strip_metadata()` removes it before sending to Claude. (ADR-018)

**Multi-environment callback URLs** — Both `localhost:8080` and the API Gateway URL are in Cognito's allowed callbacks. `window.location.origin` picks the right one automatically. (ADR-019)

## Current State

- **What works:** Full login → callback → token storage → authenticated API calls → tenant-scoped data. Cognito user pool deployed. JWT authorizer on API Gateway. Admin CRUD endpoints for tenants.
- **What doesn't yet:** Onboarding wizard (React), role-based access on admin API (any authenticated user can access admin), seeding a second tenant to verify isolation end-to-end.
- **Known issues:** Admin API has a `TODO` for role-based access — currently any authenticated user can hit admin endpoints. The `TenantUser` model exists but there's no user management API yet.

## How to Pick Up From Here

- Build the onboarding wizard (React) — form to create a tenant, invite users, connect integrations
- Add role-based access to admin API (check for `admin` role claim in JWT)
- Seed a second tenant and verify full data isolation end-to-end
- Consider adding user management endpoints (list users per tenant, assign roles)
- Token refresh flow — the frontend stores refresh tokens but doesn't yet use them to silently renew expired access tokens

## Dependencies & Gotchas

- **Cognito env vars:** `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID`, `COGNITO_AUTH_DOMAIN` must be set in ECS task definition. Terraform wires these from the Cognito module outputs.
- **Local dev bypass:** When `COGNITO_USER_POOL_ID` is empty (local dev), `_get_auth_tenant()` falls back to `DEFAULT_TENANT`. No auth enforcement locally.
- **API Gateway route order matters:** Public routes (`/login`, `/callback`, `/api/auth/config`, `/health`) must be explicit routes, not caught by the `$default` route which has the JWT authorizer.
- **Token validity:** ID and access tokens expire in 1 hour; refresh tokens last 30 days.
- **Decision log:** ADRs 016-019 all relate to the Cognito implementation. The "Pending Decisions" table has been updated to mark Cognito as decided.
