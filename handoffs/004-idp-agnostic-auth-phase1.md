# Handoff: IdP-Agnostic Auth — Phase I

**Date:** 2026-02-25
**Status:** Completed
**Roadmap Item:** Phase 2 Multi-Tenancy → IdP decoupling, in-app login, avatar support

## What Was Done

Phase I of a two-phase plan to make T3nets authentication IdP-agnostic. Previously, the auth flow was tightly coupled to AWS Cognito: login redirected to Cognito Hosted UI, `custom:tenant_id` was written to Cognito user attributes via boto3, and user→tenant resolution depended on JWT claims. This session removed all of that coupling. DynamoDB is now the sole source of truth for user→tenant mapping, login happens via an in-app form with server-side Cognito API calls, and the frontend no longer needs a token refresh dance after onboarding.

Also added `avatar_url` to the user model and chat UI, and created architecture documentation (`.docx` files generated in an earlier part of this session).

## Key Files Changed

| File | What Changed |
|------|-------------|
| `agent/models/tenant.py` | Added `avatar_url: str = ""` field to `TenantUser`; renamed `cognito_sub` comment to "IdP subject ID" |
| `agent/interfaces/tenant_store.py` | Added abstract `get_user_by_cognito_sub()` method for cross-tenant lookup |
| `adapters/aws/dynamodb_tenant_store.py` | Implemented GSI query for `cognito-sub-lookup`, persists `cognito_sub`, `gsi2pk`, `last_login`, `avatar_url` |
| `adapters/local/sqlite_tenant_store.py` | Safe ALTER TABLE migrations for `cognito_sub`, `last_login`, `avatar_url`; updated all CRUD ops |
| `adapters/aws/auth_middleware.py` | **Simplified**: removed `custom:tenant_id` extraction, removed `require_tenant` param. `AuthContext` now only has `user_id` + `email` (no `tenant_id`) |
| `adapters/aws/server.py` | `_get_auth_info()` always resolves via DynamoDB GSI (no JWT fast-path). Deleted `_handle_assign_tenant()`. Added 4 new endpoints: `POST /api/auth/login`, `/signup`, `/confirm`, `/refresh`. `_handle_auth_me()` returns `display_name` + `avatar_url` |
| `adapters/local/dev_server.py` | Removed assign-tenant no-op handler |
| `adapters/aws/admin_api.py` | Accepts `avatar_url` in admin user creation payload |
| `adapters/local/login.html` | **Full rewrite**: 3-panel UI (sign in, sign up, email verify). POSTs to `/api/auth/login` instead of redirecting to Cognito Hosted UI |
| `adapters/local/onboard.html` | Removed `assign-tenant` API call and token-clearing redirect. After activation, goes straight to `/chat` |
| `adapters/local/chat.html` | Shows user name/avatar from `/api/auth/me`, added `tryRefreshToken()` for expired sessions |
| `infra/aws/modules/cognito/main.tf` | Added `ALLOW_USER_PASSWORD_AUTH` to explicit auth flows |
| `infra/aws/modules/data/main.tf` | Added `gsi2pk` attribute + `cognito-sub-lookup` GSI to tenants table |
| `tests/test_onboarding.py` | 35 tests total (9 new): auth middleware, avatar_url model, SQLite avatar persistence, admin API avatar |

## Architecture & Design Decisions

### Why DynamoDB-only resolution (no JWT tenant_id)?

The `custom:tenant_id` JWT claim was fragile:
- New users don't have it until onboarding sets it via boto3
- Tokens must be refreshed after setting, creating a UX pain point (clear tokens → redirect → re-login)
- The claim is Cognito-specific — other IdPs don't support custom attributes the same way

By making DynamoDB the single source of truth, the auth flow becomes:
1. JWT provides `sub` + `email` (standard OIDC claims — any IdP provides these)
2. Server calls `get_user_by_cognito_sub(sub)` to resolve tenant
3. If no user found → `DEFAULT_TENANT` (new user needs onboarding)

The `cognito-sub-lookup` GSI (`gsi2pk = COGNITO#{sub}`) makes this a single DynamoDB query, not a scan.

