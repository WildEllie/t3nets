# Phase 3b: Async Skill Execution — Implementation Plan

**Date:** 2026-02-28 (revised)
**Status:** Planning
**Diagrams:** [phase-3b-architecture.html](phase-3b-architecture.html) | [phase-3b-architecture.mermaid](phase-3b-architecture.mermaid)

---

## Overview

Replace the synchronous DirectBus with an event-driven architecture. The router container stays stateless and horizontally scalable; skills run as Lambda functions. The dashboard receives async results via Server-Sent Events (SSE).

**Current:** Webhook → Router → DirectBus (executes skill in-process) → Response
**Target:** Webhook → Router → EventBridge → Lambda → SQS → Router → Response (via SSE/channel push)

---

## Architecture

### Router (ECS Fargate)
- Always warm, handles webhooks, runs Tier 1/2/3 routing
- Owns all channel adapters — skills never touch channels
- Publishes `skill.invoke` events to EventBridge
- Polls SQS results queue in a background thread
- Serves SSE endpoint (`GET /api/events`) for dashboard async results
- Fully stateless — all state in DynamoDB, scales horizontally

### Lambda Skill Executor
- Single Lambda function, dispatches by `skill_name` parameter
- Lazy-loads skill dependencies at invocation time (not at module level) to reduce cold starts
- Loads SkillRegistry, fetches secrets from Secrets Manager
- **Idempotency check:** reads pending request status from DynamoDB before executing; skips if already completed
- Calls `worker.execute(params, secrets)` — same contract as DirectBus
- Marks request as `completed` in DynamoDB, publishes result to SQS results queue
- No channel awareness — pure input/output

### EventBridge
- Event bus receives `skill.invoke` events from router
- Rule routes events to Lambda (async invocation)
- MaximumRetryAttempts=2 (default) — safe because Lambda checks idempotency via DynamoDB
- DLQ (SQS) for invocations that fail after all retries

### SQS Results Queue
- Lambda writes skill results here after successful execution
- Router background thread long-polls (WaitTimeSeconds=20) and routes to correct channel
- Standard queue (not FIFO), visibility timeout 30s
- DLQ for messages that fail processing after 3 attempts

### DynamoDB Pending Requests Table
- Tracks in-flight skill invocations
- Stores: request_id, tenant_id, channel, conversation_id, reply_target, service_url (Teams), is_raw, status
- **Status field:** `pending` → `completed` (used for idempotency)
- Enables any router instance to pick up any result (horizontal scaling)
- TTL: 5 minutes (auto-cleanup)

### SSE for Dashboard (new component)
- Dashboard opens `GET /api/events?token={jwt}` — long-lived SSE stream
- Router maintains in-memory map of `user_id → SSE connection` (per instance)
- When SQS poller picks up a dashboard result, it writes to the matching SSE connection
- If no SSE connection exists on this instance (user connected to a different ECS task), write result to a short-lived DynamoDB record; other instances poll or the client reconnects
- Keepalive comments (`: keepalive\n\n`) sent every 15s to prevent API Gateway 30s timeout
- Auto-reconnect built into SSE spec (browser handles it natively)
- Fallback: if SSE connection drops, dashboard can poll `GET /api/results/{request_id}`

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

### Dashboard-Specific Flow (new)
```
1. Dashboard opens SSE stream: GET /api/events?token={jwt}
2. User sends message: POST /api/chat → Router returns {request_id, status: "processing"}
3. Router publishes skill.invoke to EventBridge
4. Lambda executes skill → SQS result
5. Router SQS poller picks up result → formats with Bedrock AI
6. Router pushes result to SSE stream: event: message\ndata: {text, conversation_id, ...}
7. Dashboard JS renders the response
```

