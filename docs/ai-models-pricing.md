# T3nets — AI Model Options & Pricing Guide

**Last Updated:** February 21, 2026

---

## Overview

T3nets supports multiple AI models via Amazon Bedrock (AWS) and the direct Anthropic API (local dev). The platform's hybrid routing architecture means different routing tiers can use different models — optimizing cost without sacrificing quality where it matters.

---

## Available Models (Bedrock, us-east-1)

### Anthropic Claude (via Bedrock)

| Model | Model ID | Input $/1K | Output $/1K | Best For |
|-------|----------|-----------|------------|----------|
| Claude Sonnet 4.5 | `anthropic.claude-sonnet-4-5-20250929-v1:0` | $0.003 | $0.015 | Tool use, complex reasoning |
| Claude Sonnet 4 | `anthropic.claude-sonnet-4-20250514-v1:0` | $0.003 | $0.015 | Tool use (cheaper fallback) |
| Claude Haiku 4.5 | `anthropic.claude-haiku-4-5-20251001-v1:0` | $0.0008 | $0.004 | Fast responses, formatting |
| Claude Opus 4.x | Various | $0.015 | $0.075 | Maximum intelligence (expensive) |

### Amazon Nova (via Bedrock)

| Model | Input $/1K | Output $/1K | Best For |
|-------|-----------|------------|----------|
| Nova Micro | $0.000035 | $0.00014 | Greetings, simple Q&A — ~100x cheaper than Sonnet |
| Nova Lite | $0.00006 | $0.00024 | Light formatting, document processing |
| Nova Pro | $0.0008 | $0.0032 | Multi-step tasks, reasoning |
| Nova Premier | $0.0025 | $0.0125 | Complex reasoning (comparable to Sonnet) |

### Amazon Nova 2 (December 2025)

| Model | Notes |
|-------|-------|
| Nova 2 Lite | Fast reasoning, 1M token context, thinking intensity levels |
| Nova 2 Pro (Preview) | Most intelligent, agentic coding, complex tasks |
| Nova 2 Sonic | Speech-to-speech, real-time conversational AI |
| Nova 2 Omni | Multimodal (images, speech, text, video) |

Nova 2 models support extended thinking with configurable intensity (low/medium/high).

---

## Recommended Tiered Strategy

The hybrid routing architecture naturally maps to a tiered model strategy:

| Routing Tier | Model | Why | Cost Impact |
|-------------|-------|-----|-------------|
| **Conversational** (greetings, thanks, chitchat) | Nova Micro or Nova Lite | No intelligence needed, just polite responses | ~$0.0002/conversation |
| **Rule-matched formatting** (data already fetched) | Nova Pro or Haiku 4.5 | Data is ready, just format into readable text | ~$0.005/response |
| **AI routing with tool use** (decides which skill) | Claude Sonnet 4.5 | Needs reliable tool selection + parameter extraction | ~$0.02/response |

### Cost Comparison (estimated 1000 messages/day)

| Strategy | Monthly Cost |
|----------|-------------|
| All Sonnet 4.5 | ~$50-150 |
| Tiered (Micro/Haiku/Sonnet) | ~$15-40 |
| All Nova Pro | ~$10-25 |

### Important Caveats

- **Tool use quality:** Claude is significantly better at `tool_use` (picking the right tool, structuring parameters correctly). Nova models may fumble on complex tool selection.
- **Nova for formatting:** Nova is excellent when data is already fetched and you just need it formatted into human-readable text.
- **Test before committing:** Always test Nova models against your specific tool schemas before switching the AI routing tier away from Claude.

---

## Configuration

### Local Development (Anthropic API)

In `.env`:
```
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5-20250929
```

### AWS (Bedrock)

In `infra/aws/environments/dev.tfvars`:
```hcl
bedrock_model_id = "anthropic.claude-sonnet-4-5-20250929-v1:0"
```

### Per-Tenant Model Selection

The `TenantSettings` model supports per-tenant AI configuration:

```python
@dataclass
class TenantSettings:
    ai_provider: str = "bedrock"           # "bedrock", "anthropic", "openai"
    ai_model: str = "claude-sonnet-4-5-20250929"  # default model
    # Future: per-tier model selection
    # ai_model_conversational: str = "amazon.nova-micro-v1:0"
    # ai_model_formatting: str = "amazon.nova-pro-v1:0"
    # ai_model_routing: str = "anthropic.claude-sonnet-4-5-20250929-v1:0"
```

---

## Bedrock Model Access

- **Auto-enabled:** Most models activate on first invoke (no manual steps)
- **First-time Anthropic users:** May need to submit a use case form in the Bedrock console
- **Region availability:** All models available in us-east-1 (N. Virginia)

### Checking Available Models

```bash
aws bedrock list-foundation-models \
  --region us-east-1 \
  --query "modelSummaries[?contains(modelId, 'anthropic') || contains(modelId, 'nova')].{id:modelId, name:modelName}" \
  --output table
```

---

## Future: Intelligent Prompt Routing

Bedrock offers built-in Intelligent Prompt Routing that automatically selects between models in the same family based on prompt complexity. This could replace our custom tiered routing for the AI provider call:

- Routes between Nova Pro and Nova Lite automatically
- Routes between Claude Haiku and Claude Sonnet automatically
- Claims up to 30% cost reduction without accuracy loss

This is worth evaluating once the platform has enough traffic to measure quality differences.

---

## References

- [Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)
- [Amazon Nova Pricing](https://aws.amazon.com/nova/pricing/)
- [Amazon Nova 2 Announcement](https://www.aboutamazon.com/news/aws/aws-agentic-ai-amazon-bedrock-nova-models)
