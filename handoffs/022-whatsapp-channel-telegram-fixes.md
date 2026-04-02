# Handoff: WhatsApp Channel + Telegram Webhook Fixes

**Date:** 2026-04-03
**Status:** Partially Complete
**Roadmap Item:** WhatsApp adapter (docs/ROADMAP.md)

## What Was Done

Added WhatsApp as a messaging channel via Whapi.cloud API, mirroring the Telegram adapter pattern. Fixed a critical Telegram bug where webhook errors caused infinite message retry loops (24+ hours of duplicate messages). Fixed missing audio on Telegram by enabling the async Lambda path (`USE_ASYNC_SKILLS=true` via Terraform) and adding audio handling to the sync fallback path.

## Key Files Changed

| File | What Changed |
|------|-------------|
| `agent/channels/whatsapp.py` | **NEW** — Full WhatsApp adapter: parse_inbound, send_response, _send_audio (via presigned URL), validate_webhook, register_webhook, get_health, is_message_event (filters from_me) |
| `adapters/shared/server_utils.py` | Added `"whatsapp"` integration schema (api_token + webhook_secret + setup_steps) |
| `adapters/aws/server.py` | WhatsApp webhook handler + message processor + route registration; integration save/test handlers; `_register_whatsapp_webhook()` with auto-generated secret; **Fixed all channel webhooks to always return 200** (fire-and-forget processing); **Added audio result handling in sync path** for both Telegram and WhatsApp |
| `adapters/aws/result_router.py` | Added `_route_whatsapp()` for async result delivery — mirrors Telegram routing with audio_url support |
| `adapters/local/settings.html` | Added WhatsApp channel definition (icon, setup steps, brand color) |
| `adapters/local/theme.css` | Added `--channel-whatsapp: #25D366` CSS variable (both light and dark themes) |
| `infra/aws/modules/api/main.tf` | Added public API Gateway route for WhatsApp webhook (bypasses JWT authorizer) |

## Architecture & Design Decisions

1. **Whapi.cloud over whatsapp-web.js**: User rejected Puppeteer-based approach. Whapi.cloud is a hosted REST API — user scans QR on their dashboard, pastes token in t3nets. Same pattern as Telegram/BotFather.

2. **Audio via URL only (WhatsApp)**: Whapi.cloud's `/messages/voice` accepts a `media` URL and auto-converts WAV to OGG/Opus. No base64 upload path. If only base64 available, falls back to text-only.

3. **from_me filter**: Whapi.cloud forwards the bot's own outgoing messages back to the webhook (unlike Telegram). Without `from_me: true` filtering, this creates an infinite loop where the bot responds to itself.

4. **Always-200 webhooks**: Both Telegram and WhatsApp webhooks now always return HTTP 200, even on internal errors. Message processing runs via `_fire_and_forget()`. This prevents retry loops — Telegram retries indefinitely on non-200, which caused 24+ hours of duplicate messages.

5. **Sync path audio handling**: With `USE_ASYNC_SKILLS=false` (which was the state before this session's Terraform fix), the DirectBus sync path was sending audio results through AI formatting, losing the actual audio data. Added explicit `result.get("type") == "audio"` check before AI formatting in both Telegram and WhatsApp handlers.

6. **Channel mapping**: Same pattern as Telegram — `sha256(api_token)[:16]` stored in tenant `channel_mappings` dict in DynamoDB. Webhook URL includes the hash: `/api/channels/whatsapp/webhook/{token_hash}`.

## Current State

- **What works:**
  - Telegram: voice messages delivered correctly via async Lambda path (sendAudio with multipart WAV upload)
  - WhatsApp: adapter built, integration schema in UI, webhook handler + result router + API Gateway route all deployed
  - Retry loop: fixed for both channels
  - `USE_ASYNC_SKILLS=true` now set in ECS via Terraform

- **What doesn't yet:**
  - WhatsApp not tested e2e (user disconnected personal WhatsApp from Whapi, needs burner account)
  - WhatsApp voice: untested — Whapi.cloud `/messages/voice` with S3 presigned URL needs real-world test
  - Audio not persisted to conversation history DB (pre-existing issue, not introduced here)

- **Known issues:**
  - First Telegram message after deploy sometimes falls through to AI instead of rule match (cold-start? rule engine not loaded yet?)
  - `ChannelType.WHATSAPP` already existed in the enum — no change needed, but confirms this was on the roadmap

## How to Pick Up From Here

1. **WhatsApp e2e test**: Get a burner phone/SIM, set up WhatsApp, connect to Whapi.cloud, paste token in t3nets Settings, send "Say שלום" from another phone
2. **WhatsApp voice test**: Verify `/messages/voice` with presigned URL works (Whapi auto-converts WAV to OGG)
3. **Webhook secret persistence**: When saving WhatsApp integration, the auto-generated webhook_secret is set in `creds` dict after `secrets.put()` already saved the original body. The secret IS registered with Whapi but NOT stored in Secrets Manager. Fix: save again after generating, or generate before the first save.
4. **Conversation history for audio**: Audio messages (both channels) should be persisted to conversation history. Currently text is saved but audio_url/audio_b64 is not.
5. **Dashboard audio**: Verify dashboard chat still works with audio after the sync-path changes.

## Dependencies & Gotchas

- **Whapi.cloud Cloudflare**: Requests without `User-Agent` header get blocked with 403/1010. The adapter includes `User-Agent: T3nets/1.0` in all requests.
- **USE_ASYNC_SKILLS**: Was `false` in ECS despite Terraform having `true` in dev.tfvars. Root cause: user's previous `terraform apply` was targeted and didn't update the ECS task definition. Fixed by running full `terraform apply` + redeploy.
- **DirectBus fallback**: Even with async enabled, the sync audio handling code is valuable as a fallback when EventBridge/SQS env vars are missing.
- **Telegram sendAudio vs sendVoice**: We use `sendAudio` which sends as an audio file (with player). `sendVoice` would send as a voice bubble (OGG/Opus only). Current approach works but could be changed if voice bubbles are preferred.
- **Telegram auto-play**: Telegram plays audio messages sequentially. With many messages in a chat, it sounds like one long recording. Not a bug.
