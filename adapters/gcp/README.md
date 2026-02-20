# GCP Adapters — Community Contribution Welcome

T3nets needs GCP implementations for these 5 interfaces:

| Interface | AWS Implementation | GCP Equivalent |
|---|---|---|
| `AIProvider` | Amazon Bedrock | Vertex AI (Claude) |
| `ConversationStore` | DynamoDB | Firestore / Bigtable |
| `EventBus` | EventBridge | Eventarc / Pub/Sub |
| `SecretsProvider` | Secrets Manager | Secret Manager |
| `BlobStore` | S3 | Cloud Storage |

## How to Contribute

1. Create one Python file per interface
2. Implement the abstract methods defined in `agent/interfaces/`
3. Create Terraform modules in `infra/gcp/`
4. Add tests in `tests/integration/`
5. Open a PR

## Reference

See `adapters/aws/` for how the AWS implementation works.