### Teams/Telegram Flow (async push, same as before)
```
1. Webhook arrives → Router returns 200 immediately
2. Router publishes skill.invoke to EventBridge
3. Lambda executes → SQS result
4. Router SQS poller picks up result → formats with Bedrock AI
5. Router reads pending request from DynamoDB (includes service_url for Teams)
6. Router calls Teams API / Telegram API to push response
```

---

## Implementation Tasks

### 1. SSE Endpoint for Dashboard
**File:** `adapters/aws/server.py` (new handler)

New endpoint: `GET /api/events`
- Authenticates via JWT query parameter (SSE doesn't support custom headers)
- Opens chunked HTTP response with `Content-Type: text/event-stream`
- Registers connection in per-instance map keyed by `user_id`
- Sends keepalive every 15s: `: keepalive\n\n`
- On SQS result for this user: sends `event: message\ndata: {...}\n\n`
- Cleans up connection map on disconnect

**File:** `adapters/local/dev_server.py` (new handler)
- Same SSE endpoint for local dev parity
- DirectBus results pushed to SSE instead of returned in HTTP response

**Dashboard JS changes:**
- Open EventSource connection on page load
- On `message` event: render response in chat UI
- `POST /api/chat` returns `{request_id, status: "processing"}` for skill routes
- Show "thinking..." indicator while waiting for SSE result

### 2. EventBridgeBus Adapter
**File:** `adapters/aws/event_bridge_bus.py`

New EventBus implementation for AWS:
- `async publish()` — puts event to EventBridge via boto3, returns immediately
- No `get_result()` — results come through SQS instead
- Event payload: tenant_id, skill_name, params, request_id, reply_channel, reply_target, session_id

### 3. Lambda Skill Handler
**File:** `adapters/aws/lambda_handler.py`

Single Lambda entry point:
- Receives EventBridge event with skill payload
- **Idempotency:** reads pending request from DynamoDB; if `status == completed`, return early
- Loads SkillRegistry (cached across warm invocations)
- Lazy-loads skill worker module and its dependencies at invocation time
- Fetches secrets via SecretsManagerProvider
- Calls `worker_fn(params, secrets)`
- Updates pending request status to `completed` in DynamoDB
- Publishes result + request_id to SQS
- Runtime: Python 3.12, 512 MB, 30s timeout, no provisioned concurrency

### 4. SQS Poller in Router
**File:** `adapters/aws/server.py` (modified)

Background thread added to router:
- Long-polls SQS results queue (WaitTimeSeconds=20)
- On message: resolve pending request from DynamoDB, get channel/conversation context
- **Dashboard:** push to SSE connection (or write to DynamoDB for cross-instance pickup)
- **Teams:** read `service_url` from pending request, call Teams API
- **Telegram:** call Telegram API (stateless, no cached state needed)
- Optionally call Bedrock AI to format the raw skill result before delivery
- Delete message from SQS after successful delivery
- Handles: timeouts, Lambda failures, duplicate messages (idempotent — skip if already delivered)

### 5. Pending Requests Table
**DynamoDB table:** `t3nets-{env}-pending-requests`

Schema:
```
pk: {request_id}
Attributes:
  tenant_id       — which tenant owns this request
  skill_name      — which skill is executing
  channel         — "dashboard" | "teams" | "telegram"
  conversation_id — for saving the turn to conversation history
  reply_target    — channel-specific target (chat_id, conversation reference, user_id)
  service_url     — Teams Bot Framework service URL (null for other channels)
  is_raw          — whether --raw flag was used
  status          — "pending" | "completed" (for Lambda idempotency)
  created_at      — ISO timestamp
TTL: ttl (Unix epoch, 5 min after creation)
```

### 6. Router Code Changes
**File:** `adapters/aws/server.py` (modified)

- `POST /api/chat` behavior change:
  - For Tier 1 (conversational): unchanged — sync response in HTTP body
  - For Tier 2/3 (skill): store pending request in DynamoDB, publish to EventBridge,
    return `{request_id, status: "processing", conversation_id}` immediately
  - SQS poller delivers result asynchronously via SSE
- Feature flag: `USE_ASYNC_SKILLS` env var for incremental rollout
- Remove in-memory state:
  - Teams `_service_urls` cache → store `service_url` in pending requests table
  - Accept ephemeral per-instance `stats` counters (monitoring only, not critical)
- New `GET /api/events` SSE handler (see Task 1)
- New `GET /api/results/{request_id}` polling fallback endpoint

### 7. Terraform Infrastructure

New files:
- `infra/aws/modules/compute/lambda.tf` — Lambda function, IAM role (no provisioned concurrency)
- `infra/aws/modules/compute/eventbridge.tf` — Event bus, rule, DLQ
- `infra/aws/modules/compute/sqs.tf` — Results queue, DLQ
- `infra/aws/modules/data/pending_requests.tf` — DynamoDB table

New API Gateway route:
- `GET /api/events` — public route (JWT in query param, validated server-side) for SSE

IAM changes:
- ECS task role: add `events:PutEvents`, `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`
- ECS task role: add DynamoDB access to pending-requests table
- Lambda role: add `secretsmanager:GetSecretValue`, `dynamodb:GetItem`, `dynamodb:UpdateItem`, `sqs:SendMessage`

### 8. Local Development
- DirectBus stays for local dev (no Lambda/EventBridge locally)
- Feature flag `USE_ASYNC_SKILLS=false` keeps sync behavior in local dev
- SSE endpoint works locally too — DirectBus pushes results to SSE instead of returning in HTTP body
- Can test async locally with moto (AWS mocking) in future

---

## Migration Strategy

### Phase A: Deploy infrastructure (no behavior change)
1. Terraform: create Lambda, EventBridge rule, SQS queues, pending requests table
2. Add SSE API Gateway route
3. Deploy router with feature flag `USE_ASYNC_SKILLS=false`
4. Verify everything still works with DirectBus

### Phase B: Enable SSE + async dashboard
1. Deploy updated dashboard JS with EventSource support
2. Set `USE_ASYNC_SKILLS=true` in ECS task environment
3. Router starts using EventBridgeBus + SQS poller + SSE delivery
4. Monitor: CloudWatch metrics, ECS logs, SQS DLQ depth, SSE connection count
5. Rollback: flip flag back to `false` (dashboard falls back to sync HTTP)

### Phase C: Horizontal scaling
1. Increase ECS desired_count to 2+
2. Verify no message loss or duplicate responses across instances
3. Test SSE reconnect behavior (client connects to different instance after reconnect)
4. Load test with concurrent messages across channels

---

## Design Decisions

| Decision | Why |
|----------|-----|
| Single Lambda vs per-skill | Simpler infra, easier deployment, one IAM role |
| Lazy-load skill dependencies | Reduces cold start time without provisioned concurrency cost |
| No provisioned concurrency | Skills already take seconds (external API calls); cold start adds 1-3s, acceptable for dev. Saves ~$15/mo |
| SQS for results (not DynamoDB polling) | Natural queue semantics, built-in retry, efficient long-polling |
| WaitTimeSeconds=20 | Maximum SQS long-poll; returns immediately when messages arrive, near-zero cost when idle |
| SSE for dashboard (not WebSocket) | Works through existing HTTP API Gateway, auto-reconnect built in, simpler than WebSocket. One-directional push is sufficient |
| SSE keepalive every 15s | Prevents API Gateway 30s integration timeout from closing the connection |
| Idempotency via DynamoDB | Lambda checks `status` field before executing; safe for EventBridge retries and future write skills |
| Teams service_url in pending requests | Eliminates in-memory `_service_urls` cache that breaks with horizontal scaling |
| DynamoDB for pending requests | Enables horizontal scaling — any router instance handles any result |
| Separate pending-requests table | Cleaner operational boundaries; independent monitoring, TTL cleanup, and throughput |
| Feature flag rollout | Zero-risk deployment, instant rollback |

---

## Key Constraints

- **Worker contract unchanged:** `def execute(params: dict, secrets: dict) -> dict`
- **Cloud-agnostic core:** No Lambda imports in `agent/` — only in `adapters/aws/`
- **DirectBus preserved:** Local dev continues working without AWS services
- **Channel isolation:** Skills never know which channel the request came from
- **Stateless router:** No in-memory state that would break with multiple instances (except ephemeral SSE connections and stats counters)
- **API Gateway compatibility:** SSE works through existing HTTP API (v2), no WebSocket API needed

---

## Cost Estimate (dev)

| Resource | Monthly Cost |
|----------|-------------|
| Lambda (1000 invocations, 512MB, 5s avg) | ~$0.10 |
| EventBridge (1000 events) | ~$0.01 |
| SQS (1000 messages + long-poll) | ~$0.01 |
| DynamoDB pending requests (PAY_PER_REQUEST) | ~$0.25 |
| **Total new cost** | **~$0.40** |

Existing ECS + NAT stays the same (~$35-50/mo). Removed provisioned concurrency saves ~$15/mo vs original plan.

---

## Files Summary

### New Files
| File | Purpose |
|------|---------|
| `adapters/aws/event_bridge_bus.py` | EventBridge EventBus implementation |
| `adapters/aws/lambda_handler.py` | Lambda entry point for skill execution |
| `infra/aws/modules/compute/lambda.tf` | Lambda function + IAM |
| `infra/aws/modules/compute/eventbridge.tf` | EventBridge bus + rule + DLQ |
| `infra/aws/modules/compute/sqs.tf` | Results queue + DLQ |
| `infra/aws/modules/data/pending_requests.tf` | Pending requests table |

### Modified Files
| File | Change |
|------|--------|
| `adapters/aws/server.py` | SSE endpoint, SQS poller thread, feature flag, async result handling, pending request writes |
| `adapters/local/dev_server.py` | SSE endpoint for local dev parity, updated chat handler |
| `agent/channels/teams.py` | Remove in-memory `_service_urls` cache (service_url now in pending requests) |
| `infra/aws/modules/compute/main.tf` | IAM additions (events, SQS, pending-requests DynamoDB), env vars |
| `infra/aws/modules/compute/variables.tf` | Lambda config vars |
| `infra/aws/modules/compute/outputs.tf` | Queue URLs, Lambda ARN |
| `infra/aws/modules/api/main.tf` | New public route: `GET /api/events` for SSE |
| `infra/aws/modules/data/outputs.tf` | Pending requests table name + ARN |
| Dashboard HTML/JS | EventSource client, "processing" state, fallback polling |

### Unchanged
| File | Why |
|------|-----|
| `agent/interfaces/event_bus.py` | Interface stays the same |
| `agent/skills/*` | Worker contract unchanged |
| `agent/router/*` | Routing logic unchanged |
| `agent/channels/telegram.py` | Already stateless (no cached state) |
| `agent/channels/dashboard.py` | Response delivery moves to SSE, but adapter interface unchanged |
| `adapters/local/direct_bus.py` | Kept for local dev |

---

## Open Questions

1. **SSE cross-instance delivery:** When a user's SSE connection is on Instance A but the SQS poller on Instance B picks up their result — how to deliver? Options: (a) write to DynamoDB, client polls as fallback, (b) use SNS to fan out to all instances, (c) use ElastiCache pub/sub. For Phase B (single instance), this is not an issue. Defer to Phase C.
2. **Conversation turn saving:** Currently the server saves the conversation turn after formatting the response. With async, the SQS poller must save the turn. Need to ensure the poller has access to the full conversation context (original user message + skill result + formatted response).
3. **Error UX:** When a skill fails (Lambda timeout, integration error), how should the dashboard display the error? SSE can send `event: error\ndata: {...}` but the UX needs design.
