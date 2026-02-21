# T3nets — Local Development Guide

**Last Updated:** February 21, 2026

---

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/WildEllie/t3nets.git
cd t3nets

# 2. Set up environment
cp .env.example .env
# Edit .env with your Anthropic API key and Jira credentials

# 3. Run the dev server
python -m adapters.local.dev_server

# 4. Open browser
# Chat:   http://localhost:8080/chat.html
# Health: http://localhost:8080/health.html
```

---

## Prerequisites

- Python 3.12+
- An Anthropic API key (get one at https://console.anthropic.com)
- Jira credentials (for sprint_status skill)

### .env File

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-api03-...

# Jira integration
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=your-email@company.com
JIRA_API_TOKEN=your-jira-api-token
JIRA_BOARD_ID=123
```

---

## Architecture (Local Stack)

```
Browser (chat.html) → HTTP Server (port 8080) → Hybrid Router
                                                      ↓
                                            ┌─────────┼─────────┐
                                            ↓         ↓         ↓
                                     Conversational  Rule-Based  AI Routing
                                     (Claude direct) (Skill→Claude) (Claude+tools)
                                            ↓         ↓         ↓
                                         Anthropic API          Jira API
                                            ↓
                                     SQLite (conversations)
```

### Local Adapters

| Interface | Local Implementation | What It Does |
|-----------|---------------------|--------------|
| AIProvider | `anthropic_provider.py` | Direct Anthropic API (not Bedrock) |
| ConversationStore | `sqlite_store.py` | SQLite file for conversation history |
| EventBus | `direct_bus.py` | Synchronous function calls (no queue) |
| SecretsProvider | `env_secrets.py` | Reads from `.env` file |
| TenantStore | (in-memory) | Hardcoded default tenant for dev |

---

## Hybrid Routing

The dev server uses a three-tier routing strategy to minimize API costs and latency:

### Tier 1: Conversational (No API Call)
Patterns like "hi", "thanks", "good morning" get canned responses without touching Claude at all.

**Detected by:** Regex patterns in the rule engine
**Cost:** $0.00

### Tier 2: Rule-Matched (1 API Call)
When the user's intent clearly matches a skill (e.g., "sprint status", "what's blocked"), the system:
1. Calls the skill directly (no Claude needed to decide)
2. Passes raw skill output to Claude for formatting only
3. Returns formatted response

**Detected by:** Keyword triggers defined in `skill.yaml`
**Cost:** ~$0.01 (one Claude call for formatting)

### Tier 3: AI Routing (2 API Calls)
For ambiguous messages, Claude decides which tool to use:
1. Claude receives message + tool definitions → picks a tool
2. Skill executes → raw data returned
3. Claude formats the result

**Detected by:** Everything that doesn't match Tier 1 or 2
**Cost:** ~$0.02 (two Claude calls)

---

## Debug Mode (--raw)

Append `--raw` to any message to skip Claude formatting and see raw skill output:

```
You: sprint status --raw
Bot: {"sprint_name": "NOVA Sprint 12E4", "total_issues": 29, ...}
```

Useful for:
- Debugging skill output
- Verifying Jira API responses
- Testing without burning Claude tokens

---

## Dashboard Pages

### Chat (`/chat.html`)
- Full chat interface with message history
- Hybrid routing indicator shows which tier handled each message
- `--raw` debug mode support

### Health (`/health.html`)
- Live system status (AI provider, skills, conversation store)
- API key validation (shows masked key)
- Jira connection test
- Conversation count
- Last message timestamp

### Navigation
Shared nav bar across all pages. Links to Chat, Health, and (future) Settings.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/chat.html` | Chat UI |
| GET | `/health.html` | Health dashboard |
| GET | `/api/health` | Health JSON (for monitoring) |
| POST | `/api/chat` | Send a message, get a response |

### POST /api/chat

```json
// Request
{"message": "what's the sprint status?"}

// Response
{
  "response": "Here's your current sprint status...",
  "routing": "rule_matched",
  "skill": "sprint_status",
  "timing_ms": 1234
}
```

---

## Project Structure (Local Relevant)

```
t3nets/
├── agent/                     # Portable application code (no cloud imports)
│   ├── router/
│   │   ├── rule_router.py     # Hybrid routing (Tier 1–3, RuleBasedRouter)
│   │   └── router.py          # Full Router (Claude-only path)
│   ├── skills/
│   │   ├── registry.py        # Skill loader + tool builder
│   │   └── sprint_status/
│   │       ├── skill.yaml     # Skill metadata + triggers
│   │       └── worker.py      # Jira API calls
│   ├── interfaces/            # Abstract base classes
│   └── models/                # Tenant, Message, Context dataclasses
│
├── adapters/local/            # Local implementations
│   ├── dev_server.py          # HTTP server (the entry point)
│   ├── anthropic_provider.py  # AIProvider → Anthropic API
│   ├── sqlite_store.py        # ConversationStore → SQLite
│   ├── direct_bus.py          # EventBus → sync function calls
│   ├── env_secrets.py         # SecretsProvider → .env file
│   ├── chat.html              # Chat UI
│   └── health.html            # Health dashboard
│
├── .env                       # Your local credentials
└── .env.example               # Template
```

---

## Common Issues

### "ANTHROPIC_API_KEY not set"
Make sure `.env` exists in the project root with your API key.

### "Jira connection failed"
Check your Jira credentials in `.env`. The board ID must be numeric. Test with:
```bash
curl -u your-email:your-api-token https://yourcompany.atlassian.net/rest/agile/1.0/board/123/sprint?state=active
```

### "Module not found" errors
Run from the project root:
```bash
python -m adapters.local.dev_server
```
Not from inside the `adapters/local/` directory.

### SQLite database location
Conversation history is stored in `t3nets_conversations.db` in the project root. Delete it to reset conversation history.
