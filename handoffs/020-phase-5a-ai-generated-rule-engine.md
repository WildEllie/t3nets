# Handoff: Phase 5a — AI-Generated Rule Engine

**Date:** 2026-03-05
**Status:** Complete
**Roadmap item:** Phase 5a — Core Rule Engine
**Commit:** `ee6ccb7`

---

## What Was Done

Replaced the hand-maintained `RuleBasedRouter` (170+ regex patterns per skill, written by hand) with an AI-generated, per-tenant rule system. The rules are built once by Claude when a tenant's skill configuration changes, compiled into memory, and evaluated in microseconds at request time with zero AI cost.

The three-tier routing model now looks like this:

```
Message arrives
  │
  ▼
Conversational check (greetings, thanks, etc.) → Claude freeform
  │ no match
  ▼
Tier 1: CompiledRuleEngine (in-memory regex, $0, <1ms)
  ├─ Match? → Execute skill worker directly
  ├─ Disabled skill catcher? → "This skill isn't enabled — contact your admin"
  │
  ▼ no match
Tier 2: Claude with tools (enabled skills as tools, disabled skills in context)
  ├─ AI calls a skill tool → Execute skill ($0.01–0.05)
  ├─ AI identifies disabled skill → Inform user
  └─ AI responds freeform → conversational answer, no skill
       └─ Log as TrainingExample (for future rule improvement)
```

---

## Files Created

| File | Purpose |
|------|---------|
| `agent/router/models.py` | `TenantRuleSet`, `SkillRules`, `TrainingExample` dataclasses |
| `agent/router/rule_engine_builder.py` | `RuleEngineBuilder` — calls Claude with a structured `submit_rule_set` tool to generate regex per skill |
| `agent/router/compiled_engine.py` | `CompiledRuleEngine` — in-memory compiled regex; `match()`, `check_disabled_skill()`, `strip_raw_flag()`, `is_conversational()` |
| `agent/interfaces/rule_store.py` | Abstract interface: `save_rule_set()`, `load_rule_set()` |
| `agent/interfaces/training_store.py` | Abstract interface: `log_example()`, `list_examples()`, `annotate_example()`, `delete_example()` |
| `adapters/local/sqlite_rule_store.py` | SQLite rule store — stores serialised `TenantRuleSet` JSON per tenant |
| `adapters/local/sqlite_training_store.py` | SQLite training store — rows keyed by `(tenant_id, example_id)` |
| `adapters/aws/dynamo_rule_store.py` | DynamoDB rule store — `PK=TENANT#<id>`, `SK=RULE_ENGINE` |
| `adapters/aws/dynamo_training_store.py` | DynamoDB training store — `PK=TENANT#<id>`, `SK=TRAINING#<timestamp>#<uuid>` |
| `tests/test_compiled_engine.py` | 31 unit tests for engine matching, action resolution, `--raw` flag, disabled-skill detection, `is_conversational()` |

## Files Modified

| File | Change |
|------|--------|
| `agent/skills/registry.py` | Added `action_descriptions: dict[str, str]` to `SkillDefinition`; loaded from `skill.yaml` |
| `agent/skills/sprint_status/skill.yaml` | Added `action_descriptions` (status, blockers, mine) and `triggers` list |
| `agent/skills/release_notes/skill.yaml` | Added `action_descriptions` (summarize, list, latest) and `triggers` list |
| `adapters/local/dev_server.py` | Full routing rewrite: lazy engine cache, `_get_engine()`, auto-rebuild on skill toggle, training data logging, rule inspection endpoint |
| `adapters/aws/server.py` | Same routing rewrite for AWS; DynamoDB stores wired at startup |
| `docs/ROADMAP.md` | Phase 5a marked complete; Phase 5b tasks updated |

---

## Key Design Decisions

### Rule generation via Claude tool call

`RuleEngineBuilder.build_rules()` sends Claude a prompt listing all enabled and disabled skills (name, description, triggers, action descriptions) and forces a structured `submit_rule_set` tool call. This guarantees a machine-parseable response rather than free text, and lets the schema enforce correct types (array of regex strings, array of `{pattern, action}` objects, etc.).

Claude receives disabled skills as context only — it generates "catcher" patterns for them so the engine can respond "this isn't enabled" without a full Tier 2 AI call.

### Lazy, per-tenant engine cache

Both servers maintain a module-level `_compiled_engines: dict[str, CompiledRuleEngine]`. On first message for a tenant, `_get_engine()` loads the stored rule set, compiles it, and caches it. Subsequent messages for the same tenant hit the cache directly. Cache is invalidated (and rules rebuild) when the tenant toggles a skill.

