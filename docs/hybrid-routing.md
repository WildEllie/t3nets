# T3nets — Hybrid Routing Architecture

**Last Updated:** February 21, 2026

---

## Why Hybrid Routing?

Sending every message through Claude with tool definitions costs ~$0.02-0.05 per message. For a team sending 100 messages/day, that's $60-150/month just in AI costs. Most of those messages are things like "hi", "thanks", or "sprint status" — they don't need Claude to decide what to do.

Hybrid routing cuts AI costs by 50-60% by handling the obvious cases locally.

---

## The Three Tiers

### Tier 1: Conversational (Zero API Cost)

**What it catches:** Greetings, thanks, small talk, help requests

**How it works:** Regex patterns match against the message. If matched, return a canned response immediately.

```python
CONVERSATIONAL_PATTERNS = [
    (r'^(hi|hello|hey|howdy)\b', "Hey! 👋 How can I help?"),
    (r'^(thanks|thank you|thx)', "You're welcome! Need anything else?"),
    (r'^(good morning|good afternoon|good evening)', "Good {time_of_day}! What can I help with?"),
    (r'^(help|what can you do)', HELP_TEXT),
    (r'^(bye|goodbye|see you)', "See you! 👋"),
]
```

**Cost:** $0.00 per message
**Latency:** <1ms

### Tier 2: Rule-Matched (One API Call)

**What it catches:** Messages that clearly map to a known skill

**How it works:**
1. Check message against skill trigger keywords (from `skill.yaml`)
2. If matched, execute the skill directly (no Claude decision needed)
3. Pass raw skill output to Claude for human-friendly formatting
4. Return formatted response

```yaml
# skill.yaml triggers
triggers:
  - "sprint status"
  - "are we on track"
  - "what's blocked"
  - "release status"
```

**Cost:** ~$0.01 per message (one Claude call for formatting only)
**Latency:** ~1-2s (skill execution + Claude formatting)

### Tier 3: AI Routing (Two API Calls)

**What it catches:** Everything else — ambiguous, multi-intent, or complex messages

**How it works:**
1. Send message to Claude with all available tool definitions
2. Claude decides: respond directly OR call a tool
3. If tool call: execute skill, then send result back to Claude for formatting
4. Return Claude's response

**Cost:** ~$0.02-0.05 per message (two Claude calls)
**Latency:** ~2-4s (Claude routing + skill + Claude formatting)

---

## Routing Flow

```
Message arrives
    │
    ▼
[Conversational check] ──match──▶ Return canned response
    │ no match
    ▼
[Rule engine check] ──match──▶ Execute skill → Claude formats → Return
    │ no match
    ▼
[Claude with tools] ──tool_use──▶ Execute skill → Claude formats → Return
    │ no tool
    ▼
Return Claude's direct response
```

---

## Rule Engine Details

The rule engine checks messages against skill triggers. Each skill defines trigger phrases in its `skill.yaml`:

```yaml
name: sprint_status
triggers:
  - "sprint status"
  - "sprint"
  - "are we on track"
  - "what's blocked"
  - "blockers"
  - "release status"
  - "what's the status"
```

**Matching logic:**
- Case-insensitive substring match
- Ordered by specificity (longer triggers checked first)
- First match wins
- `--raw` flag detected and stripped before matching

### Adding Triggers for New Skills

When creating a new skill, define triggers that cover common phrasings:

```yaml
# Good triggers (specific, unambiguous)
triggers:
  - "sprint status"
  - "what's blocked"
  - "show blockers"

# Bad triggers (too broad, will false-positive)
triggers:
  - "status"      # matches "email status", "server status"
  - "show"        # matches everything
  - "what"        # way too broad
```

---

## Debug Mode (--raw)

Append `--raw` to any message to bypass Claude formatting:

```
User: sprint status --raw
Bot: {"sprint_name": "NOVA Sprint 12E4", "total_issues": 29, "completed": 12, ...}

User: sprint status
Bot: 🏃 **Sprint NOVA 12E4**
     Progress: 41% complete (12/29 issues)
     Days remaining: 5
     Risk: ⚠️ HIGH — 3 blockers identified
```

`--raw` works with both rule-matched (Tier 2) and AI-routed (Tier 3) messages.

---

## Response Metadata

Every chat response includes routing metadata (visible in the API response, shown in chat UI):

```json
{
  "response": "Here's your sprint status...",
  "routing": "rule_matched",
  "skill": "sprint_status",
  "timing_ms": 1450
}
```

| `routing` value | Meaning |
|----------------|---------|
| `conversational` | Matched a conversational pattern (Tier 1) |
| `rule_matched` | Matched a skill trigger (Tier 2) |
| `ai_routed` | Claude decided to use a tool (Tier 3) |
| `ai_direct` | Claude responded without tools (Tier 3) |

---

## Cost Comparison

Assuming 100 messages/day, 30 days:

| Strategy | Monthly Cost | Notes |
|----------|-------------|-------|
| All through Claude (no routing) | ~$90-150 | Every message gets tools |
| Hybrid (current) | ~$30-60 | ~40% conversational, ~30% rule-matched, ~30% AI |
| Hybrid + Nova for formatting | ~$15-30 | Use Nova Micro for Tier 1, Nova Pro for Tier 2 formatting |

---

## Future Enhancements

### Per-Tier Model Selection
Different AI models for different tiers:
- Tier 1: Amazon Nova Micro (cheapest) or no model at all
- Tier 2 (formatting): Amazon Nova Pro or Claude Haiku
- Tier 3 (tool use): Claude Sonnet (best tool use quality)

### Learning from Routing Decisions
Log which tier handled each message. Over time, identify:
- Messages that hit Tier 3 but could be rule-matched (add triggers)
- Messages that hit Tier 2 but got wrong skill (fix triggers)
- Popular patterns that should become new conversational responses

### Bedrock Intelligent Prompt Routing
AWS offers automatic routing between models in the same family (e.g., Nova Lite ↔ Nova Pro). Could replace custom tier logic for the AI provider call.
