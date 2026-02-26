"""
AI Model Registry.

Single source of truth for all supported models across providers.
"""

from dataclasses import dataclass, field


@dataclass
class AIModel:
    """A supported AI model with provider-specific identifiers."""

    id: str  # internal key, e.g. "claude-3-5-sonnet"
    short_name: str  # display tag in chat, e.g. "Claude 3.5"
    display_name: str  # full name for settings page
    anthropic_id: str  # Anthropic API model ID ("" if unsupported)
    bedrock_id: str  # base Bedrock model ID, no region prefix ("" if unsupported)
    providers: list[str] = field(default_factory=list)  # ["anthropic", "bedrock"]


AVAILABLE_MODELS: dict[str, AIModel] = {
    "claude-sonnet-4-5": AIModel(
        id="claude-sonnet-4-5",
        short_name="Sonnet 4.5",
        display_name="Claude Sonnet 4.5",
        anthropic_id="claude-sonnet-4-5-20250929",
        bedrock_id="anthropic.claude-sonnet-4-5-20250929-v1:0",
        providers=["anthropic", "bedrock"],
    ),
    "claude-sonnet-4-6": AIModel(
        id="claude-sonnet-4-6",
        short_name="Sonnet 4.6",
        display_name="Claude Sonnet 4.6",
        anthropic_id="claude-sonnet-4-6",
        bedrock_id="anthropic.claude-sonnet-4-6",
        providers=["anthropic", "bedrock"],
    ),
    "nova-pro": AIModel(
        id="nova-pro",
        short_name="Nova Pro",
        display_name="Amazon Nova Pro",
        anthropic_id="",
        bedrock_id="amazon.nova-pro-v1:0",
        providers=["bedrock"],
    ),
    "nova-lite": AIModel(
        id="nova-lite",
        short_name="Nova Lite",
        display_name="Amazon Nova Lite",
        anthropic_id="",
        bedrock_id="amazon.nova-lite-v1:0",
        providers=["bedrock"],
    ),
    "nova-micro": AIModel(
        id="nova-micro",
        short_name="Nova Micro",
        display_name="Amazon Nova Micro",
        anthropic_id="",
        bedrock_id="amazon.nova-micro-v1:0",
        providers=["bedrock"],
    ),
    "llama-3-2-1b": AIModel(
        id="llama-3-2-1b",
        short_name="Llama 3.2 1B",
        display_name="Meta Llama 3.2 1B",
        anthropic_id="",
        bedrock_id="meta.llama3-2-1b-instruct-v1:0",
        providers=["bedrock"],
    ),
}

DEFAULT_MODEL_ID = "claude-sonnet-4-5"


def get_model(model_id: str) -> AIModel | None:
    """Look up a model by its internal ID."""
    return AVAILABLE_MODELS.get(model_id)


def get_model_for_provider(model_id: str, provider: str) -> str:
    """Resolve a model's internal ID to the provider-specific model string.

    Returns the provider-specific ID (e.g. Anthropic API ID or Bedrock ID),
    or empty string if the model doesn't support that provider.
    """
    model = AVAILABLE_MODELS.get(model_id)
    if not model:
        return ""
    if provider == "anthropic":
        return model.anthropic_id
    if provider == "bedrock":
        return model.bedrock_id
    return ""


def get_models_for_provider(provider: str) -> list[dict]:
    """Return all models with availability info for a given provider.

    Returns list of dicts suitable for API responses / UI rendering.
    """
    result = []
    for model in AVAILABLE_MODELS.values():
        result.append({
            "id": model.id,
            "short_name": model.short_name,
            "display_name": model.display_name,
            "available": provider in model.providers,
        })
    return result
