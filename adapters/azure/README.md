# Azure Adapters — Community Contribution Welcome

T3nets needs Azure implementations for these 5 interfaces:

| Interface | AWS Implementation | Azure Equivalent |
|---|---|---|
| `AIProvider` | Amazon Bedrock | Azure OpenAI Service |
| `ConversationStore` | DynamoDB | Cosmos DB |
| `EventBus` | EventBridge | Azure Event Grid |
| `SecretsProvider` | Secrets Manager | Azure Key Vault |
| `BlobStore` | S3 | Azure Blob Storage |

## How to Contribute

1. Create one Python file per interface (e.g., `cosmosdb_store.py`)
2. Implement the abstract methods defined in `agent/interfaces/`
3. Create Terraform modules in `infra/azure/`
4. Add tests in `tests/integration/`
5. Open a PR

## Reference

See `adapters/aws/` for how the AWS implementation works.
Each file is a straightforward mapping from the abstract interface to the cloud SDK.
