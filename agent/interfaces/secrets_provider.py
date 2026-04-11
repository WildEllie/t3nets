"""
Re-export shim — canonical definitions live in t3nets_sdk.interfaces.secrets_provider.

Kept for backwards-compatible imports of the form:
    from agent.interfaces.secrets_provider import SecretsProvider, SecretNotFoundError
"""

from t3nets_sdk.interfaces.secrets_provider import SecretNotFoundError, SecretsProvider

__all__ = ["SecretsProvider", "SecretNotFoundError"]
