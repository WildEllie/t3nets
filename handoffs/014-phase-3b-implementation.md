# Handoff: Phase 3b — Async Skill Execution Implementation

**Date:** 2026-02-28
**Status:** Partially Complete (code done, deployment & e2e verification pending)
**Roadmap Item:** Phase 3b: Async Skill Execution (EventBridge + Lambda + SQS)

## What Was Done

Implemented the full async skill execution pipeline: EventBridge → Lambda → SQS → Router → SSE. The router publishes skill invocations to EventBridge, Lambda executes them with idempotency guarantees, results flow back via SQS, and the router delivers them to the dashboard via Server-Sent Events. A `USE_ASYNC_SKILLS` feature flag allows instant rollback to the synchronous DirectBus path. All Terraform infrastructure, Python code, and tests are complete — deployment and end-to-end verification remain.

## Key Files Changed

| File | What Changed |
|------|-------------|
| `agent/sse.py` | **New** — Thread-safe SSEConnectionManager (shared by local + AWS servers) |
| `adapters/aws/event_bridge_bus.py` | **New** — EventBus implementation using boto3 EventBridge `put_events` |
| `adapters/aws/pending_requests.py` | **New** — DynamoDB store for in-flight requests (PendingRequest dataclass, conditional update for idempotency) |
| `adapters/aws/lambda_handler.py` | **New** — Lambda entry point: idempotency check → load skill → execute → mark completed → SQS |
| `adapters/aws/sqs_poller.py` | **New** — Background daemon thread: long-polls SQS (20s), routes results via callback |
| `adapters/aws/result_router.py` | **New** — SQS callback: reads pending request, routes to dashboard (SSE), Teams, or Telegram |
| `adapters/aws/server.py` | Modified — imports new modules, `USE_ASYNC_SKILLS` flag, `init()` wires EventBridge+SQS+Pending when enabled, `_handle_chat()` branches to `_handle_async_skill()` for skill invocations |
| `adapters/local/dev_server.py` | Modified — ThreadedHTTPServer, SSE endpoint, keepalive thread |
| `adapters/local/chat.html` | Modified — EventSource SSE connection, `pendingRequests` tracking, async result rendering |
| `infra/aws/modules/compute/lambda.tf` | **New** — Lambda function + IAM role (least-privilege) |
| `infra/aws/modules/compute/eventbridge.tf` | **New** — Custom bus, rule, Lambda target, DLQ |
| `infra/aws/modules/compute/sqs.tf` | **New** — Results queue (long-poll 20s) + DLQ |
| `infra/aws/modules/data/pending_requests.tf` | **New** — DynamoDB table (PAY_PER_REQUEST, TTL) |
| `infra/aws/modules/compute/main.tf` | Modified — new variables, IAM policies, env vars, outputs |
| `infra/aws/modules/data/main.tf` | Modified — new outputs |
| `infra/aws/modules/api/main.tf` | Modified — SSE route `GET /api/events` (no JWT authorizer, validated server-side) |
| `infra/aws/main.tf` | Modified — passes pending_requests variables to compute |
| `infra/aws/variables.tf` | Modified — `use_async_skills` bool (default false) |
| `scripts/deploy.sh` | Modified — Lambda packaging + deploy step (gated by `USE_ASYNC_SKILLS`) |
| `tests/test_sse.py` | **New** — 10 unit tests for SSEConnectionManager |
| `tests/test_async_skills.py` | **New** — 9 unit tests for SQS poller, result router, pending requests |
| `docs/plan-phase-3b-async-skills.md` | Rewritten — incorporates all 6 design decisions from review |
| `docs/ROADMAP.md` | Updated — Phase 3b tasks marked complete |

## Architecture & Design Decisions

### Why SSE over WebSocket
API Gateway HTTP v2 doesn't support WebSocket. SSE is simpler (unidirectional push), works through API Gateway, and the keepalive (15s) prevents the 30s idle timeout.

### Why DynamoDB for pending requests (not Redis)
Keeps the stack consistent (already using DynamoDB). PAY_PER_REQUEST pricing means $0 at low volume. TTL auto-cleans expired requests. The conditional update on `status` field provides exactly-once idempotency for Lambda retries.

