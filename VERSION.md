# T3nets — Version Log

---

## v0.1.0 — Phase 1 Milestone (February 22, 2026)

**AWS deployment with configurable AI models**

### Features
- Cloud-agnostic core architecture with adapter pattern (local, AWS, future GCP/Azure)
- Hybrid routing engine: regex (Tier 1) → rule-matched skills (Tier 2) → full Claude with tools (Tier 3)
- Centralized AI model registry with 6 verified models: Claude Sonnet 4.5, Claude Sonnet 4.6, Nova Pro, Nova Lite, Nova Micro, Meta Llama 3.2 1B
- Settings page with model selection UI (hot-swappable per tenant)
- Chat history persistence across page navigation
- Markdown rendering in chat (tables, code blocks, lists, headings)
- Dynamic dual-badge system: platform (LOCAL/AWS) + stage (DEV/STAGING/PROD)
- Health dashboard with live routing stats, integration status, and skill inventory
- `--raw` debug mode for raw skill output (zero AI cost)
- Sprint status skill (Jira integration: status, blockers, my issues)
- Ping skill for lightweight model testing (no integrations required)
- Terraform infrastructure: VPC, ECS Fargate, API Gateway, DynamoDB, Secrets Manager, ECR
- Staging and prod Terraform tfvars templates
- Deploy and seed scripts for ECS

### Bug Fixes
- Fixed Bedrock geographic inference profiles (us/eu/apac prefixes for Sonnet 4.5+ and Nova)
- Fixed cross-region IAM for Bedrock inference (us-east-1, us-east-2, us-west-2)
- Fixed Bedrock tool_use/tool_result message conversion for Converse API
- Fixed 400 Bad Request caused by metadata in conversation history sent to AI providers
- Fixed double region prefix in ECS model configuration
- Removed retired Claude 3.5 Sonnet and invalid Nova v2 model IDs
- Removed incompatible Voxtral Mini (audio-only) and Gemma 3 4B (not on Bedrock)
