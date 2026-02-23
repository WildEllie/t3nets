# Handoff: Fix Release Notes Skill

**Date:** 2026-02-23
**Status:** Completed
**Roadmap Item:** Phase 4: Expand Skills — release_notes skill fix

## What Was Done

Fixed five issues with the `release_notes` skill: (1) added missing routing rules in `rule_router.py` so messages actually route to the skill via Tier 1 instead of falling through to Claude, (2) eliminated hallucination by ensuring the skill is invoked directly rather than relying on Claude to fabricate release data, (3) added release name parameter extraction from user messages, (4) added future/unstarted release detection so the worker doesn't try to summarize releases with no work, and (5) created a comprehensive test suite. Also fixed two integration issues: added `project_key` to the Jira secrets mapping in `env_secrets.py`, and migrated the JQL search from the deprecated `/rest/api/3/search` endpoint (removed by Atlassian Aug 2025) to the new `/rest/api/3/search/jql` with `nextPageToken` pagination.

## Key Files Changed

| File | What Changed |
|------|-------------|
| `agent/router/rule_router.py` | Added `release_notes` entries to `SKILL_PATTERNS` and `SKILL_ACTION_RULES` dicts. Added `_extract_release_name()` static method. Updated `_extract_params()` to extract release names and preserve original case. |
| `agent/skills/release_notes/worker.py` | `_summarize_release()` now validates the release exists via `_get_version_info()` before querying issues. Returns `not_started=True` with a clear message for future releases with no work or only backlog items. Migrated `_jira_search()` from deprecated `/rest/api/3/search` to `/rest/api/3/search/jql` with `nextPageToken` pagination. |
| `adapters/local/env_secrets.py` | Added `"project_key": "JIRA_PROJECT_KEY"` to the Jira integration key mapping so the `.env` value gets passed to skill workers. |
| `tests/test_release_notes.py` | New test file with 22 tests covering worker behavior, routing, parameter extraction, future release handling, search endpoint migration, pagination, and skill registration. |

## Architecture & Design Decisions

The root cause of all five reported issues was a single gap: `SKILL_PATTERNS` and `SKILL_ACTION_RULES` in `rule_router.py` had no entries for the `release_notes` skill. This meant every release-related message bypassed Tier 1 (rule-based routing) and went straight to Tier 2 (full Claude with tools). Claude would then sometimes fabricate release data instead of invoking the skill, and `--raw` mode was unreliable because it depends on Tier 1 matching.

**Release name extraction** uses a priority order: quoted strings first (preserves names like "Nova 3.0"), then version patterns (v1.2.3), then named patterns ("release X"). The original-case text is passed through for extraction to preserve proper casing.

**Future release detection** works in two stages: (1) if the release has zero issues, return `not_started=True`, (2) if issues exist but none have a "work" status (In Progress, Done, etc.), also return `not_started=True`. This distinguishes planned-but-unstarted releases from active ones.

**Negative lookahead** on the `release` pattern (`(?!\s*(status|ready|readiness)\b)`) prevents stealing "release status" messages from `sprint_status`, which handles delivery/readiness context.

## Current State

- **What works:** Routing to `release_notes` for list/summarize actions, `--raw` mode, version extraction from messages (quoted names like `"12.0 Lynx"` and version patterns like `v2.5.0`), future release detection, Jira API v3 search/jql with token pagination, project_key mapping from `.env` — all 22 tests pass
- **What doesn't yet:** No way for users to specify a different project from the chat message itself (project_key is always read from `.env`/secrets)
- **Known issues:** The Atlassian `nextPageToken` pagination has known reliability issues in some Jira Cloud instances (tokens occasionally loop). Current code has a guard (`not issues` check) to prevent infinite loops, but worth monitoring.

## How to Pick Up From Here

- Consider adding project name extraction from messages (e.g., "release notes for Nova v2.0" → project_key=NOVA, release_name=v2.0)
- The Jira project is Nova (key: `NV`) — configured via `JIRA_PROJECT_KEY=NV` in `.env`
- The test file at `tests/test_release_notes.py` can't run with pytest in the current environment (pip install blocked by proxy) — verify it runs in the real CI/dev environment
- The worktree at `.claude/worktrees/laughing-gagarin/` has an older version of the test file — it can be cleaned up
- The AWS secrets adapter (`adapters/aws/secrets_manager.py`) stores arbitrary JSON so it doesn't need a key mapping like `env_secrets.py`, but verify that `project_key` is included when seeding secrets via `scripts/seed.sh`

## Dependencies & Gotchas

- `_extract_params` now takes an optional 4th argument `text_original` to preserve case for release name extraction. This is backward-compatible (defaults to empty string, falls back to text_lower).
- The `_get_version_info()` call is now made early in `_summarize_release()` and its result is reused (previously it was called redundantly at the end).
- The `not_started` key in worker output is new — any formatting prompts or Claude instructions that handle release_notes output should be aware of this field.
- **Jira API migration**: `_jira_search()` now uses `/rest/api/3/search/jql` with `nextPageToken` pagination. The old `/rest/api/3/search` with `startAt` was removed by Atlassian on 2025-08-01. The sprint_status skill is unaffected — it uses the Agile API (`/rest/agile/1.0/`) which has separate endpoints.
- The `JIRA_PROJECT_KEY` env var must be set in `.env` for release queries to work. This was already present in the `.env` file but wasn't being mapped through to workers.
