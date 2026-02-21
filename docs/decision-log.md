# T3nets — Architecture Decision Log

**Last Updated:** February 21, 2026

This document captures key architecture decisions, their rationale, and alternatives considered.

---

## ADR-001: Cloud-Agnostic Application Code

**Decision:** All business logic in `agent/` has zero cloud imports. Cloud-specific code lives in `adapters/`.

**Rationale:** Enables the project to be truly open-source — contributors can add Azure, GCP, or local adapters without touching the brain. Makes testing trivial (mock the interfaces).

**Alternatives considered:**
- Direct AWS SDK calls everywhere — faster to build but locks in AWS
- Pulumi/CDK in application code — tighter coupling

---

## ADR-002: Hybrid Routing (Rule-Based → Claude Fallback)

**Decision:** Three-tier routing: conversational (no API), rule-matched (1 API call), AI routing (2 API calls).

**Rationale:** 60-70% of messages match simple patterns. Calling Claude for "hi" or "sprint status" wastes money and adds latency. Rule engine handles the obvious cases; Claude handles the ambiguous ones.

**Cost impact:** ~50-60% reduction in Anthropic API costs vs. routing everything through Claude.

**Trade-offs:**
- Rule definitions need maintenance as skills are added
- Edge cases may route incorrectly (fall through to Claude anyway)
- Slightly more complex codebase

---

## ADR-003: DynamoDB Single-Table Design for Tenants

**Decision:** Single `tenants` table with composite keys and a GSI for channel mapping.

**Rationale:** Single-table design is DynamoDB best practice for related entities. All tenant data (metadata, users, channel mappings) lives in one table, accessed via different key patterns:

| Entity | PK | SK |
|--------|----|----|
| Tenant metadata | `TENANT#{id}` | `META` |
| User | `TENANT#{id}` | `USER#{user_id}` |
| Channel mapping | `TENANT#{id}` | `CHANNEL#{type}#{id}` |

GSI `channel-mapping` on `gsi1pk = CHANNEL#{type}#{id}` enables tenant resolution from webhook payloads.

**Alternatives considered:**
- Separate tables per entity — simpler queries but higher cost and more IAM policies
- PostgreSQL RDS — overkill for key-value access patterns, adds fixed cost

**Future expansion:** DynamoDB is schemaless. Add attributes like `preferences`, `memory_summary`, `custom_properties` to USER items without migration.

---

## ADR-004: ECS Fargate over Lambda for Router

**Decision:** Run the router as an always-warm ECS Fargate container, not a Lambda.