### Why lazy-loaded skill dependencies (not provisioned concurrency)
Provisioned concurrency costs ~$15/mo for a single instance. Cold starts are acceptable for dev/staging. Skills are loaded via `SkillRegistry.load_from_directory()` on first Lambda invocation and cached in the global scope for subsequent warm invocations.

### Feature flag pattern
`USE_ASYNC_SKILLS=true/false` env var, checked once at import time. When `false`, the server doesn't import EventBridge/SQS clients or start the poller thread — zero overhead. When `true` but env vars are missing, it falls back to DirectBus with a warning log.

### SQS long-polling (20s)
Original plan had `WaitTimeSeconds=1` which would cause ~43,200 API calls/day idle. Changed to 20 (maximum) — near-zero cost when idle, ~4,320 calls/day.

### Teams serviceUrl persistence
The Teams Bot Framework requires the `serviceUrl` from the original webhook to send proactive messages. Stored in the DynamoDB pending-requests table alongside the request, so any router instance can pick up the SQS result and reply.

## Current State

- **What works:** All code compiles, 19 unit tests pass (10 SSE + 9 async), Terraform validates, deploy script has Lambda packaging
- **What doesn't yet:** Not deployed to AWS. End-to-end flow not verified. Teams/Telegram async routing has TODO stubs (Phase 3c)
- **Known issues:**
  - `result_router.py` Teams/Telegram routes are stubs — they log but don't actually send messages. Needs proactive messaging implementation.
  - Lambda handler calls `asyncio.get_event_loop().run_until_complete()` for the `secrets.get()` call since the SecretsProvider interface is async but Lambda handler is sync. Works but could be cleaner.
  - The `_handle_async_skill` in server.py generates `import uuid` at call time — could be moved to module-level import.

## How to Pick Up From Here

1. **Deploy infrastructure:**
   ```bash
   cd infra/aws
   terraform apply -var-file=environments/dev.tfvars -var="use_async_skills=true"
   ```
   This creates the Lambda function (with placeholder ZIP), EventBridge bus, SQS queues, and pending-requests table.

2. **Deploy code:**
   ```bash
   USE_ASYNC_SKILLS=true ./scripts/deploy.sh
   ```
   This packages the Lambda, updates it, then deploys the ECS container with the async env vars.

3. **Verify end-to-end:**
   - Open dashboard, connect to SSE (`/api/events`)
   - Send a skill invocation (e.g., "ping")
   - Confirm: router → EventBridge → Lambda → SQS → router → SSE → dashboard
   - Check CloudWatch logs for Lambda execution, DynamoDB for pending request lifecycle

4. **Test rollback:** Set `use_async_skills=false` in tfvars, re-deploy. Verify DirectBus still works.

5. **Horizontal scaling test:** Run 2+ ECS tasks, send concurrent skill invocations, verify no duplicate responses.

## Dependencies & Gotchas

- **Terraform must be applied before deploy.sh** — the Lambda function and SQS queue must exist before the deploy script can update function code.
- **Lambda ZIP placeholder:** Terraform creates the Lambda with a placeholder `lambda_placeholder.zip`. The real code is deployed by `scripts/deploy.sh`. If the placeholder doesn't exist at `terraform apply` time, create it: `echo "placeholder" | zip > infra/aws/modules/compute/lambda_placeholder.zip`
- **SSE route has no JWT authorizer** in API Gateway — the token is passed as a query parameter and validated server-side. This is intentional (EventSource API doesn't support headers).
- **`asyncio.run()` vs `get_event_loop()`:** The server uses `asyncio.run()` (via `_run_async()`), but Lambda uses `get_event_loop().run_until_complete()`. Both work because there's only one thread per call, but be careful not to mix them.
- **DynamoDB TTL delay:** DynamoDB TTL deletes can take up to 48 hours. Don't rely on TTL for real-time cleanup — it's just a safety net. The 5-minute retention on the SQS queue is the real guard.
