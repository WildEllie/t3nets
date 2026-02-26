# Handoff: Microsoft Teams Channel Adapter

**Date:** 2026-02-26
**Status:** Completed
**Roadmap Item:** Phase 3 — First External Channel (Teams channel adapter)

## What Was Done

Implemented the Microsoft Teams channel adapter, enabling T3nets to receive messages from Teams users and respond via the Bot Framework REST API. This is the first external channel beyond the built-in web dashboard. The adapter uses the Bot Framework REST API directly (no SDK dependency) for minimal footprint. Both AWS and local dev server webhook endpoints are implemented, along with Terraform API Gateway routing and Teams integration config in the settings UI.

## Key Files Changed

| File | What Changed |
|------|-------------|
| `agent/channels/teams.py` | **New** — TeamsAdapter implementing ChannelAdapter (parse_inbound, send_response, validate_webhook, typing indicator, @mention stripping) |
| `agent/channels/teams_auth.py` | **New** — BotFrameworkAuth (JWT validation via Microsoft OpenID keys, OAuth token acquisition for outbound messages, key/token caching) |
| `adapters/aws/server.py` | Added Teams webhook route (`POST /api/channels/teams/webhook`), `_handle_teams_webhook()`, `_handle_teams_message()`, `_handle_teams_bot_added()`, `_get_teams_adapter()`, Teams integration schema, ChannelType import |
| `adapters/local/dev_server.py` | Added Teams webhook route, `_handle_teams_webhook()`, `_handle_teams_message_local()`, `_get_teams_adapter_local()`, Teams integration schema |
| `infra/aws/modules/api/main.tf` | Added public route `POST /api/channels/teams/webhook` (no JWT authorizer — Microsoft uses its own auth) |
| `tests/test_teams_adapter.py` | **New** — 20 unit tests covering TeamsAdapter, BotFrameworkAuth, TokenCache, SigningKeyCache |

## Architecture & Design Decisions

**No SDK dependency:** Instead of using the heavyweight `botbuilder-python` SDK, we use the Bot Framework REST API directly via `urllib.request`. The API is simple — just HTTP POST with Bearer tokens. This avoids pulling in the entire Bot Framework SDK (dozens of packages) for what's essentially 2 API calls (validate JWT + POST response).

**JWT validation approach:** Incoming webhooks carry a Microsoft-signed JWT in the Authorization header. We fetch signing keys from Microsoft's OpenID metadata endpoint and validate with PyJWT if available, falling back to unverified decode for dev/testing. Keys are cached for 24 hours; tokens for outbound calls are cached until 5 minutes before expiry.

**Tenant resolution from webhook:** When a Teams message arrives, we resolve the T3nets tenant by looking up the bot's App ID in the DynamoDB channel mapping (`CHANNEL#teams#{app_id}`). If no mapping exists yet, we scan all tenants' Teams integration configs and create the mapping on first match for faster future lookups.

**Synchronous processing (DirectBus):** We kept the synchronous DirectBus for Teams rather than implementing EventBridge/SQS async flow. Teams tolerates ~15 second response times, and our skills execute in 1-5 seconds. The adapter sends a typing indicator immediately while processing continues synchronously.

**Service URL caching:** Bot Framework requires responses to be sent back to the `serviceUrl` included in each inbound Activity. The adapter caches these per-conversation since they don't change.

**@mention stripping:** In group chats and channels, Teams prepends `<at>BotName</at>` to the message text. The adapter strips only the bot's own mention, preserving any other @mentions in the text.

## Current State

- **What works:**
  - TeamsAdapter parses Bot Framework Activity payloads correctly
  - Bot mention stripping for group chats
  - JWT header/payload decoding
  - Token caching (outbound and signing keys)
  - Webhook endpoints in both AWS and local dev servers
  - Full routing pipeline (conversational → rule-matched → AI) for Teams messages
  - Conversation history persists per Teams conversation (prefixed with `teams-`)
  - Welcome message when bot is added to a team/chat
  - Teams integration config form in settings UI (renders from INTEGRATION_SCHEMAS)
  - API Gateway route for Teams webhook (Terraform)
  - 20 unit tests passing

- **What doesn't yet:**
  - No live end-to-end test (requires Azure Bot registration)
  - PyJWT not installed in the base environment — JWT signature verification falls back to unsafe decode
  - Channel mapping auto-creation hasn't been tested against real DynamoDB
  - No Adaptive Card rich responses yet (just plain text)

- **Known issues:**
  - `_get_teams_adapter()` creates a new TeamsAdapter instance per request — could be cached
  - If PyJWT is not installed, JWT signatures are NOT verified (logged as warning)
  - `urlopen` in `send_response` is synchronous and blocks — acceptable for now but could use `aiohttp` later

## How to Pick Up From Here

1. **Azure Bot Registration:** Go to Azure Portal → Bot Services → Create Azure Bot. Get App ID + Secret. Set messaging endpoint to `https://{api-gateway-url}/api/channels/teams/webhook`.

2. **Install PyJWT:** Add `PyJWT[crypto]` to requirements for production JWT validation: `pip install PyJWT[crypto]`

3. **Test end-to-end:** Save Teams credentials in T3nets settings, message the bot in Teams, verify sprint status response comes back.

4. **Add to Dockerfile:** Ensure `PyJWT[crypto]` is in the container's requirements.

5. **Adaptive Cards:** For richer responses (tables, buttons), implement `_build_adaptive_card()` in TeamsAdapter and use `rich_content` field on OutboundMessage.

6. **Phase 3 completion:** The roadmap milestone is "Team member asks sprint status in Teams, gets answer." This is architecturally complete but needs a live Azure Bot registration to verify.

## Dependencies & Gotchas

- **PyJWT[crypto]** is needed for production JWT validation. Without it, the auth module falls back to decoding without signature verification (insecure, logged as warning).
- **Bot Framework Emulator** can be used for local testing: point it at `http://localhost:8080/api/channels/teams/webhook`. It simulates Teams messages without needing Azure.
- **Service URL varies by region** — Microsoft uses different URLs (e.g., `smba.trafficmanager.net` for most, `smba.infra.gcc.teams.microsoft.com` for GCC). The adapter handles this by caching whatever URL comes in each Activity.
- **Rate limits:** Bot Framework has its own rate limits (~1 reply per second per conversation). Our API Gateway throttling adds another layer.
- **Teams bot manifest:** To install the bot in Teams, you need an app manifest (JSON file) uploaded via Teams Admin Center or App Studio. This is a Teams-side config, not T3nets code.
