# Ollama Integration — Free AI Models for T3nets

## Context

T3nets currently supports two AI providers — Anthropic (direct API) and AWS Bedrock. Both charge per token. This creates two pain points:

1. **Local development requires an API key** — contributors need an Anthropic API key to run the dev server
2. **Every AI call costs money** — even simple formatting of skill results goes through Claude

Adding Ollama as a third provider enables zero-cost local development with open models (Llama 3.x, Mistral, Qwen) and opens the door to per-tier model optimization where cheap tasks use free models.

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│                    PROVIDER SELECTION                         │
│                                                              │
│  Environment variable decides provider at startup:           │
│                                                              │
│  ANTHROPIC_API_KEY set    → AnthropicProvider (direct API)   │
│  BEDROCK_MODEL_ID set     → BedrockProvider (AWS Converse)   │
│  OLLAMA_API_URL set       → OllamaProvider (OpenAI-compat)   │
│  Nothing set              → OllamaProvider (localhost:11434) │
│                                                              │
│  All providers implement the same AIProvider interface:      │
│    chat() + chat_with_tool_result() → AIResponse             │
└──────────────┬───────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────┐
│                    OLLAMA DEPLOYMENT                          │
│                                                              │
│  Local dev:                                                  │
│    ollama serve          ← separate process                  │
│    python -m adapters.local.dev_server                       │
│                                                              │
│  Docker Compose:                                             │
│    services:                                                 │
│      ollama:  (image: ollama/ollama, port 11434)             │
│      router:  (OLLAMA_API_URL=http://ollama:11434)           │
│                                                              │
│  AWS ECS:                                                    │
│    Same task definition, two containers:                     │
│      router container  ← existing                            │
│      ollama sidecar    ← http://localhost:11434              │
│                                                              │
│  Ollama is always a SEPARATE container/process.              │
│  Communicates via HTTP API on port 11434.                    │
└──────────────────────────────────────────────────────────────┘
```

## OllamaProvider Design

### Interface Implementation

The provider implements `AIProvider` (defined in `agent/interfaces/ai_provider.py`) with the same two methods as AnthropicProvider and BedrockProvider:

```python
class OllamaProvider(AIProvider):
    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url

    async def chat(self, model, system, messages, tools, max_tokens=4096) -> AIResponse:
        # POST to {base_url}/v1/chat/completions (OpenAI-compatible)

    async def chat_with_tool_result(self, model, system, messages, tools,
                                     tool_use_id, tool_result, max_tokens=4096) -> AIResponse:
        # Append tool result, call chat again
```

### API Format Mapping

Ollama's `/v1/chat/completions` endpoint uses OpenAI-compatible format:

| T3nets Concept | Ollama/OpenAI Format |
|----------------|---------------------|
| `ToolDefinition` | `{"type": "function", "function": {"name": ..., "parameters": ...}}` |
| `AIResponse.tool_calls` | `choices[0].message.tool_calls` |
| `ToolCall.tool_use_id` | `tool_calls[0].id` |
| `stop_reason: "tool_use"` | `finish_reason: "tool_calls"` |
| `stop_reason: "end_turn"` | `finish_reason: "stop"` |
| Tool result message | `{"role": "tool", "tool_call_id": ..., "content": ...}` |

### No New Dependencies

Uses `urllib.request` (stdlib), same as `AnthropicProvider`. No pip packages needed.

## Model Registry

New entries in `agent/models/ai_models.py`:

| Internal ID | Ollama Model | Size | Tool Use | Best For |
|------------|-------------|------|----------|----------|
| `llama-3.2-3b` | `llama3.2:3b` | 2GB | Good | Tier 1 formatting, fast responses |
| `llama-3.1-8b` | `llama3.1:8b` | 4.7GB | Good | General purpose, balanced |
| `mistral-7b` | `mistral:7b` | 4.1GB | Strong | Tool use, structured output |
| `qwen-2.5-7b` | `qwen2.5:7b` | 4.7GB | Good | Multilingual, tool use |

These are the recommended models shown in the settings UI. The provider works with **any** model the user has pulled via `ollama pull` — the model name is passed directly to the API.

The `AIModel` dataclass gets a new `ollama_id` field, and `get_model_for_provider("ollama")` resolves it.

## Tier 1 Formatting Optimization

Currently, when a rule matches (Tier 1), the skill executes directly but Claude still formats the result. This is the most impactful place to use a free model:

```
Current Tier 1 flow:
  Rule match → Skill executes → Claude formats result ($0.01-0.02)

Optimized Tier 1 flow:
  Rule match → Skill executes → Ollama formats result ($0)
```

Formatting skill results (turning JSON into natural language) is a simple task that small models (3B) handle well. This is gated behind an optional `tier1_formatting_model` field in `TenantSettings` — off by default until quality is validated.

## Settings UI

The model selector in `settings.html` shows Ollama models with a "Free" badge when the provider is `"ollama"`. Same card-based UI, same selection flow.

## Trade-offs

### Advantages
- Zero API cost — no per-token charges
- No API key required for local development
- Data stays local — nothing leaves the machine
- Works with any OpenAI-compatible endpoint (vLLM, Groq, Together.ai)
- Clean integration — just another AIProvider implementation

### Limitations
- Tool use quality is lower than Claude — complex skill schemas may need testing
- Requires local compute (8GB+ RAM for 7B models, GPU recommended)
- Response quality degrades for complex multi-step reasoning
- Not truly free in production — self-hosting has compute costs
- Model management (pulling, updating) is user responsibility

### Recommendation
Use Ollama for:
- Local development (no API key needed)
- Tier 1 formatting (simple task, small model suffices)
- Cost-sensitive tenants with simple use cases

Keep Claude for:
- Tier 2 full routing (tool selection quality matters)
- Complex multi-tool chains
- Production deployments where quality is critical
