# T3nets — AWS Infrastructure Reference

**Last Updated:** February 21, 2026
**Status:** Phase 1 Terraform complete, ready for `terraform apply`

---

## Architecture Overview

```
Internet → API Gateway (HTTP) → VPC Link → ALB (internal) → ECS Fargate (router)
                                                                    ↓
                                                        DynamoDB (conversations, tenants)
                                                        Secrets Manager (per-tenant creds)
                                                        Bedrock (Claude AI)
```

---

## Terraform Modules

All infrastructure is in `infra/aws/`, organized as reusable modules:

### Module: networking
**Path:** `infra/aws/modules/networking/`

- VPC: `10.0.0.0/16` with DNS support enabled
- 2 public subnets: `10.0.1.0/24`, `10.0.2.0/24` (across 2 AZs)
- 2 private subnets: `10.0.10.0/24`, `10.0.11.0/24`
- Internet Gateway → public subnets
- NAT Gateway (single, in public subnet) → private subnets
- Route tables: public → IGW, private → NAT

**Cost note:** The NAT Gateway is the biggest cost (~$32/mo). For non-prod, consider a NAT instance or placing Fargate in public subnets.

### Module: data
**Path:** `infra/aws/modules/data/`

**conversations table** (PAY_PER_REQUEST):
- PK: `{tenant_id}#channel#{user_id}`
- SK: `{session_id}`
- TTL: 30-day auto-expiry
- Stores: messages (JSON array), updated_at

**tenants table** (PAY_PER_REQUEST, single-table design):
- PK: `TENANT#{tenant_id}` or `USER#{tenant_id}`
- SK: `META`, `USER#{user_id}`, `CHANNEL#{channel_type}#{id}`
- GSI `channel-mapping`: `gsi1pk=CHANNEL#{type}#{id}` → resolves tenant from channel webhooks

Schema supports without migration: tenant metadata, users, channel mappings, user preferences, custom properties, memory summaries.

### Module: secrets
**Path:** `infra/aws/modules/secrets/`

- Base path: `/{project}/{environment}/tenants/{tenant_id}/{integration}`
- Secrets created dynamically by admin API (not Terraform)
- IAM policy ARN for path-based access control

### Module: ecr
**Path:** `infra/aws/modules/ecr/`

- Repository: `{project}-{env}-router`
- Image scanning on push
- Lifecycle: keep last 10 images
- `force_delete=true` for dev convenience

### Module: compute
**Path:** `infra/aws/modules/compute/`

**ECS Fargate:**
- Cluster with Container Insights (disabled in dev)
- Task definition: 256 CPU, 512MB memory
- Service: desired_count=1, rolling deployment (100% min / 200% max)
- Health check: `GET /health` on port 8080

**Internal ALB:**
- In private subnets
- Target group → port 8080
- VPC Link for API Gateway → ALB connection

**Security groups:**
- ALB: allows 80 from anywhere
- Router: allows 8080 from ALB only

**IAM task role permissions:**
- DynamoDB: GetItem, PutItem, UpdateItem, DeleteItem, Query, Scan
- Secrets Manager: GetSecretValue, CreateSecret, UpdateSecret, ListSecrets
- Bedrock: InvokeModel, InvokeModelWithResponseStream
- CloudWatch Logs: CreateLogGroup, CreateLogStream, PutLogEvents

**Environment variables passed to container:**
- `T3NETS_ENV=aws`
- `AWS_REGION`
- `BEDROCK_MODEL_ID`
- `DYNAMODB_CONVERSATIONS_TABLE`
- `DYNAMODB_TENANTS_TABLE`
- `SECRETS_PREFIX`

### Module: api
**Path:** `infra/aws/modules/api/`

- HTTP API (v2) — cheaper than REST API
- CORS: allow all origins, GET/POST/OPTIONS
- Integration: HTTP_PROXY → ALB via VPC Link
- Route: `$default` catch-all (forwards all paths)
- Stage: `$default` with auto-deploy
- Access logs: JSON format, 14-day retention

---

## AWS Adapters

Code in `adapters/aws/`:

| File | Purpose |
|------|---------|
| `bedrock_provider.py` | AIProvider → Bedrock Converse API (IAM auth, no API key) |
| `dynamodb_conversation_store.py` | ConversationStore → DynamoDB (JSON messages, 30-day TTL) |
| `dynamodb_tenant_store.py` | TenantStore → DynamoDB single-table (GSI for channel mapping) |
| `secrets_manager.py` | SecretsProvider → AWS Secrets Manager (path-based, per-tenant) |
| `server.py` | HTTP server wired to AWS adapters (runs in Fargate container) |

---

## Deployment Steps

### Prerequisites
- AWS CLI configured with appropriate credentials
- Terraform >= 1.5
- Docker
- Bedrock model access in us-east-1

### 1. Terraform

```bash
cd infra/aws
terraform init
terraform plan -var-file=environments/dev.tfvars
terraform apply -var-file=environments/dev.tfvars
```

### 2. Seed Data

```bash
./scripts/seed.sh
```

Seeds default tenant + admin user into DynamoDB, creates Jira secret in Secrets Manager.

### 3. Build & Deploy Container

```bash
./scripts/deploy.sh
```

Builds Docker image, pushes to ECR, updates ECS service, waits for stabilization.

### 4. Test

```bash
# Health check
curl $(terraform -chdir=infra/aws output -raw api_endpoint)/api/health

# Chat
curl -X POST $(terraform -chdir=infra/aws output -raw api_endpoint)/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "what is the sprint status?"}'
```

---

## Cost Estimate (Dev Environment)

| Service | Monthly Cost |
|---------|-------------|
| NAT Gateway | ~$32 |
| ECS Fargate (0.25 vCPU, 0.5GB) | ~$5-10 |
| DynamoDB (on-demand) | ~$1-2 |
| API Gateway | ~$1-2 |
| ECR | ~$0.50 |
| Secrets Manager | ~$0.50 |
| CloudWatch Logs | ~$0.50 |
| Bedrock (Claude Sonnet) | ~$10-50 (usage dependent) |
| **Total** | **~$35-50/mo** (excluding Bedrock usage) |

### Cost Optimization Options
- Replace NAT Gateway with NAT instance (~$4/mo vs $32/mo)
- Use Nova models for conversational/formatting tiers
- Enable Bedrock Intelligent Prompt Routing

---

## Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir pyyaml boto3
COPY agent/ agent/
COPY adapters/ adapters/
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"
CMD ["python", "-m", "adapters.aws.server"]
```

---

## Files Reference

```
infra/aws/
├── main.tf                    # Root module, wires all modules
├── variables.tf               # project, environment, region, CPU/memory, model ID
├── outputs.tf                 # api_endpoint, ecr_url, cluster/service names, table names
├── backend.tf                 # S3 remote state (commented, optional)
├── environments/
│   └── dev.tfvars             # Dev values (256 CPU, 512MB, us-east-1)
└── modules/
    ├── networking/main.tf     # VPC, subnets, NAT, IGW
    ├── data/main.tf           # DynamoDB tables (conversations, tenants)
    ├── secrets/main.tf        # Secrets Manager base config
    ├── ecr/main.tf            # Container registry
    ├── compute/main.tf        # ECS Fargate, ALB, IAM, task definition
    └── api/main.tf            # API Gateway HTTP API

scripts/
├── deploy.sh                  # Build, push, deploy container
└── seed.sh                    # Seed default tenant + secrets

Dockerfile                     # Router container image
```
