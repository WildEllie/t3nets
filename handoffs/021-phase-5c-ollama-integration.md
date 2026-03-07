# Handoff: Phase 5c — Ollama Integration (Free AI Models)

**Date:** 2026-03-07
**Status:** Complete
**Roadmap item:** Phase 5c — Ollama Integration (Free AI Models)
**PR:** #9 (merged)

---

## What Was Done

Added Ollama as a third AI provider alongside Anthropic (direct API) and AWS Bedrock. Ollama runs as a separate container/process and exposes an OpenAI-compatible API. This enables zero-cost local development without an API key and free model selection for any tenant.

---

## Files Created

| File | Purpose |
|------|---------|
| `adapters/ollama/__init__.py` | Package init |
| `adapters/ollama/provider.py` | `OllamaProvider` implementing `AIProvider` via OpenAI-compatible `/v1/chat/completions` endpoint |
| `tests/test_ollama_provider.py` | 12 unit tests: response parsing, tool call mapping, message format conversion, connection errors |
| `docs/plan-ollama-integration.md` | Architecture plan with diagrams, model table, trade-offs |

## Files Modified

| File | Change |
|------|--------|
| `agent/models/ai_models.py` | Added `ollama_id` field to `AIModel`, 4 Ollama models (Llama 3.2 3B, Llama 3.1 8B, Mistral 7B, Qwen 2.5 7B), `get_model_for_provider()` handles `"ollama"` |
| `agent/models/tenant.py` | Added `tier1_formatting_model` field to `TenantSettings` |
| `adapters/local/dev_server.py` | Provider selection via `OLLAMA_API_URL` env var, smart model defaults per provider |
| `adapters/aws/server.py` | Same Ollama provider support for AWS (ECS sidecar) |
| `adapters/local/settings.html` | "Free" and "Cloud only" badge styles for model selector |
| `docker-compose.yml` | Ollama sidecar service with `ollama` profile and persistent volume |
| `docs/ROADMAP.md` | Phase 5c added with tasks + milestone |
| `agent/models/__init__.py` | Fixed import sorting (pre-existing) |
| `agent/models/context.py` | Fixed import sorting and line length (pre-existing) |

---

## How It Works

### Provider Selection

At startup, the server checks environment variables to decide which provider to use:

```
OLLAMA_API_URL set       → OllamaProvider (free, local)
ANTHROPIC_API_KEY set    → AnthropicProvider (paid, direct API)
BEDROCK_MODEL_ID set     → BedrockProvider (paid, AWS)
Nothing set              → Error with helpful message
```

The `PROVIDER` constant (`"ollama"`, `"anthropic"`, or `"bedrock"`) controls model registry filtering and settings UI display.

### OllamaProvider

Located at `adapters/ollama/provider.py`. Implements `AIProvider.chat()` and `chat_with_tool_result()` by:

1. Converting T3nets/Anthropic-style messages to OpenAI format
2. POSTing to `{base_url}/v1/chat/completions` with tool definitions
3. Parsing OpenAI-format responses back to `AIResponse`/`ToolCall`

Uses `urllib.request` only — no new dependencies.

### Model Registry

Four Ollama models added to `AVAILABLE_MODELS` with `providers=["ollama"]`. The `ollama_id` field stores the Ollama model tag (e.g., `"llama3.1:8b"`). Any model the user has pulled via `ollama pull` works — these are the ones shown in the UI.

### Docker Compose

Ollama runs as an optional sidecar via Docker profiles:

```bash
# With Ollama (free, no API key):
docker compose --profile ollama up

# With Anthropic (needs ANTHROPIC_API_KEY in .env):
docker compose up
```

---

## How to Test

```bash
# 1. Start Ollama and pull a model
ollama serve
ollama pull llama3.1:8b

# 2. Run dev server with Ollama
OLLAMA_API_URL=http://localhost:11434 python -m adapters.local.dev_server

# 3. Open http://localhost:8080 — chat should work with Llama
# 4. Check settings page — Ollama models should show with "Free" badge
```

---

## Future Work

- **Tier 1 formatting optimization**: Use `tier1_formatting_model` (a small free model) to format rule-matched skill results instead of the primary AI — field is in `TenantSettings` but not yet wired into routing logic
- **ECS sidecar Terraform**: Add Ollama container to ECS task definition for AWS deployments
- **Model auto-detection**: Query `ollama list` to show only locally-available models in settings UI
