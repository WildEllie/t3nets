"""
AWS Secrets Provider — AWS Secrets Manager.

Secrets stored at: /{prefix}/{tenant_id}/{integration}
Each secret is a JSON blob with integration-specific keys.
"""

import json
import boto3
from botocore.exceptions import ClientError

from agent.interfaces.secrets_provider import SecretsProvider, SecretNotFound


class SecretsManagerProvider(SecretsProvider):
    """AWS Secrets Manager backed secrets provider."""

    def __init__(self, prefix: str, region: str = "us-east-1"):
        self.prefix = prefix.rstrip("/")
        self.client = boto3.client("secretsmanager", region_name=region)

    def _secret_id(self, tenant_id: str, integration_name: str) -> str:
        return f"{self.prefix}/{tenant_id}/{integration_name}"

    async def get(self, tenant_id: str, integration_name: str) -> dict:
        secret_id = self._secret_id(tenant_id, integration_name)

        try:
            response = self.client.get_secret_value(SecretId=secret_id)
            return json.loads(response["SecretString"])
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("ResourceNotFoundException", "DecryptionFailureException"):
                raise SecretNotFound(
                    f"No secrets found for tenant '{tenant_id}', "
                    f"integration '{integration_name}'"
                )
            raise

    async def put(self, tenant_id: str, integration_name: str, secrets: dict) -> None:
        secret_id = self._secret_id(tenant_id, integration_name)
        secret_string = json.dumps(secrets)

        try:
            self.client.update_secret(
                SecretId=secret_id,
                SecretString=secret_string,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                self.client.create_secret(
                    Name=secret_id,
                    SecretString=secret_string,
                    Tags=[
                        {"Key": "Project", "Value": "t3nets"},
                        {"Key": "TenantId", "Value": tenant_id},
                        {"Key": "Integration", "Value": integration_name},
                    ],
                )
            else:
                raise

    async def delete(self, tenant_id: str, integration_name: str) -> None:
        secret_id = self._secret_id(tenant_id, integration_name)

        try:
            self.client.delete_secret(
                SecretId=secret_id,
                ForceDeleteWithoutRecovery=True,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise

    async def list_integrations(self, tenant_id: str) -> list[str]:
        prefix = f"{self.prefix}/{tenant_id}/"

        try:
            response = self.client.list_secrets(
                Filters=[{"Key": "name", "Values": [prefix]}],
                MaxResults=20,
            )
        except ClientError:
            return []

        integrations = []
        for secret in response.get("SecretList", []):
            name = secret["Name"]
            # Extract integration name from path
            integration = name.replace(prefix, "").strip("/")
            if integration:
                integrations.append(integration)

        return integrations
