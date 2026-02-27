# Phase 3b: Async Skill Execution — Implementation Plan

**Date:** 2026-02-27
**Status:** Planning
**Diagrams:** [phase-3b-architecture.html](phase-3b-architecture.html) | [phase-3b-architecture.mermaid](phase-3b-architecture.mermaid)

---

## Overview

Replace the synchronous DirectBus with an event-driven architecture. The router container stays stateless and horizontally scalable; skills run as Lambda functions.

**Current:** Webhook → Router → DirectBus (executes skill in-process) → Response
**Target:** Webhook → Router → EventBridge → Lambda → SQS → Router → Response

---

## Architecture

### Router (ECS Fargate)
- Always warm, handles webhooks, runs Tier 1/2/3 routing
- Owns all channel adapters — skills never touch channels
- Publishes `skill.invoke` events to EventBridge
- Polls SQS results queue in a background thread
- Fully stateless — all state in DynamoDB, scales horizontally

### Lambda Skill Executor
- Single Lambda function, dispatches by `skill_name` parameter
- Loads SkillRegistry, fetches secrets from Secrets Manager
- Calls `worker.execute(params, secrets)` — same contract as DirectBus
- Publishes result to SQS results queue
- No channel awareness — pure input/output

### EventBridge
- Event bus receives `skill.invoke` events from router
- Rule routes events to Lambda (async invocation)
- DLQ for failed invocations

### SQS Results Queue
- Lambda writes skill results here
- Router background thread polls and routes to correct channel adapter
- Standard queue (not FIFO), visibility timeout 30s
- DLQ for messages that fail processing

### DynamoDB Pending Requests
- Tracks in-flight skill invocations
- Stores: request_id, tenant_id, channel, conversation_id, reply_target
- Enables any router instance to pick up any result (horizontal scaling)
- TTL: 5 minutes (auto-cleanup)

---

## Request Flows

### Tier 1 — Conversational (unchanged, sync)
```
Channel → API Gateway → Router (regex match) → Bedrock AI → Router → Channel
```
No Lambda involved. Fast path stays fast.

### Tier 2 — Rule-Matched Skill (async)
```
Channel → Router (rule match) → EventBridge → Lambda (skill) → SQS → Router (poller) → Bedrock AI (format) → Channel
```

### Tier 3 — AI-Routed Skill (async)
```
Channel → Router → Bedrock AI (tool choice) → EventBridge → Lambda (skill) → SQS → Router (poller) → Bedrock AI (format) → Channel
```

---

## Implementation Tasks

### 1. EventBridgeBus Adapter
**File:** `adapters/aws/event_bridge_bus.py`

New EventBus implementation for AWS:
- `async publish()` — puts event to EventBridge, returns immediately
- No `get_result()` — results come through SQS instead
- Event payload: tenant_id, skill_name, params, request_id, reply_channel, reply_target, session_id

### 2. Lambda Skill Handler
**File:** `adapters/aws/lambda_handler.py`

Single Lambda entry point:
- Receives EventBridge event with skill payload
- Loads SkillRegistry (cached after cold start)
- Fetches secrets via SecretsManagerProvider
- Calls `worker_fn(params, secrets)`
- Publishes result + request context to SQS
- Runtime: Python 3.12, 512 MB, 30s timeout

### 3. SQS Poller in Router
**File:** `adapters/aws/server.py` (modified)

Background thread added to router:
- Long-polls SQS results queue (WaitTimeSeconds=1)
- On message: resolve pending request from DynamoDB, get channel/conversation context
- Route response through channel adapter (Telegram/Teams/Dashboard)
- Delete message from SQS after successful delivery
- Handles: timeouts, Lambda failures, duplicate messages

### 4. Pending Requests Table
**DynamoDB table:** `t3nets-{env}-pending-requests`

Schema:
```
pk: {request_id}
Attributes: tenant_id, skill_name, channel, conversation_id,
            reply_target, is_raw, created_at, timeout_at
TTL: ttl (Unix epoch, 5 min after creation)
```

### 5. Router Code Changes
**File:** `adapters/aws/server.py` (modified)

- Replace `bus.get_result(request_id)` pattern:
  - For sync path (Tier 1): unchanged
  - For async path (Tier 2/3): store pending request in DynamoDB, return 200 to webhook
  - SQS poller delivers result asynchronously
- Feature flag: `USE_ASYNC_SKILLS` env var for incremental rollout
- Remove in-memory state (service URL caches → DynamoDB)
- Add `stats` persistence to DynamoDB (or accept ephemeral counters per instance)

