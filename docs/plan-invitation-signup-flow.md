# Plan: Two-Path Signup — Tenant Creation vs Invitation

## Context

The current signup flow creates a Cognito user but doesn't link them to a tenant. After signup, every user goes through the onboarding wizard to create a new tenant. There's no way for an existing tenant admin to invite users. In a real-world scenario:

- **Path 1 (Create Account):** A new user creates a tenant and becomes its admin → existing flow, just needs clearer labeling
- **Path 2 (Join via Invitation):** A tenant admin invites a user by email → user receives a link → signs up with tenant context pre-filled → automatically linked to that tenant as member → skips onboarding

---

## Phase 1: Invitation Backend

### 1.1 Invitation Data Model

New `Invitation` dataclass in `agent/models/tenant.py`:

```python
@dataclass
class Invitation:
    invite_code: str        # "inv_" + 32 random URL-safe chars
    tenant_id: str
    email: str              # Must match signup email
    role: str = "member"    # member or admin
    status: str = "pending" # pending | accepted | revoked
    invited_by: str = ""    # user_id of admin who created it
    created_at: str = ""
    expires_at: str = ""    # 14 days from creation
    accepted_at: str = ""
```

### 1.2 DynamoDB Schema

Store in existing tenants table (single-table design):

```
pk: INVITE#{invite_code}
sk: META
+ all Invitation fields
+ ttl: unix timestamp of expires_at (DynamoDB TTL auto-cleanup)
```

Terraform changes in `infra/aws/modules/data/main.tf`:
- Enable TTL on `ttl` attribute
- No new GSI needed — invites are looked up by code (pk)

### 1.3 Backend Endpoints

| Endpoint | Auth | Handler |
|----------|------|---------|
| `POST /api/admin/tenants/{id}/invitations` | JWT (admin) | Create invitation, return invite_code + URL |
| `GET /api/admin/tenants/{id}/invitations` | JWT (admin) | List pending invitations for tenant |
| `DELETE /api/admin/tenants/{id}/invitations/{code}` | JWT (admin) | Revoke invitation |
| `GET /api/invitations/validate?code=xxx` | Public | Validate invite, return tenant name + email |
| `POST /api/invitations/accept` | Public* | Accept invite, link user to tenant |

*Accept requires a valid Cognito JWT but the invite validation itself is public.

**Create invitation** (`admin_api.py`):
- Validate caller is admin of the tenant
- Check email not already a member
- Generate `inv_` + `secrets.token_urlsafe(32)`
- Store in DynamoDB with 14-day TTL
- Return invite code + constructed URL

**Validate invitation** (`server.py`):
- Look up by `pk=INVITE#{code}`
- Check status == "pending" and not expired
- Return tenant_name, email, role (no sensitive data)

**Accept invitation** (`server.py`):
- Requires JWT (user must have signed up + verified email first)
- Validate invite code, check email matches JWT email
- Create TenantUser in DynamoDB with cognito_sub + role from invite
- Mark invite as "accepted"
- Return tenant_id

### 1.4 Terraform (API Gateway)

New public routes in `infra/aws/modules/api/main.tf`:
- `GET /api/invitations/validate` — no JWT
- `POST /api/invitations/accept` — no JWT (server validates JWT manually)

Admin routes (`/api/admin/...`) already go through JWT catch-all.

### 1.5 Local Dev Support

- `dev_server.py`: Add mock endpoints for validate/accept
- `sqlite_tenant_store.py`: Store invitations in SQLite or in-memory dict

---

## Phase 2: Login/Signup UI Changes

### 2.1 New `/join` Page (or mode in login.html)

When user visits `/join?code=inv_xxx`:

1. Call `GET /api/invitations/validate?code=inv_xxx`
2. If invalid/expired → show error message with link to regular signup
3. If valid → show **invitation panel**:
   - "You've been invited to join **{tenant_name}**"
   - Email shown (read-only, from invitation)
   - Two options:
     - "I already have an account" → login form (email pre-filled)
     - "Create Account" → signup form (email pre-filled, locked)

### 2.2 Signup Panel Changes

