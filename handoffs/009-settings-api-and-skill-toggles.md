# Handoff: Extend Settings API & Skill Toggles

**Date:** 2026-02-26
**Status:** Completed
**Roadmap Item:** Phase 2b Features 2 & 3 — Full TenantSettings API + skill toggle UI

## What Was Done

Extended the settings API (`GET` and `POST /api/settings`) in both the local and AWS servers to expose all `TenantSettings` fields. Added a skill toggle section and agent settings form (system prompt, limits) to the settings page UI. Skill toggles save immediately on click; agent settings (prompt, tokens, limits) use a single "Save" button.

## Key Files Changed

| File | What Changed |
|------|-------------|
| `adapters/local/dev_server.py` | `_handle_settings_get()` returns `enabled_skills`, `available_skills`, `system_prompt_override`, `max_tokens_per_message`, `messages_per_day`, `max_conversation_history`. `_handle_settings_post()` accepts all these fields with validation. |
| `adapters/aws/server.py` | Identical API changes to the AWS server. |
| `adapters/local/settings.html` | New CSS for skill toggles and form fields. New HTML sections: Skills (toggle list), Agent Settings (system prompt textarea, 3 numeric inputs, save button). New JS: `renderSkills()`, `toggleSkill()`, `saveAgentSettings()`. `loadSettings()` populates all fields. |

## Architecture & Design Decisions

- **Skills toggle is immediate-save.** Each checkbox change POSTs the full `enabled_skills` list to the server. This gives instant feedback without needing a separate save button for skills. The agent settings (prompt, limits) use a Save button since those are text/number fields where the user types.
- **Validation lives server-side.** Both servers validate `enabled_skills` against the skill registry (`list_skill_names()`), and validate numeric ranges for tokens (256-16384), messages per day (≥1), and history length (1-100).
- **`available_skills` includes metadata.** The GET response returns `available_skills` as an array of `{name, description, requires_integration}` so the UI can display descriptions and integration requirement tags without needing a separate skills endpoint.
- **`enabled_skills` defaults to empty.** A new tenant starts with no skills enabled. The admin enables what they need.
- **Both servers share identical logic** for settings POST validation. This is intentional duplication to avoid adding a shared module — the local and AWS servers remain independent.

## Current State

- **What works:** Full TenantSettings round-trip (read → edit → save) on both local and AWS. Skills can be toggled on/off. System prompt, max tokens, daily message limit, and conversation history length can be set.
- **What doesn't yet:** Feature 4 (per-skill integration config) — Jira/GitHub credentials UI is not built yet.
- **Known issues:** The `enabled_skills` list isn't yet enforced during routing — a disabled skill can still be triggered by Claude if the router sends it. Enforcing this should happen in the router/skill registry when building tool definitions.

## How to Pick Up From Here

1. **Phase 2b Feature 4: Per-skill integration config.** Skills like `sprint_status` and `release_notes` require Jira credentials. The UI needs an expandable config section per skill where admins can enter integration fields (URL, API token, etc.) and save them via the existing `SecretsProvider` interface.
2. **Enforce `enabled_skills` in routing.** When building tool definitions for Claude, filter out skills not in the tenant's `enabled_skills` list. This happens in `agent/skills/registry.py` or the router layer.
3. **`enabled_channels`** is returned by the API but has no UI or enforcement yet.

## Dependencies & Gotchas

- The `skills` module-level global must be initialized before the settings endpoints are called. Both servers do this at startup (`SkillRegistry()` + `load_from_directory()`).
- `available_skills` is built from the registry at request time, so newly deployed skills appear automatically.
- The `TenantSettings` dataclass defaults are the fallback — if a field is missing from DynamoDB, the dataclass default applies (e.g., `max_tokens_per_message=4096`).
- The `HEAD.lock` git issue persists in the VM. You may need to `rm -f .git/HEAD.lock` before committing.
