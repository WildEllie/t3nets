# AI-Generated Rule Engine for T3nets Routing

## Context

The current regex-based router works at $0 per call — the right cost target. But maintaining 170+ hand-written patterns per skill doesn't scale when the platform is designed for an ever-growing skill catalog with per-tenant enable/disable. The solution: **keep the $0 regex routing, but have AI generate and maintain the rules**.

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│                    SKILL CONFIGURATION                       │
│  Each skill defines: name, description, trigger words/phrases│
│  Stored in DynamoDB alongside skill metadata                 │
│                                                              │
│  When tenant enables/disables skills:                        │
│    → Rule Engine Builder (AI service) is invoked             │
│    → Generates optimized regex rules for that tenant's       │
│      specific combination of enabled skills                  │
│    → Rules saved to DynamoDB, loaded into memory             │
└──────────────────┬───────────────────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────────────────┐
│                    REQUEST FLOW                              │
│                                                              │
│  Message arrives                                             │
│    │                                                         │
│    ▼                                                         │
│  Tier 1: AI-generated regex engine (in-memory)               │
│    Match? ──yes──▶ Execute skill worker ($0, <1ms)           │
│    │                                                         │
│    ▼ no match                                                │
│  Tier 2: AI reasoning over enabled + disabled skills         │
│    ├─ Enabled skill matched ──▶ Execute skill ($0.01-0.05)   │
│    ├─ Disabled skill matched ──▶ "This skill is unavailable, │
│    │                              contact your admin"        │
│    └─ No skill is the clear choice ──▶ Freeform chat         │
│         AI responds conversationally with skill awareness    │
│         (e.g. "What time is it in Malta" → direct answer)    │
│                                                              │
│  Tier 2 misses (no skill matched) are logged as training     │
│  data for future rule improvement                            │
└──────────────────────────────────────────────────────────────┘
```

## Detailed Design

### 1. Skill Trigger Storage (DynamoDB)

Each skill stores its trigger words/phrases as part of its metadata. These are the **raw ingredients** the AI rule builder uses — not the compiled regex patterns themselves.

```
PK: SKILL#sprint_status
SK: META
{
  "name": "sprint_status",
  "description": "Get current sprint status, blockers, progress...",
  "triggers": ["sprint status", "are we on track", "what's blocked", ...],
  "actions": ["status", "blockers", "mine"],
  "action_descriptions": {
    "status": "Full sprint overview with progress and risk",
    "blockers": "Only blocked/flagged items",
    "mine": "Tickets assigned to a specific person"
  },
  "parameters": { ... JSON Schema ... },
  "requires_integration": "jira"
}
```

**Key files:**
- `agent/interfaces/skill_store.py` (new) — abstract interface for skill metadata CRUD
- `adapters/local/sqlite_skill_store.py` (new) — local dev implementation
- `adapters/aws/dynamo_skill_store.py` (new) — DynamoDB implementation

### 2. Rule Engine Builder Service

An AI-powered service that generates optimized regex rule sets for a specific combination of enabled skills. Invoked when:
- A tenant enables/disables a skill
- An admin triggers recalculation after adding training data
- A new skill is added to the platform

**How it works:**

```python
class RuleEngineBuilder:
    """Uses AI to generate regex rules from skill definitions."""

    async def build_rules(
        self,
        enabled_skills: list[SkillDefinition],
        disabled_skills: list[SkillDefinition],  # for awareness
        training_data: list[TrainingExample],      # admin-curated misses
        ai: AIProvider,
        model: str,
    ) -> TenantRuleSet:
        """
        Prompt AI with all skill metadata + training examples.
        Returns a complete rule set: detection patterns + action rules.
        """
```

The AI receives:
- Each enabled skill's name, description, triggers, actions, action descriptions
- Each disabled skill's name and description (so it can generate "catch" patterns)
- Training data: previous unmatched messages that admins have mapped to skills
- Instructions to generate non-overlapping regex patterns that disambiguate between skills

The AI returns a structured `TenantRuleSet`:
```python
@dataclass
class TenantRuleSet:
    tenant_id: str
    version: int
    generated_at: str  # ISO timestamp
    skill_rules: dict[str, SkillRules]  # per-skill detection + action patterns
    disabled_skill_catchers: dict[str, list[str]]  # patterns that catch disabled skill requests
```

Each `SkillRules` contains:
```python
@dataclass
class SkillRules:
    detection_patterns: list[str]          # regex patterns to detect this skill
    action_rules: list[tuple[str, str]]    # (regex_pattern, action_name)
    disambiguation_notes: str              # AI's reasoning for pattern choices
```

**Key advantage over hand-maintained regex:** The AI understands the **full combination** of enabled skills and generates patterns that minimize cross-skill confusion. When `sprint_status` and `release_notes` are both enabled, the AI knows "release status" is ambiguous and generates patterns that disambiguate. When `release_notes` is disabled, it can relax the `sprint_status` patterns to catch more broadly.

**Key files:**
- `agent/router/rule_engine_builder.py` (new) — AI-powered rule generation
- `agent/router/models.py` (new) — `TenantRuleSet`, `SkillRules`, `TrainingExample` dataclasses

### 3. Tenant Rule Set Storage and Loading

Generated rule sets are stored per-tenant in DynamoDB:

```
PK: TENANT#acme
SK: RULE_ENGINE
{
  "version": 3,
  "generated_at": "2026-03-03T10:00:00Z",
  "rules": { ... serialized TenantRuleSet ... },
  "generation_model": "claude-sonnet-4-5",
  "generation_prompt_hash": "abc123"  # for cache invalidation
}
```

On server startup and after regeneration, the rule set is loaded into memory and compiled:

```python
class CompiledRuleEngine:
    """In-memory compiled regex engine for a specific tenant."""

    def __init__(self, rule_set: TenantRuleSet):
        self._compiled = {}  # skill_name → compiled patterns
        self._compile(rule_set)

    def match(self, text: str) -> Optional[RouteMatch]:
        """Same interface as current RuleBasedRouter.match()"""
        ...

    def check_disabled_skill(self, text: str) -> Optional[str]:
        """Check if message targets a disabled skill. Returns skill name or None."""
        ...