### 6. Terraform Infrastructure

New files:
- `infra/aws/modules/compute/lambda.tf` — Lambda function, IAM role, provisioned concurrency
- `infra/aws/modules/compute/eventbridge.tf` — Event bus, rule, DLQ
- `infra/aws/modules/compute/sqs.tf` — Results queue, DLQ
- `infra/aws/modules/data/pending_requests.tf` — DynamoDB table

IAM changes:
- ECS task role: add `events:PutEvents`, `sqs:ReceiveMessage`, `sqs:DeleteMessage`
- Lambda role: add `secretsmanager:GetSecretValue`, `dynamodb:GetItem`, `sqs:SendMessage`

### 7. Local Development
- DirectBus stays for local dev (no Lambda/EventBridge locally)
- Feature flag `USE_ASYNC_SKILLS=false` keeps sync behavior
- Can test async locally with moto (AWS mocking) in future

---

## Migration Strategy

### Phase A: Deploy infrastructure (no behavior change)
1. Terraform: create Lambda, EventBridge rule, SQS queues, pending requests table
2. Deploy router with feature flag `USE_ASYNC_SKILLS=false`
3. Verify everything still works with DirectBus

### Phase B: Enable async skills
1. Set `USE_ASYNC_SKILLS=true` in ECS task environment
2. Router starts using EventBridgeBus + SQS poller
3. Monitor: CloudWatch metrics, ECS logs, SQS DLQ depth
4. Rollback: flip flag back to `false`

### Phase C: Horizontal scaling
1. Increase ECS desired_count to 2+
2. Verify no message loss or duplicate responses
3. Load test with concurrent messages across channels

---

## Design Decisions

| Decision | Why |
|----------|-----|
| Single Lambda vs per-skill | Simpler infra, easier deployment, one IAM role |
| SQS for results (not DynamoDB) | Natural queue semantics, built-in retry, no polling needed |
| Background polling thread | Simpler than WebSocket/callback, works with existing HTTP server |
| DynamoDB for pending requests | Enables horizontal scaling — any router instance handles any result |
| Feature flag rollout | Zero-risk deployment, instant rollback |
| Provisioned concurrency (2) | Eliminates cold start latency for dev (~$15/mo) |

---

## Key Constraints

- **Worker contract unchanged:** `def execute(params: dict, secrets: dict) -> dict`
- **Cloud-agnostic core:** No Lambda imports in `agent/` — only in `adapters/aws/`
- **DirectBus preserved:** Local dev continues working without AWS services
- **Channel isolation:** Skills never know which channel the request came from
- **Stateless router:** No in-memory state that would break with multiple instances

---

## Cost Estimate (dev)

| Resource | Monthly Cost |
|----------|-------------|
| Lambda (1000 invocations, 512MB, 5s avg) | ~$0.10 |
| Lambda provisioned concurrency (2) | ~$15 |
| EventBridge (1000 events) | ~$0.01 |
| SQS (1000 messages) | ~$0.01 |
| DynamoDB pending requests (PAY_PER_REQUEST) | ~$0.25 |
| **Total new cost** | **~$15.40** |

Existing ECS + NAT stays the same (~$35-50/mo).

---

## Files Summary

### New Files
| File | Purpose |
|------|---------|
| `adapters/aws/event_bridge_bus.py` | EventBridge EventBus implementation |
| `adapters/aws/lambda_handler.py` | Lambda entry point for skill execution |
| `infra/aws/modules/compute/lambda.tf` | Lambda function + IAM |
| `infra/aws/modules/compute/eventbridge.tf` | EventBridge bus + rule |
| `infra/aws/modules/compute/sqs.tf` | Results queue + DLQ |
| `infra/aws/modules/data/pending_requests.tf` | Pending requests table |

### Modified Files
| File | Change |
|------|--------|
| `adapters/aws/server.py` | SQS poller thread, feature flag, async result handling |
| `infra/aws/modules/compute/main.tf` | IAM additions (events, SQS), env vars |
| `infra/aws/modules/compute/variables.tf` | Lambda config vars |
| `infra/aws/modules/compute/outputs.tf` | Queue URLs, Lambda ARN |
| `infra/aws/modules/data/outputs.tf` | Pending requests table name |

### Unchanged
| File | Why |
|------|-----|
| `agent/interfaces/event_bus.py` | Interface stays the same |
| `agent/skills/*` | Worker contract unchanged |
| `agent/router/*` | Routing logic unchanged |
| `agent/channels/*` | Channel adapters unchanged |
| `adapters/local/direct_bus.py` | Kept for local dev |
