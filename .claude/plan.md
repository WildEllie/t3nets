# Phase 2: Cognito Multi-Tenancy — Implementation Plan

## Summary

Add AWS Cognito authentication with custom login UI, tenant isolation via JWT, and admin API for user/tenant management. Local dev remains auth-free.

---

## Step 1: Cognito Terraform Module

**Create** `infra/aws/modules/cognito/` with:
- `main.tf` — User pool (email verification, password policy), app client (Authorization Code + PKCE), custom attribute `tenant_id`
- `variables.tf` — callback URLs, password config
- `outputs.tf` — user_pool_id, client_id, endpoints

**Modify** `infra/aws/main.tf` — Wire in the new cognito module

No Lambda trigger needed — we'll store `tenant_id` as a custom Cognito attribute and read it from the JWT directly.

## Step 2: API Gateway JWT Authorizer

**Modify** `infra/aws/modules/api/main.tf`:
- Add `aws_apigatewayv2_authorizer` (JWT type) pointing to Cognito issuer + JWKS
- Apply authorizer to `$default` route
- Exempt `/health` endpoint (keep public for ALB health checks)
- Add request parameter mappings to forward `$context.authorizer.claims.sub` and `$context.authorizer.claims.custom:tenant_id` as headers (`X-Auth-UserId`, `X-Auth-TenantId`)

## Step 3: Auth Middleware for AWS Server

**Create** `adapters/aws/auth_middleware.py`:
- `AuthContext` dataclass: `user_id`, `tenant_id`, `email`
- `extract_auth(headers)` — reads `X-Auth-UserId` and `X-Auth-TenantId` headers set by API Gateway
- Returns 401 if missing

**Modify** `adapters/aws/server.py`:
- Import auth middleware
- Replace hardcoded `DEFAULT_TENANT` with `auth.tenant_id` from headers
- Add auth extraction to all `/api/*` handlers (chat, settings, history, clear)
- Keep `/health` and static HTML serving unauthenticated
- Add 401/403 error responses

## Step 4: Local Dev — No Auth Changes

**No changes** to `adapters/local/dev_server.py` — continues using `DEFAULT_TENANT = "local"` with no login required. All existing functionality works as-is.

## Step 5: Custom Login Page

**Create** `adapters/local/login.html`:
- Dark-themed login form (email + password) matching existing UI
- OAuth 2.0 PKCE flow: redirects to Cognito's token endpoint
- Signup link → Cognito hosted signup (only the signup page is hosted, not login)
- On success, stores `id_token` in localStorage, redirects to `/chat`

**Create** `adapters/local/callback.html`:
- Handles OAuth redirect with `?code=` parameter
- Exchanges auth code for tokens via Cognito token endpoint (client-side PKCE, no server involvement)
- Stores tokens in localStorage, redirects to `/chat`

**Modify** all three pages (`chat.html`, `health.html`, `settings.html`):
- Add auth check: if no token in localStorage, redirect to `/login`
- Add `Authorization: Bearer {token}` header to all `fetch()` calls
- Add logout button in nav bar (clears localStorage, redirects to `/login`)
- Show logged-in user email in nav

**Modify** `adapters/aws/server.py`:
- Add route for `/login` → serve login.html
- Add route for `/callback` → serve callback.html
- Add `GET /api/auth/me` endpoint — returns current user info from JWT

## Step 6: Tenant Model Extensions

**Modify** `agent/models/tenant.py`:
- Add `cognito_sub: str = ""` to `TenantUser` (links to Cognito user ID)
- Add `last_login: str = ""` to `TenantUser`

No DynamoDB schema changes needed — single-table design is schemaless, new attributes are stored automatically.

## Step 7: Admin API Endpoints

**Create** `adapters/aws/admin_api.py`:
- `POST /api/admin/tenants` — Create tenant + first admin user
- `GET /api/admin/tenants/{id}` — Get tenant details (admin only)
- `POST /api/admin/tenants/{id}/users` — Add user to tenant (creates in DynamoDB + Cognito)
- `GET /api/admin/tenants/{id}/users` — List tenant users
- All endpoints require auth + admin role check

**Modify** `adapters/aws/server.py`:
- Import and register admin API routes

## Step 8: Seed Second Tenant & Verify Isolation

**Modify** `scripts/seed.sh`:
- Add option to create a second tenant ("acme") with its own admin user
- Create Cognito user with `custom:tenant_id = acme`

**Create** `scripts/test_isolation.py`:
- Authenticate as user from tenant A, verify can only see tenant A data
- Authenticate as user from tenant B, verify can only see tenant B data
- Cross-tenant access returns 403

## Step 9: Terraform Variables & Deployment

**Create** `infra/aws/environments/dev.tfvars` additions:
- `cognito_callback_urls`, `cognito_logout_urls`

**Modify** `infra/aws/variables.tf`:
- Add Cognito-related variables

---

## Files Summary

### Create (8 files)
1. `infra/aws/modules/cognito/main.tf`
2. `infra/aws/modules/cognito/variables.tf`
3. `infra/aws/modules/cognito/outputs.tf`
4. `adapters/aws/auth_middleware.py`
5. `adapters/aws/admin_api.py`
6. `adapters/local/login.html`
7. `adapters/local/callback.html`
8. `scripts/test_isolation.py`

### Modify (8 files)
1. `infra/aws/main.tf` — Add cognito module
2. `infra/aws/modules/api/main.tf` — JWT authorizer
3. `adapters/aws/server.py` — Auth middleware, login routes, admin routes
4. `adapters/local/chat.html` — Auth check, token header, logout
5. `adapters/local/health.html` — Auth check, token header, logout
6. `adapters/local/settings.html` — Auth check, token header, logout
7. `agent/models/tenant.py` — Add cognito_sub, last_login
8. `scripts/seed.sh` — Second tenant seeding

### Unchanged
- `adapters/local/dev_server.py` — No auth locally
- `agent/` core — No cloud imports, stays clean

---

## Implementation Order

1. Terraform (Cognito module + API Gateway authorizer) — infrastructure first
2. Auth middleware + server changes — backend auth enforcement
3. Login page + callback + frontend auth — UI flow
4. Admin API — tenant/user management
5. Seed + test isolation — verify everything works
6. Deploy and test end-to-end