```

### 4. Tier 2: AI Reasoning Fallback

When the compiled rules don't match, the request goes to an AI model (Sonnet) with enhanced context:

**System prompt includes:**
- List of enabled skills with descriptions (as tools via function calling)
- List of disabled skills with descriptions (as context, NOT as tools)
- Instruction: if the user clearly wants a disabled skill, say so; if no skill fits, respond conversationally

**The AI can:**
- Select and call an enabled skill tool → execute the skill
- Identify a disabled skill → respond: "The [skill name] feature isn't enabled for your workspace. Contact your admin to enable it."
- Determine no skill is relevant → respond as a helpful assistant (answer general questions, have a conversation)

This is the existing `Router.handle_message()` flow with two additions:
1. The system prompt mentions disabled skills
2. The response includes "no_skill_match" logging

**Key changes to:** `agent/router/router.py`

### 5. Training Data Collection

Every Tier 2 interaction where the AI selects a skill (or no skill) is logged:

```python
@dataclass
class TrainingExample:
    tenant_id: str
    message_text: str
    timestamp: str
    matched_skill: Optional[str]    # what AI chose (or None)
    matched_action: Optional[str]
    was_disabled_skill: bool
    confidence: Optional[float]
    admin_override_skill: Optional[str]   # set later by admin
    admin_override_action: Optional[str]  # set later by admin
```

Storage:
```
PK: TENANT#acme
SK: TRAINING#2026-03-03T10:15:00Z#<uuid>
{
  "message_text": "show me my pull requests",
  "matched_skill": null,
  "was_disabled_skill": false,
  ...
}
```

**Key files:**
- `agent/interfaces/training_store.py` (new) — abstract interface
- Adapter implementations for local/aws

### 6. Admin Dashboard Integration (Future Phase)

A new dashboard section where admins can:
- View unmatched messages (training data)
- Map messages to skills: "this message should trigger `sprint_status.mine`"
- Trigger rule engine recalculation
- View current rule set with AI-generated disambiguation notes
- See rule engine performance metrics (hit rate, false positive rate)

**This is a future phase** — the core routing works without it. Training data accumulates passively.

---

## Implementation Plan

### Phase 5a: Core Rule Engine

1. **Data models** — `TenantRuleSet`, `SkillRules`, `TrainingExample` dataclasses
2. **Rule Engine Builder** — AI service that generates rules from skill metadata
3. **Compiled Rule Engine** — in-memory compiled regex with `match()` and `check_disabled_skill()`
4. **Router integration** — wire compiled engine into `router.py` as Tier 1, with existing Claude call as Tier 2
5. **Rule persistence** — store/load tenant rule sets (SQLite for local, DynamoDB for AWS)
6. **Regeneration trigger** — rebuild rules when tenant skill config changes
7. **Training data logging** — save Tier 2 misses for future use

### Phase 5b: Admin Training Tools

8. **Training data API endpoints** — list, annotate, delete training examples
9. **Rule recalculation endpoint** — admin triggers rebuild with training data
10. **Dashboard UI** — training data viewer, skill mapping, recalculate button

---

## Key Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `agent/router/models.py` | Create | TenantRuleSet, SkillRules, TrainingExample |
| `agent/router/rule_engine_builder.py` | Create | AI-powered rule generation service |
| `agent/router/compiled_engine.py` | Create | In-memory compiled regex engine |
| `agent/router/router.py` | Modify | Wire in compiled engine as Tier 1, add disabled-skill awareness to Tier 2 |
| `agent/router/rule_router.py` | Remove/deprecate | Replaced by compiled_engine.py |
| `agent/interfaces/rule_store.py` | Create | Abstract interface for rule set persistence |
| `agent/interfaces/training_store.py` | Create | Abstract interface for training data |
| `adapters/local/sqlite_rule_store.py` | Create | Local dev rule storage |
| `adapters/local/sqlite_training_store.py` | Create | Local dev training storage |
| `adapters/aws/dynamo_rule_store.py` | Create | DynamoDB rule storage |
| `adapters/aws/dynamo_training_store.py` | Create | DynamoDB training storage |
| `agent/skills/registry.py` | Modify | Expose skill metadata for rule builder |
| Skill `skill.yaml` files | Modify | Add `action_descriptions` field |

---

## Verification

1. **Unit tests**: rule engine builder generates valid regex; compiled engine matches known inputs
2. **Integration test**: enable/disable skills → rule engine regenerates → new patterns work
3. **Manual test**: run local dev server, send messages, verify Tier 1 handles known patterns and Tier 2 handles unknowns
4. **Cost validation**: check logs to confirm Tier 1 handles majority of skill-routable messages at $0