- Accept optional `invite_code` state
- If invitation flow: email field is pre-filled and read-only
- After Cognito signup + email verification + login:
  - Call `POST /api/invitations/accept` with JWT + invite_code
  - On success → redirect to `/chat` (skip onboarding)

### 2.3 Login Panel Changes

- After successful login, if invite_code in state:
  - Call `POST /api/invitations/accept`
  - Redirect to `/chat`
- Original "Create Account" link stays as-is (→ new tenant flow)

### 2.4 Conditional Redirect in `checkAuth()` (chat.html)

Current logic in `GET /api/auth/me`:
- No tenant → redirect to `/onboard`

New logic:
- No tenant + no invite context → redirect to `/onboard` (create tenant)
- Has tenant + tenant status "active" → stay on `/chat`
- Has tenant + tenant status "onboarding" + role "admin" → redirect to `/onboard`

---

## Phase 3: Admin UI — Team Members & Invitations

### 3.1 New Tab in Settings Page

Add a third tab: **General | Skills | Team**

### 3.2 Team Tab Contents

**Current Members** section:
- Table: Name | Email | Role | Joined
- Data from new endpoint: `GET /api/admin/tenants/{id}/users`

**Pending Invitations** section:
- Table: Email | Role | Expires | Actions
- "Copy Link" button (copies invite URL to clipboard)
- "Revoke" button

**Invite New Member** form:
- Email input + Role dropdown (member/admin)
- "Create Invitation" button
- On success: show invite URL, auto-copy to clipboard, toast notification

### 3.3 Backend for Team Tab

New endpoints:
- `GET /api/admin/tenants/{id}/users` — list users (already exists partially in admin_api)
- Reuse invitation CRUD from Phase 1

---

## Phase 4: Email Delivery (Future)

Phase 1 uses **copy-paste invite links**. When ready:
- Add SES to Terraform (domain verification, IAM permissions)
- Create HTML email template with invite link + tenant branding
- Call SES from `_create_invitation()` endpoint
- Keep copy-link as fallback

---

## Email Delivery (Phase 1: No SES)

In Phase 1, the admin creates an invitation and gets a link to share manually. The UI auto-copies the link to clipboard. No email infrastructure needed.

---

## Security

- **Token entropy:** `secrets.token_urlsafe(32)` = 256 bits
- **Single-use:** Status changes to "accepted" after use
- **Email match:** Accept verifies JWT email matches invitation email
- **TTL:** 14-day expiry, DynamoDB auto-cleanup
- **Admin-only creation:** Verify caller role before creating invites
- **No token in logs:** Invite codes only in request bodies, not URL paths for POST

---

## Files to Modify

| File | Changes |
|------|---------|
| `agent/models/tenant.py` | Add `Invitation` dataclass |
| `adapters/aws/admin_api.py` | Add invitation CRUD methods (create, list, revoke, accept) |
| `adapters/aws/server.py` | Wire new routes (validate, accept in do_GET/do_POST) |
| `adapters/aws/dynamodb_tenant_store.py` | Add invitation persistence methods |
| `adapters/local/dev_server.py` | Mock invitation endpoints for local dev |
| `adapters/local/sqlite_tenant_store.py` | SQLite invitation storage |
| `adapters/local/login.html` | Add invitation panel, modify signup to support invite flow |
| `adapters/local/settings.html` | Add Team tab with members list + invitation management |
| `infra/aws/modules/api/main.tf` | Add public routes for validate/accept |
| `infra/aws/modules/data/main.tf` | Enable DynamoDB TTL |

## Implementation Order

1. Data model + DynamoDB persistence (Invitation dataclass, store methods)
2. Backend endpoints (create, validate, accept, list, revoke)
3. Terraform (API Gateway routes, TTL)
4. Login UI (join page, invitation-aware signup)
5. Settings UI (Team tab with members + invitations)
6. Local dev support
7. Testing

## Verification

1. Create invitation via API → verify DynamoDB record
2. Visit `/join?code=xxx` → verify tenant name + email shown
3. Sign up via invitation → verify user linked to correct tenant
4. Login after invitation signup → verify redirect to `/chat` (not `/onboard`)
5. Admin settings → verify Team tab shows members + pending invites
6. Revoke invitation → verify it can't be accepted
7. Wait for expiry → verify DynamoDB TTL cleans up
