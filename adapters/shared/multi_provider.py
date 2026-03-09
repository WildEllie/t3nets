"""Multi-provider AI dispatcher.

Routes AI calls to the correct underlying provider based on which
provider the selected model belongs to. Holds multiple provider instances
simultaneously (e.g. Bedrock + Ollama on AWS, Anthropic + Ollama locally).
"""

from agent.interfaces.ai_provider import AIProvider


class MultiAIProvider:
    """Holds multiple AIProvider instances and routes by provider name.

    Usage:
        providers = {"bedrock": bedrock_inst, "ollama": ollama_inst}
        ai = MultiAIProvider(providers)

        provider_name, api_model_id = resolve_model(tenant)
        response = await ai.for_provider(provider_name).chat(api_model_id, ...)
    """

    def __init__(self, providers: dict[str, AIProvider]) -> None:
        self._providers = providers

    @property
    def active_providers(self) -> list[str]:
        """List of provider names that are currently active."""
        return list(self._providers.keys())

    def for_provider(self, name: str) -> AIProvider:
        """Return the AIProvider for the given provider name.

        Falls back to the first available provider if name is not recognised,
        so callers never get a KeyError on misconfiguration.
        """
        return self._providers.get(name, next(iter(self._providers.values())))
