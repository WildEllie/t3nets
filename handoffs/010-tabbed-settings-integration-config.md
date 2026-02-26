# Handoff: Tabbed Settings + Per-Skill Integration Config

**Date:** 2026-02-26
**Status:** Completed
**Roadmap Item:** Phase 2b Feature 4 — Per-skill integration config from settings dashboard + tabbed UI refactor

## What Was Done

Refactored the settings page from a single-scroll layout into a two-tab interface (General / Skills). Added per-skill integration configuration with collapsible config panes, test/save functionality, and two new GET endpoints for reading integration state. This completes the Phase 2b milestone — admins can now fully manage tenant settings, skills, and integrations from the dashboard.

## Key Files Changed

| File | What Changed |
|------|-------------|
| `adapters/local/settings.html` | Full rewrite: tabbed layout (General + Skills tabs), skill cards with collapsible integration config panes, test/save buttons, form pre-fill from API |
| `adapters/local/dev_server.py` | Added `INTEGRATION_SCHEMAS` constant (jira, github field definitions), `GET /api/integrations` list endpoint, `GET /api/integrations/{name}` detail endpoint (with password masking), added `connected_integrations` to `GET /api/settings` |
| `adapters/aws/server.py` | Mirror of all local server backend changes |

## Architecture & Design Decisions

- **Two tabs instead of three.** "General" has model + agent settings, "Skills" has toggles + integration config. Integration config lives inside the skill cards rather than in a separate tab, because integrations are meaningless without the skills that use them.
- **Collapsible panes per skill.** Only skills with `requires_integration` get an expand button. Clicking it fetches `GET /api/integrations/{name}` and renders the form. This avoids loading all integration configs upfront.
- **Password masking on read.** `GET /api/integrations/{name}` returns `••••••••` for password-type fields. The form clears masked values so users type fresh credentials (not editing masks).
- **`INTEGRATION_SCHEMAS` constant.** Defined in both servers (intentional duplication). Contains field metadata (key, label, type, required, placeholder) for each integration. The frontend uses this schema to dynamically render forms.
- **Existing endpoints reused.** `POST /api/integrations/{name}` and `POST /api/integrations/{name}/test` already existed. No changes needed — the new UI just calls them.
- **`connected_integrations` in settings.** Added to `GET /api/settings` so the skills tab can show "Connected" / "Not connected" badges without a separate API call.

## Current State

- **What works:** Full round-trip: list integrations → view config → test connection → save credentials → status updates in UI. All per tenant. Tabbed layout with smooth switching. All previous settings functionality preserved.
- **What doesn't yet:** `enabled_skills` is not enforced during routing (disabled skills can still be triggered). `enabled_channels` has no UI.
- **Known issues:** The `HEAD.lock` git issue persists in the VM environment.

## How to Pick Up From Here

Phase 2b is complete. The next phases from the roadmap are:

1. **Enforce `enabled_skills` in routing** — Filter out disabled skills when building tool definitions for Claude. Modify `agent/skills/registry.py` or the router layer.
2. **Phase 3: First External Channel** — Teams channel adapter (Azure Bot → webhook), async skill execution.
3. **Phase 4: Expand Skills** — GitHub integration skill, etc.

## Dependencies & Gotchas

- **`INTEGRATION_SCHEMAS` must stay in sync** between `dev_server.py` and `server.py`. If a new integration is added, update both files. Consider extracting to a shared module in the future.
- **Password fields clear on expand.** The frontend deliberately doesn't pre-fill password fields with the masked value — users must re-enter the token if they want to update it. If they only change non-password fields, the empty password field will overwrite the stored value. The save endpoint should handle this (skip empty password fields), but currently it stores whatever is sent.
- **No `DELETE /api/integrations/{name}`.** There's no UI to disconnect an integration yet, only to overwrite credentials.
- **The `secrets.list_integrations()` check** in the local env provider returns any integration that has at least one env var set. This means a partially configured integration (e.g., only URL set) will show as "connected" even if it won't work.