```python
async def _get_engine(tenant_id: str) -> CompiledRuleEngine | None:
    if tenant_id in _compiled_engines:
        return _compiled_engines[tenant_id]
    # load from store, or build via RuleEngineBuilder, compile, cache
```

On server startup, all tenants with cached rule sets are pre-loaded into memory.

### Scoring match (not first-match)

The engine counts how many detection patterns fire for each enabled skill and picks the skill with the most hits. This is more robust than first-match when two skills share common words (e.g. "status" appears in both `sprint_status` and `release_notes`). First-match remains for action selection within a skill (most specific rule first).

### Disabled skill detection between Tier 1 and Tier 2

After `match()` returns `None`, `check_disabled_skill()` scans the disabled-skill catcher patterns. A hit skips the Claude call entirely and returns a templated message:

```
The 'sprint_status' skill isn't currently enabled for your workspace.
Contact your workspace admin to enable it.
```

This avoids a ~$0.02 Claude call just to tell a user something is turned off.

### Training data logging

Every Tier 2 routing decision is saved as a `TrainingExample`. The `matched_skill` field is `None` when Claude responded freeform (no tool call). This is the raw material for Phase 5b: admins annotate these in the dashboard and trigger a rule rebuild that incorporates them.

---

## API Endpoints (local dev server)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/admin/training-data?limit=N` | List recent training examples for the default tenant |
| `POST` | `/api/admin/training-data/{id}/annotate` | Set `admin_override_skill` + `admin_override_action` on an example |
| `DELETE` | `/api/admin/training-data/{id}` | Remove a training example |
| `POST` | `/api/admin/rule-engine/rebuild` | Trigger rule set rebuild for the default tenant (uses training data) |
| `GET` | `/api/admin/rule-engine/inspect` | Return the current rule set JSON for debugging |

AWS server exposes the same endpoints via `/api/admin/tenants/{id}/...` (auth-gated to admins).

---

## Storage Schemas

### DynamoDB — Rule set

```
PK: TENANT#<tenant_id>
SK: RULE_ENGINE
{
  "version": 3,
  "generated_at": "2026-03-05T10:00:00Z",
  "generation_model": "claude-sonnet-4-5",
  "rules": { ...serialised TenantRuleSet JSON... }
}
```

### DynamoDB — Training examples

```
PK: TENANT#<tenant_id>
SK: TRAINING#2026-03-05T10:15:00Z#<uuid>
{
  "example_id": "<uuid>",
  "message_text": "show me my pull requests",
  "matched_skill": null,
  "matched_action": null,
  "was_disabled_skill": false,
  "timestamp": "2026-03-05T10:15:00Z"
}
```

---

## Tests

`tests/test_compiled_engine.py` — 31 tests, all green. Covers:

- `match()` on known sprint and release patterns
- Action selection (blockers vs mine vs status)
- No false match on unrelated messages
- `check_disabled_skill()` returns skill name when catcher fires
- `strip_raw_flag()` — strips `--raw` and returns flag state
- `is_conversational()` — greetings, thanks, etc.
- Assignee email extraction for `mine` action
- Release name extraction (quoted, semver, "release X" form)

Run with: `pytest tests/test_compiled_engine.py`

---

## Deployment Notes

No Terraform changes required for Phase 5a. Both the SQLite and DynamoDB stores write to existing infrastructure:

- **Local:** SQLite (`data/t3nets.db`) — two new tables created on startup via `CREATE TABLE IF NOT EXISTS`
- **AWS:** DynamoDB tenants table (`t3nets-dev-tenants`) — new item type under existing `TENANT#<id>` partition key; training examples also in the same table with `TRAINING#` sort key prefix

Deploy normally with `./scripts/deploy.sh`.

The rule engine is triggered automatically on first use — no seed step needed. First message for a tenant will build and cache rules. Subsequent messages are fully cached.

---

## What's Next

**Phase 5b (Training Data Admin UI)** — already implemented in commit `3a7b728`:

- `adapters/local/training.html` — training data viewer
- Admin can map unmatched messages to skills, trigger rule rebuild from UI
- Performance metrics (hit rate, Tier 1 vs Tier 2 breakdown)

See the Phase 5b handoff (pending) for details.

**Future improvements to consider:**

- Pre-warm rule engines in background on startup (currently lazy on first message)
- Rule set versioning — store N historical rule sets per tenant for rollback
- Cross-tenant pattern sharing for common skill combinations
- Track Tier 1 hit rate in metrics to validate cost savings
