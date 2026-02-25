# Handoff: Fix Auth — API Gateway Routes & Cognito Name Attribute

**Date:** 2026-02-25
**Status:** Completed
**Roadmap Item:** Phase 2 Multi-Tenancy → Auth flow fix (blocking login/signup on AWS)

## What Was Done

Fixed two blocking issues preventing login and signup on the AWS deployment:

1. **API Gateway 401 on auth endpoints.** The four auth POST endpoints (`/api/auth/login`, `/signup`, `/confirm`, `/refresh`) had no explicit routes in API Gateway. They fell through to the `$default` catch-all route, which has a JWT authorizer attached. Since users don't have tokens when logging in, API Gateway returned 401 before requests ever reached the ECS container. Added explicit public (no-auth) routes for all four endpoints.

2. **Signup failing with Cognito `name` attribute.** The signup handler was sending `name` as a Cognito user attribute, but the app client's `write_attributes` didn't include `name`. Added `name` to both `read_attributes` and `write_attributes` in the Cognito Terraform module, and restored the `name` attribute in the signup call.

Also fixed a latent bug where Cognito challenge responses (e.g., `NEW_PASSWORD_REQUIRED` for seeded users stuck in `FORCE_CHANGE_PASSWORD` state) were silently returning 200 with empty tokens, causing a redirect loop between `/login` and `/chat`.

## Key Files Changed

| File | What Changed |
|------|-------------|
| `infra/aws/modules/api/main.tf` | Added 4 public routes: `POST /api/auth/login`, `/signup`, `/confirm`, `/refresh` — no JWT authorizer |
| `infra/aws/modules/cognito/main.tf` | Added `name` to app client `read_attributes` and `write_attributes` |
| `adapters/aws/server.py` | Restored `name` in signup `UserAttributes`; added Cognito challenge detection in login handler (returns 403 with challenge name instead of empty 200) |
| `scripts/seed.sh` | Major upgrade: creates Cognito users via `admin-create-user`, sets permanent passwords, links to DynamoDB via `cognito_sub` + `gsi2pk`. Supports two tenants. |
| `scripts/deploy.sh` | Added deployment timer; fixed `$(start)` bug (was executing `start` as command) |
| `.env.example` | Added Cognito and second-tenant config vars |

## Architecture & Design Decisions

### Why explicit routes instead of a path-prefix wildcard?

API Gateway HTTP API v2 supports `{proxy+}` catch-all patterns, but we intentionally use explicit routes for auth endpoints. This makes it clear in the Terraform which routes are public vs. authenticated, and avoids accidentally exposing future `/api/auth/*` endpoints without JWT validation.

### Why handle Cognito challenges in the login handler?

When a Cognito user is in `FORCE_CHANGE_PASSWORD` status (e.g., created via `admin-create-user` without a permanent password), `initiate_auth` returns a challenge response instead of `AuthenticationResult`. The previous code didn't check for this — it returned 200 with empty tokens, causing the frontend to redirect to `/chat`, which redirected back to `/login` (no token in localStorage), creating a silent infinite loop with no errors.

The fix checks for `ChallengeName` in the response and returns a 403 with the challenge name. The frontend's existing error display handles this gracefully.

### Seed script Cognito user creation

The seed script now creates Cognito users with `admin-create-user` + `admin-set-user-password --permanent`. This:
- Suppresses the welcome email (`--message-action SUPPRESS`)
- Sets email as verified (`email_verified=true`)
- Skips the force-change-password flow (`--permanent` flag)
- Is idempotent — checks if user exists first via `admin-get-user`

**Important:** The `> /dev/null 2>&1` on `admin-set-user-password` means password-set failures are silent. If the password doesn't meet Cognito policy (min 8, uppercase, lowercase, number), the user stays in `FORCE_CHANGE_PASSWORD` state. The login handler now surfaces this as a clear error instead of a silent loop.

## Current State

- **What works:** API Gateway routes allow unauthenticated access to auth endpoints. Login, signup, confirm, and refresh requests reach the ECS container. Name attribute is set during signup. Cognito challenges are detected and reported to the frontend.
- **What doesn't yet:** No UI for handling `NEW_PASSWORD_REQUIRED` challenge (user just sees an error message). Token refresh in `chat.html` hasn't been tested E2E on AWS.
- **Known issues:**
  - `callback.html` is still dead code (leftover from Cognito Hosted UI redirect flow). Safe to remove.
  - `assign-tenant` route in API Gateway is dead code (endpoint removed from server in handoff 004). Safe to remove.
  - The `cognito_sub` field name is still Cognito-specific. Phase II should rename to `idp_sub`.

## How to Pick Up From Here

### Immediate: Deploy this fix

```bash
# 1. Terraform first — adds API Gateway routes + Cognito write_attributes
cd infra/aws
terraform plan -var-file=environments/dev.tfvars
terraform apply -var-file=environments/dev.tfvars

# 2. Re-seed (optional — only if you want fresh Cognito users linked to DynamoDB)
cd ../..
./scripts/seed.sh

# 3. Deploy new container (has challenge handling + name attribute fix)
./scripts/deploy.sh

# 4. Test: /login → Create Account → name + email + password → verify code → login
```

**Critical ordering:** Terraform must be applied before deploying the container. The container sends `name` during signup, and the Cognito app client must have `name` in `write_attributes` first.

### Next: Phase II — IdentityProvider Interface

The approved plan from handoff 004 still applies. Key steps:
1. Create `agent/interfaces/identity_provider.py`
2. Create `adapters/aws/cognito_identity_provider.py` (move boto3 calls from server.py)
3. Create `adapters/local/authentik_identity_provider.py`
4. Update both servers to use the interface
5. Docker Compose with Authentik for local dev

### Clean up dead code

- Remove `callback.html` and its route
- Remove `POST /api/auth/assign-tenant` route from API Gateway
- Consider renaming `cognito_sub` → `idp_sub` across the codebase

## Dependencies & Gotchas

- **Terraform apply order matters.** The API Gateway routes and Cognito write_attributes must be deployed before the new container code. If you deploy the container first, signup will fail (Cognito rejects the `name` attribute).

- **Seeded user passwords.** If `ADMIN_PASSWORD` in `.env` doesn't meet Cognito's password policy (min 8 chars, uppercase, lowercase, number), `admin-set-user-password` fails silently and the user is stuck in `FORCE_CHANGE_PASSWORD`. The login handler now returns a 403 with the challenge name, but there's no UI to handle it — the user would need to be recreated or have their password reset via AWS Console.

- **API Gateway route precedence.** HTTP API v2 uses most-specific-match routing. `POST /api/auth/login` (explicit) takes priority over `$default` (catch-all). This is correct — we want auth routes to skip the JWT authorizer.

- **The git HEAD.lock warnings** during commits are harmless — the VM filesystem doesn't allow unlinking temp files, but the commits succeed.