**Rationale:**
- Router needs to be fast (<100ms overhead) — Lambda cold starts add 500ms-2s
- Router maintains in-memory skill definitions
- WebSocket support for dashboard chat (Lambda doesn't support persistent connections well)
- Conversation flow requires multiple back-and-forth with Claude (tool calls) in a single request

**Cost:** ~$5-10/month for 0.25 vCPU / 512MB (always on).

**Alternatives considered:**
- Lambda with provisioned concurrency — similar cost but more complex
- EC2 — cheaper for sustained load but no auto-scaling, more ops burden

---

## ADR-005: Skills as Lambda Functions (via EventBridge)

**Decision:** Each skill runs as an independent Lambda, dispatched via EventBridge.

**Rationale:** Skills have unpredictable execution times (Jira API calls, email processing). Lambda gives per-invocation billing and independent scaling. EventBridge enables adding skills without changing the router.

**Not yet implemented:** Phase 1 uses synchronous `DirectBus` (local) and inline execution (AWS). EventBridge dispatch is Phase 3.

---

## ADR-006: HTTP API (v2) over REST API

**Decision:** Use API Gateway HTTP API (v2) instead of REST API (v1).

**Rationale:** HTTP API is ~70% cheaper, supports CORS natively, has lower latency, and we don't need REST API features (request validation, caching, API keys).

---

## ADR-007: Bedrock Converse API over InvokeModel

**Decision:** Use Bedrock's Converse API rather than raw InvokeModel.

**Rationale:** Converse API provides a unified interface across model providers. If we switch from Claude to Nova for certain tiers, the API call structure stays the same. Also handles tool_use natively.

---

## ADR-008: Internal ALB (not public)

**Decision:** ALB sits in private subnets, accessible only via API Gateway VPC Link.

**Rationale:** No direct internet access to the router container. All traffic flows through API Gateway, which handles CORS, throttling, and logging. Defense in depth.

---

## ADR-009: Per-Tenant Secrets in Secrets Manager

**Decision:** Store integration credentials (Jira tokens, GitHub tokens, etc.) in AWS Secrets Manager with path-based isolation: `/{project}/{env}/tenants/{tenant_id}/{integration}`.

**Rationale:** IAM policies can scope access by path. Each tenant's secrets are physically separated. Automatic rotation available for future.

**Alternatives considered:**
- DynamoDB encrypted attributes — no rotation, harder to audit
- SSM Parameter Store — cheaper but lacks rotation and fine-grained access
- HashiCorp Vault — overkill for prototype

---

## ADR-010: Tiered AI Model Strategy

**Decision:** Support different AI models for different routing tiers (conversational, formatting, tool use).

**Rationale:** Using Claude Sonnet ($0.003/$0.015 per 1K tokens) for "hi, how are you?" is wasteful. Amazon Nova Micro ($0.000035/$0.00014) handles greetings at ~100x lower cost. Claude remains essential for reliable tool use.

**Implementation:** `TenantSettings` stores model configuration. Dashboard settings page allows model selection per tier. Future: per-tenant model selection.

---

## ADR-011: SQLite for Local Development

**Decision:** Use SQLite for conversation storage in local dev, not DynamoDB Local.

**Rationale:** Zero setup — SQLite ships with Python. Contributors can run the full stack without Docker, Java, or any cloud tools. The ConversationStore interface abstracts the difference.

---

## ADR-012: Shared HTML Files Between Adapters

**Decision:** Chat and health HTML files live in `adapters/local/` and are served by both the local dev server and the AWS server.

**Rationale:** Same UI regardless of which adapter runs the server. Avoids duplication. The AWS server imports the same HTML files — the only difference is the backend wiring.

---

## ADR-013: NAT Gateway for Dev (Cost-Conscious)

**Decision:** Single NAT Gateway (not per-AZ) for dev environment.

**Rationale:** NAT Gateway costs ~$32/month fixed. A single NAT is sufficient for dev (no HA needed). Prod should use one per AZ.

**Cost optimization options for later:**
- NAT instance (~$4/month for t4g.nano)
- Place Fargate in public subnets (eliminates NAT entirely, but less secure)

---

## ADR-014: `--raw` Debug Mode

**Decision:** Appending `--raw` to any chat message skips Claude formatting and returns raw skill output.

**Rationale:** Essential for debugging during development. Lets you verify that skill workers return correct data before Claude reformats it. Zero cost (no Claude API call for formatting).

---

## ADR-015: Single-Region Bedrock Inference

**Decision:** Use single-region inference profiles (e.g. `us-east-1.anthropic.claude-3-5-sonnet-20241022-v2:0`) instead of cross-region (`us.` prefix). Restrict Bedrock IAM policy to `us-east-1` only.

**Rationale:** GDPR/HIPAA data residency compliance — data stays in a single region. Cross-region inference routes through AWS-managed endpoints; single-region keeps inference and data colocated. Also reduces blast radius of IAM permissions.

**Model choice:** Claude 3.5 Sonnet v2 selected for cost efficiency vs Claude 4.5 Sonnet v1 while maintaining strong tool-use quality.

**Implementation:**
- Bedrock IAM Resource: `arn:aws:bedrock:us-east-1::foundation-model/*` and `arn:aws:bedrock:us-east-1:{account}:inference-profile/*`
- Model ID: single-region inference profile format `us-east-1.{provider}.{model-id}:{version}` (e.g. `us-east-1.anthropic.claude-3-5-sonnet-20241022-v2:0`). Required because Claude 3.5 Sonnet v2 no longer supports direct foundation model invocation (on-demand throughput deprecated).

---

## Pending Decisions

| Topic | Status | Notes |
|-------|--------|-------|
| Nova models for conversational tier | Testing needed | Need to validate quality |
| WebSocket vs SSE for streaming | Deferred to Phase 2 | Current chat is request/response |
| Cognito vs Auth0 | Phase 2 | Cognito is cheaper but less flexible |
| S3 vs DynamoDB for long-term memory | Phase 5 | S3 is cheaper for large blobs |
| Dashboard restart from browser | In progress | Settings page + server restart API |
