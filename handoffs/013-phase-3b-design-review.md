# Handoff: Phase 3b Async Skills — Design Review & Plan Revision

**Date:** 2026-02-28
**Status:** Planning Complete — Ready for Implementation
**Roadmap Item:** Phase 3b: Async Skill Execution (EventBridge + Lambda + SQS)

## What Was Done

Reviewed the existing Phase 3b design plan (`docs/plan-phase-3b-async-skills.md`) against the actual codebase to identify architectural pitfalls. Found 6 issues, consulted with the user on resolution options, and rewrote the design doc incorporating all decisions. No code was written — this is purely a design revision session.

## Key Files Changed

| File | What Changed |
|------|-------------|
| `docs/plan-phase-3b-async-skills.md` | Full rewrite with 6 design fixes, new SSE section, updated file lists, revised cost estimate |

## Architecture & Design Decisions

### 6 Pitfalls Found and Resolved

1. **Dashboard has no async response path** — The original plan assumed all channels could receive async push responses, but the dashboard uses synchronous HTTP POST/response. Resolved: add **SSE (Server-Sent Events)** endpoint (`GET /api/events`). Chose SSE over WebSocket because the existing API Gateway is HTTP v2 (doesn't support WebSocket), and SSE works over regular HTTP with auto-reconnect.

2. **Teams `_service_urls` in-memory cache breaks horizontal scaling** — `TeamsAdapter` stores `serviceUrl` in a per-instance dict. If the SQS poller on Instance B picks up a result that Instance A received, it can't send the Teams response. Resolved: persist `service_url` in the pending-requests DynamoDB table alongside other reply context.

3. **SQS long-poll interval too short** — Original plan used `WaitTimeSeconds=1`, burning ~60 API calls/min when idle. Changed to `WaitTimeSeconds=20` (SQS maximum). Returns immediately when messages arrive, near-zero cost when idle.

4. **Lambda retry risk without idempotency** — EventBridge async invocation retries up to 2 times on failure. Without idempotency, write skills could execute 3 times. Resolved: Lambda reads pending request `status` from DynamoDB before executing; marks `completed` after. Safe for retries.

5. **Pending requests table** — User chose to keep as a separate DynamoDB table (not merged into existing tenants table) for cleaner operational boundaries.

6. **Lambda cold start + dependency bloat** — Single Lambda bundles all skill dependencies. Instead of provisioned concurrency ($15/mo), user chose **lazy-loading** — skill worker modules and their dependencies are imported at invocation time, not at module level. Saves $15/mo, cold start acceptable for dev since skills already take seconds.

### Key Design Choice: SSE over WebSocket

The API Gateway is `protocol_type = "HTTP"` (v2), which does **not** support WebSocket upgrade. Options were: (a) separate WebSocket API Gateway, (b) direct ALB exposure, (c) SSE. SSE was chosen because it works through existing HTTP infrastructure, auto-reconnects natively, and only requires server→client push (which is all we need). Keepalive comments every 15s prevent the API Gateway 30s integration timeout.

## Current State

- **What works:** The design doc is fully revised and ready for implementation
- **What doesn't yet:** No code has been written — this was a design-only session
- **Known issues:** Three open questions documented at the bottom of the plan:
  1. SSE cross-instance delivery (deferred to Phase C — single instance in Phase B)
  2. Conversation turn saving from the SQS poller context
  3. Error UX design for skill failures in the dashboard

## How to Pick Up From Here

The revised plan at `docs/plan-phase-3b-async-skills.md` has a clear implementation task list (8 tasks) and a 3-phase migration strategy. Suggested implementation order:

1. **Start with Terraform** (Task 7) — create Lambda, EventBridge, SQS, pending-requests table. Deploy with feature flag OFF.
2. **SSE endpoint** (Task 1) — add `GET /api/events` to both AWS and local servers. Update dashboard JS.
3. **EventBridgeBus adapter** (Task 2) — new `EventBus` implementation using boto3 EventBridge client.
4. **Lambda handler** (Task 3) — single entry point with idempotency check and lazy-loading.
5. **SQS poller** (Task 4) — background thread in router with `WaitTimeSeconds=20`.
6. **Pending requests writes** (Task 5) — DynamoDB table for in-flight tracking.
7. **Router code changes** (Task 6) — wire everything together, feature flag.
8. **Local dev parity** (Task 8) — SSE works locally, DirectBus stays for sync.

## Dependencies & Gotchas

- **API Gateway route needed:** `GET /api/events` must be added as a public route in `infra/aws/modules/api/main.tf` (JWT validated server-side via query param, not header — SSE doesn't support custom headers).
- **API Gateway 30s timeout:** SSE connections will be dropped if no data sent for 30s. The keepalive comment (`: keepalive\n\n`) every 15s is critical.
- **Cross-instance SSE in Phase C:** When scaling to 2+ ECS tasks, a user's SSE connection may be on a different instance than the SQS poller that picks up their result. This is explicitly deferred — Phase B runs on a single instance. Phase C options: SNS fan-out, ElastiCache pub/sub, or DynamoDB polling fallback.
- **Dashboard JS is currently sync:** The chat frontend awaits the HTTP response body. The JS must be updated to: (a) open EventSource, (b) handle `POST /api/chat` returning `{status: "processing"}`, (c) render results from SSE events. This is a meaningful frontend change.
- **Teams adapter refactor:** `agent/channels/teams.py` has an in-memory `_service_urls` dict (line 46). This needs to be removed; the service URL will come from the pending-requests DynamoDB record instead. The adapter's `send_response()` method will need a way to receive the service_url — either via the `OutboundMessage` model or a new parameter.
