# Handoff: Show Tenant Name in Nav Bar

**Date:** 2026-02-26
**Status:** Completed
**Roadmap Item:** Phase 2b Feature 1 — Show tenant name in nav bar across all pages

## What Was Done

Added the tenant name to the navigation bar on all three dashboard pages (chat, settings, health). The `/api/auth/me` endpoint now returns `tenant_name` alongside the existing user/tenant fields. Each page's `checkAuth()` function reads this value and displays it in the nav bar next to the platform/stage badges, separated by a subtle vertical border.

## Key Files Changed

| File | What Changed |
|------|-------------|
| `adapters/local/dev_server.py` | Added `tenant_name: tenant.name` to `/api/auth/me` response |
| `adapters/aws/server.py` | Added `tenant_name: tenant.name` to `/api/auth/me` response; consolidated tenant fetch (reuses the same `get_tenant()` call that already retrieves `tenant_status`) |
| `adapters/local/chat.html` | Added `.nav-tenant-name` CSS, `<span id="nav-tenant-name">` in nav, JS to populate from `auth/me` (both auth and local modes) |
| `adapters/local/settings.html` | Same nav span + CSS; expanded `checkAuth()` to call `/api/auth/me` and populate tenant name |
| `adapters/local/health.html` | Same nav span + CSS; expanded `checkAuth()` to call `/api/auth/me` and populate tenant name |

## Architecture & Design Decisions

- **Tenant name comes from `/api/auth/me`**, not `/api/settings`. This keeps the settings endpoint focused on configuration and avoids an extra fetch on pages that don't load settings (health page).
- **Both local and AWS modes work.** In local mode, `auth/config` returns `enabled: false`, so the frontend falls through to an `else` branch that still fetches `/api/auth/me` for tenant info. In AWS mode, it's fetched alongside the JWT validation flow.
- **AWS server avoids duplicate DynamoDB calls.** The `_handle_auth_me()` method already fetched the tenant to get `tenant_status`. We now grab `tenant.name` from the same object rather than making a second `get_tenant()` call.
- **Hidden by default.** The `<span>` starts with `display:none` and only becomes visible if `tenant_name` is non-empty, so it degrades gracefully.
- **Consistent styling across pages.** All three pages use the same `.nav-tenant-name` CSS class: 13px gray text with a 1px left border separator.

## Current State

- **What works:** Tenant name displays in nav bar on chat, settings, and health pages in both local dev and AWS modes.
- **What doesn't yet:** Other Phase 2b features (settings API, skill toggles, integration config).
- **Known issues:** None for this feature. The git `.git/HEAD.lock` issue from the VM persists but doesn't block work (warnings during commit are cosmetic).

## How to Pick Up From Here

The next Phase 2b item is **Feature 2: Extend settings API to expose full TenantSettings**. This involves:

1. Update `GET /api/settings` to return `enabled_skills`, `enabled_channels`, and other `TenantSettings` fields
2. Update `POST /api/settings` to accept those new fields
3. Update the settings page UI to display/edit them

The `TenantSettings` model in `agent/models/tenant.py` already has all the fields defined (`enabled_skills`, `enabled_channels`, `system_prompt_override`, etc.) — they just aren't exposed through the API or UI yet.

## Dependencies & Gotchas

- **No Terraform changes needed** for this feature — it's purely backend response + frontend display.
- **No new API routes** — uses existing `/api/auth/me`.
- The settings and health pages previously did NOT call `/api/auth/me` at all. Now they do, which adds one extra API call on page load. This is minimal overhead since it's a lightweight DynamoDB lookup.
- The `Tenant.name` field is set during seeding (`scripts/seed.sh`). If a tenant was seeded without a name, it would show as empty and the span stays hidden.
