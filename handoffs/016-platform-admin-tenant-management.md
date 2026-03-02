# Handoff: Platform Admin — Tenant Management Page

**Date:** 2026-03-02
**Status:** Complete, deployed to dev
**Roadmap item:** Phase 4.6 — Platform Admin

---

## What Was Done

Added a platform admin page (`/platform`) that lets default-tenant admins manage all tenants on the platform. Previously there was no UI or strictly-gated API for this — the existing `POST /api/admin/tenants` used relaxed auth (designed for self-service onboarding) and there was no list/suspend/delete surface.

A new `/api/platform/` URL prefix was introduced to keep strict platform operations clearly separate from the onboarding-permissive `/api/admin/` prefix.

---

## Files Created

| File | Purpose |
|------|---------|
| `adapters/aws/platform_api.py` | `PlatformAPI` class — all endpoints, strict auth guard |
| `adapters/local/platform.html` | Tenant management dashboard page |

## Files Modified

| File | Change |
|------|--------|
| `adapters/aws/server.py` | Import + instantiate `PlatformAPI`; route `/platform` page and all `/api/platform/` methods |
| `adapters/local/dev_server.py` | Route `/platform` page; 5 inline platform handler methods (no auth, local dev pattern) |
| `adapters/local/chat.html` | Inject Platform nav link for default-tenant admins |
| `adapters/local/health.html` | Same |
| `adapters/local/settings.html` | Same |
| `infra/aws/modules/api/main.tf` | `aws_apigatewayv2_route.public_platform` for `GET /platform` |

---

## API Endpoints (`/api/platform/`)

All endpoints require the calling user to be `tenant_id == "default"` and `role == "admin"` — verified via `get_user_by_cognito_sub()`. Returns 403 otherwise.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/platform/tenants` | List all tenants. For each: `tenant_id`, `name`, `status`, `created_at`, `user_count` (fetched via `list_users()`) |
| `POST` | `/api/platform/tenants` | Create tenant + admin invitation. Body: `tenant_name`, `admin_email`, `admin_name`. Server slugifies name, checks uniqueness (appends `-2`, `-3` if needed), creates `Tenant` + `Invitation`. Returns `tenant_id`, `tenant_name`, `invite_code`, `invite_url`, `admin_name`, `admin_email`. |
| `PATCH` | `/api/platform/tenants/{id}/suspend` | Set `status = "suspended"`. Refuses if `id == "default"` (400). |
| `PATCH` | `/api/platform/tenants/{id}/activate` | Set `status = "active"`. |
| `DELETE` | `/api/platform/tenants/{id}` | Tombstone: set `status = "deleted"`. Refuses if `id == "default"` (400). Preserves all user records. |

---

## Auth Guard Pattern (`platform_api.py`)

```python
auth = extract_auth(headers)
user = asyncio.run(self.tenants.get_user_by_cognito_sub(auth.user_id))
if not user or user.tenant_id != DEFAULT_TENANT or user.role != "admin":
    return {"error": "Forbidden"}, 403
```

This is stricter than `admin_api.py` — no relaxed-auth paths exist in `platform_api.py`.

---

## Frontend (`platform.html`)

Same skeleton as `health.html`: dark nav, SessionManager, session-expired modal.

**Access guard in `checkAuth()`:**
```javascript
if (me.tenant_id !== 'default' || me.role !== 'admin') {
    window.location.href = '/chat';
    return;
}
```

**Tenant table:** Name | Tenant ID | Status | Users | Created | Actions

- Status badges: green (active), amber (suspended), red (deleted), blue (onboarding)
- Action buttons gated by row status:
  - `active` / `onboarding`: **Suspend**
  - `suspended`: **Activate** + **Delete**
  - `deleted`: no buttons (text: "Deleted")
  - `default` tenant: no buttons (text: "Protected")
- Suspend and Delete both prompt `confirm()` before calling the API

**Create Tenant dialog:**
- Fields: Tenant Name (live-updates slug preview), Admin Name, Admin Email, Tenant ID (read-only)
- On submit → `POST /api/platform/tenants`
- On success: form replaced with invitation email text (same green-box + copy pattern as Settings → Team tab)
- Close button dismisses dialog and refreshes tenant list

---

## Nav link injection (chat.html, health.html, settings.html)

In both auth-mode and local-mode branches of `checkAuth()`, after all existing role checks:

```javascript
if (me.tenant_id === 'default' && me.role === 'admin') {
    const pl = document.createElement('a');
    pl.href = '/platform';
    pl.textContent = 'Platform';
    document.querySelector('.nav-links').appendChild(pl);
}
```

The platform.html nav itself has the Platform link hardcoded as `.active`.

---

## Invitation compatibility

No changes to the invitation accept flow. `POST /api/invitations/accept` already sets `user.role = invitation.role`, so the invited first admin lands in their new tenant as `role = "admin"`. Tenants are created with `status = "active"` so the onboarding redirect in `chat.html` does not fire.

---

## Terraform note

The `GET /platform` API Gateway route was added to `infra/aws/modules/api/main.tf` but **not yet applied via `terraform apply`**. Without it, direct navigation to `/platform` hits the `$default` catch-all (which has a JWT authorizer and will return 401 before the page JS runs). Run:

```bash
cd infra/aws && terraform apply -var-file=environments/dev.tfvars
```

The ECS container itself is already deployed with the new code (`./scripts/deploy.sh` completed successfully). The API handles `/api/platform/` routes correctly without the Terraform change — only the bare page URL is affected.

---

## Local dev notes

`dev_server.py` handlers have no auth check (consistent with all other local dev handlers). The `DEFAULT_TENANT` in dev_server is `"local"`, so the platform page's `checkAuth()` guard (`me.tenant_id !== 'default'`) will redirect away unless you test with a mock that returns `tenant_id: "default"`. The API endpoints themselves work fine locally — useful for manual testing via curl.

---

## What's next

- Run `terraform apply` to activate the `GET /platform` API Gateway route in dev
- Consider whether to add an audit log entry when platform operations happen (suspend/delete)
- Phase 4c (email delivery via SES) would let the create-tenant flow send the invitation automatically rather than requiring copy-paste