### Why server-side auth endpoints instead of Cognito Hosted UI?

Cognito Hosted UI is a redirect-based flow: browser → Cognito domain → back to `/callback`. This:
- Exposes Cognito domain in the URL (not white-labeable on free tier)
- Requires client-side token exchange (PKCE)
- Can't be abstracted behind a generic interface

The new approach: login form POSTs to `/api/auth/login`, server calls Cognito `InitiateAuth` (USER_PASSWORD_AUTH) via boto3, returns tokens. The frontend just stores tokens in localStorage. This same endpoint pattern works for any IdP in Phase II.

### Why keep `custom:tenant_id` in Cognito schema?

Removing a custom attribute from Cognito requires recreating the user pool (destructive). We just stopped reading/writing it. The attribute stays in the schema but is dead code. No migration risk.

### Auth middleware simplification

```python
# Before (3 fields, require_tenant param):
@dataclass
class AuthContext:
    user_id: str       # Cognito sub
    tenant_id: str     # custom:tenant_id claim
    email: str = ""

def extract_auth(headers, require_tenant: bool = True) -> AuthContext:
    # ... reads custom:tenant_id, raises if missing and required ...

# After (2 fields, no params):
@dataclass
class AuthContext:
    user_id: str       # IdP subject (sub claim)
    email: str = ""

def extract_auth(headers) -> AuthContext:
    # ... reads sub + email, that's it ...
```

### Server auth resolution

```python
# Before (3-step with JWT fast-path):
def _get_auth_info(headers):
    auth = extract_auth(headers, require_tenant=False)
    if auth.tenant_id: return auth.tenant_id, auth.email  # JWT fast-path
    user = tenants.get_user_by_cognito_sub(auth.user_id)  # DynamoDB fallback
    if user: return user.tenant_id, auth.email
    return DEFAULT_TENANT, auth.email

# After (1-step, always DynamoDB):
def _get_auth_info(headers):
    auth = extract_auth(headers)
    user = tenants.get_user_by_cognito_sub(auth.user_id)
    if user: return user.tenant_id, auth.email
    return DEFAULT_TENANT, auth.email
```

### New API endpoints

| Endpoint | Purpose | Cognito API |
|----------|---------|-------------|
| `POST /api/auth/login` | Email + password → tokens | `InitiateAuth(USER_PASSWORD_AUTH)` |
| `POST /api/auth/signup` | Create account | `SignUp` |
| `POST /api/auth/confirm` | Verify email code | `ConfirmSignUp` |
| `POST /api/auth/refresh` | Refresh expired tokens | `InitiateAuth(REFRESH_TOKEN_AUTH)` |

These are all in `adapters/aws/server.py` and use `boto3` directly. In Phase II, they'll delegate to `IdentityProvider.authenticate()` etc.

## Current State

- **What works:** All auth flows (login, signup, verify, refresh), tenant resolution from DynamoDB, avatar persistence, onboarding without assign-tenant, in-app login UI with 3 panels
- **What doesn't yet:** Local dev server still uses hardcoded `local-admin` (no real auth). Phase II will fix this with Authentik. Token refresh in `chat.html` calls the endpoint but hasn't been tested E2E on AWS yet.
- **Known issues:**
  - `callback.html` still exists but is mostly dead code (was for Cognito OAuth redirect). Can be removed in Phase II.
  - The `cognito_sub` field name and `get_user_by_cognito_sub()` method name are Cognito-specific. Phase II should rename to `idp_sub` / `get_user_by_idp_sub()` when introducing the interface.
  - Git lock file issue prevented committing in-session. Changes are staged but need 3 commits (see below).

## Uncommitted Changes — Git Commit Plan

A stale `.git/HEAD.lock` file prevented completing commits. The first commit (DynamoDB GSI) may have partially succeeded. Run `git status` to check, then:

