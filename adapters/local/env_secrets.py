"""
Local Secrets Provider — reads from .env file.

For local development. No Secrets Manager dependency.
Secrets are loaded from environment variables with a naming convention:
    {INTEGRATION}_URL, {INTEGRATION}_EMAIL, etc.
"""

import os
from pathlib import Path

from agent.interfaces.secrets_provider import SecretsProvider, SecretNotFound


# Maps integration names to their env var prefixes and expected keys
INTEGRATION_KEYS = {
    "jira": {
        "url": "JIRA_URL",
        "email": "JIRA_EMAIL",
        "api_token": "JIRA_API_TOKEN",
        "board_id": "JIRA_BOARD_ID",
    },
    "github": {
        "token": "GITHUB_TOKEN",
        "org": "GITHUB_ORG",
    },
    "teams": {
        "app_id": "TEAMS_APP_ID",
        "app_secret": "TEAMS_APP_SECRET",
        "tenant_id": "TEAMS_TENANT_ID",
    },
    "twilio": {
        "account_sid": "TWILIO_ACCOUNT_SID",
        "auth_token": "TWILIO_AUTH_TOKEN",
        "phone_number": "TWILIO_PHONE_NUMBER",
    },
}


class EnvSecretsProvider(SecretsProvider):
    """
    Reads secrets from environment variables.
    In local mode, all tenants share the same secrets (from .env).
    """

    def __init__(self, env_file: str = ".env"):
        self._load_env_file(env_file)

    def _load_env_file(self, env_file: str):
        """Load .env file into os.environ."""
        path = Path(env_file)
        if not path.exists():
            return
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())

    async def get(self, tenant_id: str, integration_name: str) -> dict:
        """
        Get secrets for an integration.
        In local mode, tenant_id is ignored (single-tenant).
        """
        key_map = INTEGRATION_KEYS.get(integration_name)
        if not key_map:
            raise SecretNotFound(
                f"Unknown integration: {integration_name}. "
                f"Known: {list(INTEGRATION_KEYS.keys())}"
            )

        secrets = {}
        for secret_key, env_var in key_map.items():
            value = os.getenv(env_var, "")
            if value:
                secrets[secret_key] = value

        if not secrets:
            raise SecretNotFound(
                f"No secrets found for '{integration_name}'. "
                f"Set these in .env: {list(key_map.values())}"
            )

        return secrets

    async def put(self, tenant_id: str, integration_name: str, secrets: dict) -> None:
        """In local mode, just set env vars (non-persistent)."""
        key_map = INTEGRATION_KEYS.get(integration_name, {})
        for secret_key, value in secrets.items():
            env_var = key_map.get(secret_key, f"{integration_name.upper()}_{secret_key.upper()}")
            os.environ[env_var] = value

    async def delete(self, tenant_id: str, integration_name: str) -> None:
        key_map = INTEGRATION_KEYS.get(integration_name, {})
        for env_var in key_map.values():
            os.environ.pop(env_var, None)

    async def list_integrations(self, tenant_id: str) -> list[str]:
        """List integrations that have at least one env var set."""
        connected = []
        for name, key_map in INTEGRATION_KEYS.items():
            if any(os.getenv(env_var) for env_var in key_map.values()):
                connected.append(name)
        return connected
