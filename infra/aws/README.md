# T3nets — AWS Deployment Guide

## Prerequisites

1. **AWS CLI** configured with credentials (`aws configure`)
2. **Terraform** >= 1.5 installed
3. **Docker** running
4. **Bedrock model access** — request access to Claude in the AWS console:
   - Go to Amazon Bedrock → Model access → Request access to Anthropic Claude models

## First-Time Setup

### 1. Deploy Infrastructure

```bash
cd infra/aws
terraform init
terraform plan -var-file=environments/dev.tfvars
terraform apply -var-file=environments/dev.tfvars
```

This creates: VPC, ECS cluster, DynamoDB tables, Secrets Manager paths, ECR repo, API Gateway.

Estimated cost: **~$35-50/month** for dev (NAT gateway is the biggest cost).

### 2. Seed Data

Push your tenant and Jira credentials into AWS:

```bash
cd ~/projects/t3nets
./scripts/seed.sh
```

This reads your `.env` file and populates DynamoDB + Secrets Manager.

### 3. Build & Deploy

```bash
./scripts/deploy.sh
```

This builds the Docker image, pushes to ECR, and updates the ECS service.

### 4. Test

Get the API endpoint:

```bash
cd infra/aws
terraform output api_endpoint
```

Test it:

```bash
API=$(cd infra/aws && terraform output -raw api_endpoint)

# Health check
curl $API/api/health | python -m json.tool

# Chat
curl -X POST $API/api/chat \
  -H "Content-Type: application/json" \
  -d '{"text": "sprint status --raw"}' | python -m json.tool
```

Open the dashboard: `$API/chat`

## Updating

After code changes:

```bash
./scripts/deploy.sh
```

After infrastructure changes:

```bash
cd infra/aws
terraform plan -var-file=environments/dev.tfvars
terraform apply -var-file=environments/dev.tfvars
```

## Tear Down

```bash
cd infra/aws
terraform destroy -var-file=environments/dev.tfvars
```

## Architecture

```
Internet
  → API Gateway (HTTP API)
    → VPC Link
      → Internal ALB
        → ECS Fargate (router container)
          → Bedrock (Claude)
          → DynamoDB (conversations, tenants)
          → Secrets Manager (Jira credentials)
          → Jira API (skill execution)
```

## Cost Breakdown (dev)

| Resource         | ~Monthly Cost |
|-----------------|---------------|
| NAT Gateway     | $32           |
| ECS Fargate     | $5-10         |
| DynamoDB        | $0-2          |
| API Gateway     | $0-1          |
| ECR             | $0-1          |
| Secrets Manager | $0-1          |
| CloudWatch      | $0-1          |
| **Total**       | **~$35-50**   |

## Cost Optimization Tips

- **NAT Gateway** is the biggest cost. For non-production, you could use a NAT instance instead (~$4/month).
- DynamoDB PAY_PER_REQUEST keeps costs near zero for low traffic.
- Fargate 0.25 vCPU / 512MB is the minimum — sufficient for development.
