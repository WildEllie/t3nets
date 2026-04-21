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


| Resource        | ~Monthly Cost |
| --------------- | ------------- |
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

## Custom Domain

By default the dashboard is served at the CloudFront-assigned URL
(`https://<hash>.cloudfront.net`). To serve it at your own domain — e.g.
`https://www.t3nets.dev` — set one line in your tfvars:

```hcl
root_domain = "t3nets.dev"
```

That's it. The API keeps living under `/api/*` on the same hostname, so
`https://www.t3nets.dev/api/chat` works the same way `.../api/chat` did on
the default CloudFront URL. The existing CloudFront URL also keeps working,
so nothing breaks during cutover.

Optional knobs (defaults shown):

```hcl
dashboard_subdomain = "www"    # final URL is <subdomain>.<root_domain>
manage_route53_zone = true     # set false if you manage DNS elsewhere
```

### If you use Route 53 (DNS delegated to AWS)

`terraform apply` creates the hosted zone and the ACM certificate. On the
**first** apply, Terraform will print the name servers for the new zone — you
need to set those at your domain registrar before certificate validation can
complete.

```bash
terraform apply -var-file=environments/dev.tfvars
terraform output route53_nameservers
```

Update the NS records at your registrar to the four values printed. ACM
validation will complete automatically (usually within a few minutes, up to
30). If the first apply times out waiting for validation, just re-run it
after the NS change has propagated.

### If you manage DNS externally (Cloudflare, registrar DNS, etc.)

Set `manage_route53_zone = false` in your tfvars. Terraform will still
provision the ACM certificate but will not create DNS records — you add
them at your DNS provider:

```bash
terraform apply -var-file=environments/dev.tfvars
terraform output acm_validation_records    # for ACM validation
terraform output cloudfront_domain         # for the www CNAME target
```

Add two records at your DNS provider:

1. **ACM validation** — CNAME from the `name` in `acm_validation_records` to
   its `value`.
2. **Dashboard** — CNAME from `www.<root_domain>` to the value of
   `cloudfront_domain`.

Once the validation CNAME resolves, ACM issues the cert and `terraform apply`
finishes. Browsing to `https://www.<root_domain>` then serves the dashboard.

### After enabling a custom domain

- The Cognito hosted-login callback/logout URLs for `https://<subdomain>.<root_domain>`
  are appended automatically — no extra tfvars edit needed.
- Channel webhooks (Telegram, WhatsApp, Teams) that tenants already registered
  still point at the CloudFront URL and keep working; only new registrations
  will use the custom domain.

