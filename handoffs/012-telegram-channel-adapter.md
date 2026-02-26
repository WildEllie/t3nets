# Handoff: Telegram Channel Adapter + Settings Channels Tab

**Date:** 2026-02-26
**Status:** Completed
**Roadmap Item:** Phase 3 — External Channels (Telegram adapter, Channels tab)

## What Was Done

Implemented a Telegram channel adapter using the Telegram Bot API, plus a new "Channels" tab in the settings dashboard. The adapter handles inbound webhook updates, command parsing (e.g., `/status` → "sprint status"), and outbound message delivery with Markdown support. The Channels tab provides inline setup instructions guiding admins through BotFather bot creation, with a "Test Connection" button for verifying credentials.

## Key Files Changed

| File | What Changed |
|------|-------------|
| `agent/channels/telegram.py` | **New** — TelegramAdapter implementing ChannelAdapter (parse_inbound, send_response, validate_webhook, register_webhook, get_bot_info) |
| `adapters/aws/server.py` | Added Telegram webhook route, handler, integration schema, `_test_telegram()` endpoint |
| `adapters/local/dev_server.py` | Same additions for local dev server |
| `adapters/local/settings.html` | **New Channels tab** with setup guides, config forms, connection testing for Telegram and Teams |
| `infra/aws/modules/api/main.tf` | Added `POST /api/channels/telegram/webhook/{proxy+}` public route |
| `infra/aws/modules/compute/main.tf` | Added `secretsmanager:TagResource` to ECS task IAM policy |
| `adapters/aws/dynamodb_tenant_store.py` | Fixed `list_tenants()` — changed invalid `Query` to `Scan` |
| `tests/test_telegram_adapter.py` | **New** — 19 unit tests covering parsing, command mapping, webhook validation, edge cases |
| `docs/ROADMAP.md` | Marked Telegram adapter and Channels tab as completed; added Phase 3b roadmap |
| `CLAUDE.md` | Added Lessons Learned section (DynamoDB, API Gateway, IAM, Channel Adapters) |

## Architecture & Design Decisions

**Why direct Bot API (no SDK):** Same philosophy as the Teams adapter — minimal dependencies, pure HTTP. The Telegram Bot API is simple (sendMessage, setWebhook, getMe) and doesn't warrant a third-party SDK.

**Command mapping:** Telegram bots conventionally use `/commands`, but T3nets routes natural language. The adapter maps known commands to natural text (`/start` → "help", `/status` → "sprint status", `/releases` → "release notes"). Unknown commands strip the slash and pass through. Non-command text passes unchanged.

**Webhook URL design:** Each tenant's webhook URL includes a hash of their bot token: `/api/channels/telegram/webhook/{token_hash}`. This allows multi-tenant routing — when Telegram sends an update, the server matches the hash against stored tenant configs. The API Gateway route uses `{proxy+}` to accept the variable suffix.

**Webhook secret validation:** Telegram supports a `secret_token` parameter on setWebhook, sent as `X-Telegram-Bot-Api-Secret-Token` header. The adapter validates this when configured, or accepts all requests when no secret is set (for simple setups).

**Channels tab (new UI pattern):** Previously, integrations (Jira, GitHub) were configured under the Skills tab. Channels (Teams, Telegram) are a different category — they're message sources, not skill integrations. A dedicated Channels tab was added with `CHANNEL_DEFS` (parallel to `INTEGRATION_SCHEMAS`) that includes setup steps rendered as numbered instructions.

**Markdown fallback:** `send_response()` first tries sending with `parse_mode=Markdown`. If Telegram rejects it (malformed Markdown), it retries without parse_mode. This prevents message delivery failures from formatting issues.

## Bugs Found & Fixed During Deployment

1. **`list_tenants()` used invalid DynamoDB Query** — `begins_with` on partition key is not supported in `Query`. Changed to `Scan` with filter. Only used in admin/health contexts (not hot path).
2. **Webhook not registered on save** — `register_webhook()` existed on TelegramAdapter but neither server called it. Added auto-registration in `_handle_integrations_post` when saving Telegram credentials.
3. **Channel mapping not saved on credential save** — GSI lookup (`CHANNEL#telegram#{token_hash}`) had no entry. Added `set_channel_mapping()` call alongside webhook registration.
4. **Inconsistent channel mapping key** — `_get_telegram_adapter` used token hash but `_handle_telegram_message` used numeric bot ID (`8717639200`). Unified both to use `sha256(bot_token)[:16]`.
5. **Missing IAM permission** — `secretsmanager:TagResource` needed for `create_secret` with tags. Added to ECS task role.
6. **User record missing attributes** — Manual DynamoDB `put-item` must include `user_id` and `tenant_id` as top-level attributes (not just in pk/sk), since `_item_to_user()` reads them explicitly.

## Current State

- **What works:** Full end-to-end Telegram integration — messages from Telegram bot route through T3nets and responses are delivered back. Settings UI with setup guide and connection testing. 19 passing unit tests. Deployed and verified on AWS.
- **What doesn't yet:** Inline keyboards (buttons) not yet rendered in Telegram responses. Only one tenant can use Telegram at a time (each tenant needs its own bot).
- **Known issues:** `list_tenants()` uses a DynamoDB Scan which won't scale. Consider adding a GSI with a fixed partition key for tenant metadata lookups if tenant count grows significantly.

## How to Pick Up From Here

1. **Deploy to AWS:** `terraform apply` to create the API Gateway route, then `deploy.sh` to push the updated server.
2. **Create a Telegram bot:** Message @BotFather on Telegram, create a bot, copy the token.
3. **Configure in settings:** Open the Settings → Channels tab, paste the bot token, save, and click "Test Connection."
4. **Register webhook:** After saving, the server calls `setWebhook` on Telegram to register the endpoint. Verify in Telegram's getWebhookInfo.
5. **Test:** Send `/status` or a natural language message to the bot in Telegram. Should route through T3nets and return a response.

## Dependencies & Gotchas

- **Bot token security:** The bot token is stored via the integration config system (Secrets Manager on AWS, env/config on local). The token hash in the webhook URL is a SHA-256 prefix — not the full token.
- **Group chat behavior:** In groups, Telegram only forwards messages that mention the bot (`@botname`) or start with `/commands`. The adapter strips the `@botname` suffix from commands in groups.
- **Telegram rate limits:** Telegram allows ~30 messages/second to different chats, 1 message/second to the same chat. The adapter doesn't implement rate limiting — this is fine for typical usage but could matter at scale.
- **No inline keyboards yet:** The adapter has `BUTTONS` in capabilities but `send_response()` doesn't yet render `OutboundMessage.buttons` as Telegram inline keyboards. The infrastructure is ready for a future enhancement.
