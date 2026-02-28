# Handoff: API Gateway WebSocket API for Real-Time Push

**Date:** 2026-02-28
**Status:** Code complete, deployment pending
**Roadmap Item:** Phase 3b: Async Skill Execution — WebSocket transport

## What Was Done

Replaced SSE with API Gateway WebSocket API for delivering async skill results to the browser on AWS. SSE connections were being killed by API Gateway HTTP v2's hard 30-second integration timeout (503 errors). The WebSocket API routes to the **existing ECS container** via the existing VPC Link and ALB — no new Lambda or DynamoDB. Connection tracking stays in-memory on ECS (same pattern as SSE). SSE is preserved for local development.

## Why WebSocket Instead of SSE

API Gateway HTTP v2 has a hard 30-second integration timeout that cannot be changed. SSE connections get 503'd after 30 seconds. WebSocket API Gateway has no such timeout — connections persist for up to 2 hours idle (configurable). The WebSocket API sends HTTP POST requests to ECS for `$connect`/`$disconnect`/`$default`, so ECS never handles raw WebSocket protocol.

## Key Files Changed

| File | What Changed |
|------|-------------|
| `adapters/aws/ws_connections.py` | **New** — Thread-safe in-memory WebSocket connection registry + `post_to_connection()` push via API Gateway Management API. Mirrors `SSEConnectionManager.send_event()` interface. |
| `adapters/aws/result_router.py` | **Modified** — Replaced `SSEConnectionManager` type hint with `PushClient` Protocol. Constructor param renamed to `push_client`. Both SSE and WS managers satisfy the Protocol. |
| `adapters/aws/server.py` | **Modified** — Conditional push transport init (WS when `WS_API_ENDPOINT` is set, SSE fallback). WebSocket route handlers dispatched via `X-WS-Route` header. Dashboard HTML config injection (`window.__CONFIG__`). Management endpoint derived at runtime from `WS_API_ENDPOINT`. |
| `adapters/local/chat.html` | **Modified** — Dual-transport: `connectRealtime()` chooses WebSocket or SSE. Shared `handleIncomingMessage()` handler used by both. WebSocket auto-reconnects on close (2s delay). |
| `infra/aws/modules/websocket/main.tf` | **New** — WebSocket API Gateway (API, integration via VPC Link, 3 routes, auto-deploy stage). Passes route key and connection ID to ECS via `request_parameters` headers. |
| `infra/aws/main.tf` | **Modified** — Websocket module (gated by `use_async_skills`), IAM policy for `execute-api:ManageConnections` attached to ECS task role. |
| `infra/aws/outputs.tf` | **Modified** — Added `ws_endpoint` output. |
| `infra/aws/modules/compute/main.tf` | **Modified** — `ws_api_endpoint` variable + ECS env var, exported `ecs_task_role_id` for IAM attachment from root module. |

## Architecture

```
Browser ──WebSocket──→ API Gateway WebSocket API
                         ├─ $connect    ─→ VPC Link → ALB → ECS (register connection_id)
                         ├─ $disconnect ─→ VPC Link → ALB → ECS (remove connection_id)
                         └─ $default    ─→ VPC Link → ALB → ECS (no-op, 200)

Skill result (last mile):
  SQS → ECS SQS Poller → AsyncResultRouter
    → push_client.send_event(user_key, "message", data)
    → WebSocketConnectionManager.post_to_connection() → API Gateway → Browser
```

## Design Decisions

### 1. Header-Based Route Dispatch (Not Path-Based)
WebSocket API Gateway with VPC_LINK HTTP_PROXY integration sends all routes to the same ALB path. ECS differentiates routes via the `X-WS-Route` header (set by `request_parameters` in the integration). This avoids needing separate integrations per route.

### 2. Management Endpoint Derived at Runtime
`WS_MANAGEMENT_ENDPOINT` is derived from `WS_API_ENDPOINT` by replacing `wss://` with `https://`. This eliminates a second env var and avoids a Terraform circular dependency (websocket module needs compute's VPC Link; compute's ECS task definition needs websocket's endpoint).

### 3. IAM Policy in Root Module (Not Compute)
The `execute-api:ManageConnections` IAM policy is attached in `main.tf` (not inside the compute module) because the websocket module's ARN is only available after compute creates the VPC Link. Root-level attachment avoids circular module references.

### 4. PushClient Protocol Pattern
`result_router.py` uses a `Protocol` class (`PushClient`) instead of importing either concrete manager. Both `SSEConnectionManager` and `WebSocketConnectionManager` satisfy it with `send_event()` and `connection_count`. The result router doesn't know which transport is active.

### 5. No Lambda, No DynamoDB for Connections
Connections are tracked in-memory on ECS. If ECS restarts, clients auto-reconnect via WebSocket `onclose` handler (2-second retry). This trades durability for simplicity — acceptable since connection state is ephemeral.

## Deployment Steps

1. `cd infra/aws && terraform apply -var-file=environments/dev.tfvars` — creates WebSocket API, routes, integration, stage, IAM policy
2. Set `WS_API_ENDPOINT` env var on ECS (Terraform handles this via the compute module variable)
3. `./scripts/deploy.sh` — redeploy ECS with new code
4. Verify: `wscat -c "wss://xxx.execute-api.region.amazonaws.com/prod?token=<jwt>"`

## What's NOT Done

- **End-to-end deployment verification** — needs `terraform apply` + `deploy.sh` with `USE_ASYNC_SKILLS=true`
- **Load testing** — verify concurrent WebSocket connections under load
- **Multi-tab testing** — verify same user across multiple tabs receives push in all tabs
- **Horizontal scaling** — verify connection routing works with 2+ ECS tasks (in-memory state means each task tracks its own connections; a user must reconnect to the task that handles their SQS message)

## Known Limitations

- **Single-task affinity**: In-memory connection tracking means a user's WebSocket connection is on one specific ECS task. If the SQS message is consumed by a different task, push delivery fails. For horizontal scaling, connection state should move to DynamoDB or Redis. This is fine for single-task deployments (current setup).
- **No auth on $connect**: JWT is decoded but not cryptographically verified on WebSocket connect (same as current SSE behavior). Full Cognito JWT verification would require JWKS fetching.