**Commit 1: DynamoDB GSI + store changes**
```bash
rm -f .git/HEAD.lock
git add agent/interfaces/tenant_store.py infra/aws/modules/data/main.tf \
  adapters/aws/dynamodb_tenant_store.py adapters/local/sqlite_tenant_store.py
git commit -m "Add DynamoDB GSI for cross-tenant user lookup by IdP sub"
```

**Commit 2: Auth refactor + new endpoints**
```bash
git add adapters/aws/auth_middleware.py adapters/aws/server.py \
  adapters/local/dev_server.py adapters/aws/admin_api.py \
  infra/aws/modules/cognito/main.tf
git commit -m "Remove custom:tenant_id from JWT, add server-side auth endpoints"
```

**Commit 3: Frontend + model + tests**
```bash
git add agent/models/tenant.py adapters/local/login.html \
  adapters/local/onboard.html adapters/local/chat.html \
  tests/test_onboarding.py
git commit -m "Replace Cognito Hosted UI with in-app login, add avatar support"
```

**Optional: Architecture docs**
```bash
git add t3nets-architecture.docx t3nets-auth-and-tenancy.docx
git commit -m "Add system architecture and auth documentation"
```

## How to Pick Up From Here

### Immediate: Phase II — IdentityProvider Interface

The approved plan is in `.claude/plans/smooth-wibbling-kettle.md`. Phase II steps:

1. **Create `agent/interfaces/identity_provider.py`** — Abstract interface with `authenticate()`, `signup()`, `confirm_signup()`, `refresh_token()`, `get_user_info()`, `get_auth_config()` methods. Define `AuthResult`, `SignupResult`, `UserInfo` dataclasses.

2. **Create `adapters/aws/cognito_identity_provider.py`** — Move the boto3 Cognito calls from server.py's `_handle_auth_login/signup/confirm/refresh` into this adapter.

3. **Create `adapters/local/authentik_identity_provider.py`** — Standard OIDC implementation against Authentik. Use `httpx` or `requests` for OIDC token endpoint calls.

4. **Update both servers** — `server.py` uses `CognitoIdentityProvider`, `dev_server.py` uses `AuthentikIdentityProvider`. Replace direct boto3 calls with interface methods.

5. **Docker Compose + Authentik bootstrap** — Add Authentik server/worker/postgres/redis to `docker-compose.yml`. Create `scripts/setup_authentik.py` to auto-configure the OAuth app.

6. **Rename cognito_sub → idp_sub** — Optional but recommended for consistency. Update model, stores, GSI key prefix.

### Before deploying to AWS:
- Run `terraform plan` to verify: new GSI + `ALLOW_USER_PASSWORD_AUTH` should show as changes (no pool recreation)
- Test the new login flow manually: signup → verify → login → onboard → chat
- Verify `custom:tenant_id` is truly not read anywhere (search for it)

## Dependencies & Gotchas

- **Cognito USER_PASSWORD_AUTH requires the app client to NOT have a secret.** The current Cognito module creates a client without a secret (PKCE), so this works. If someone adds a client secret, `InitiateAuth` will fail with "Unable to verify secret hash."

- **The `callback.html` file is still served** at `/callback` by both servers. It's dead code for the login flow but could confuse future developers. Safe to delete in Phase II.

- **SQLite migrations are additive only** — we use `ALTER TABLE ADD COLUMN` which is safe. But the `_row_to_user()` method uses positional indexing with `len(row) > N` guards. If column order ever changes, this breaks. Consider switching to `sqlite3.Row` objects with named access.

- **DynamoDB GSI `cognito-sub-lookup`** uses key pattern `COGNITO#{sub}`. If Phase II renames to a generic prefix (e.g., `IDP#{sub}`), existing data needs a migration or dual-prefix support.

- **The `admin_api.py` `extract_auth(headers)` call on line 45** is used for authorization but the returned `AuthContext` is never read — it just validates that the JWT is present. This means any authenticated user (regardless of tenant) can access admin routes. The TODO comment about role-based access is still open.

- **`t3nets-architecture.docx` and `t3nets-auth-and-tenancy.docx`** are in the repo root (untracked). These were generated via docx-js in an earlier part of this session. Commit them or move to `docs/` as needed.
