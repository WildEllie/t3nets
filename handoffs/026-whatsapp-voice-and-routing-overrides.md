# Handoff: WhatsApp voice reliability + per-sender routing overrides

**Date:** 2026-05-20
**Status:** Done — kuchnir WhatsApp voice synthesis works for any message length; UI for per-sender skill overrides shipped
**Commits:** `a1df22b → 93693c6` (5 commits on `main`)
**Tags:** `sdk/v0.1.2`

---

## What Was Done

End-to-end loop on Ellie's WhatsApp voice line (kuchnir tenant via Whapi.cloud), spanning routing override config, infra plumbing for webhook registration, and finally a long debug to make voice work reliably regardless of message length.

| Block | Outcome |
|---|---|
| **WhatsApp on AWS** | Tenant credentials saved through dashboard; auto-registration silently failed (`no Host header or API_BASE_URL`); fixed by manually setting the Whapi webhook URL and then wiring `API_BASE_URL` permanently. |
| **Custom domain for webhooks** | Confirmed `www.t3nets.dev` (CloudFront alias) already routes `/api/*` to API Gateway → ECS. Swapped Whapi webhook to use the custom domain. |
| **`API_BASE_URL` ECS env var** | Added `api_base_url` variable in `infra/aws/modules/compute/main.tf`, wired from `local.custom_dashboard_url` in `infra/aws/main.tf`. ECS rev 29 picked it up. Future credential saves auto-register webhooks without `Host` header dependency. |
| **Per-sender routing overrides** | New `TenantSettings.channel_routing_overrides: dict[str, str]` field in SDK 0.1.2. Key format `"{channel}:{normalized_sender_id}"` (digits only for whatsapp/telegram). Router checks the override before the rule engine and dispatches the named skill directly. Settings UI panel under **Channels** tab. |
| **Chatterbox EC2** | Started the existing g4dn.xlarge spot instance in t3nets's VPC (`10.0.10.194:8080`). Health green; CUDA-loaded model serves `/synthesize`. Tenant secrets `/t3nets/dev/tenants/{default,kuchnir}/voiceher` already pointed at the right private IP. Router SG `sg-04b2d31a407416315` is on the chatterbox SG's allow-list. |
| **WhatsApp voice reliability** | Hunted a "voice works for long messages, text-only for short messages" intermittence. Root cause: Lambda's `_offload_audio_to_s3` skipped uploading when `audio_b64` length was below `SQS_MAX_BYTES = 250_000`. Below the threshold the result reached `result_router._route_whatsapp` with only `audio_b64`, which Whapi can't deliver (URL-only API) → fell through to text. Fix: removed the threshold; the Lambda now always offloads. |

---

## Files Created

| File | Lines | Purpose |
|------|-------|---------|
| `tests/test_routing_overrides.py` | 49 | Unit tests for `normalize_sender_id` and `lookup_routing_override` — covers WhatsApp suffix stripping, channel mismatch, missing-attribute (pre-0.1.2 settings), Teams pass-through. 8 tests. |

## Files Modified

| File | Change |
|------|--------|
| `sdk/t3nets_sdk/models/tenant.py` | Added `TenantSettings.channel_routing_overrides` field (default empty dict). Bumped `sdk/pyproject.toml` to `0.1.2`. |
| `sdk/CHANGELOG.md` | Entry for 0.1.2 documenting the additive field. |
| `Dockerfile` | Briefly switched to in-tree SDK install (`pip install ./sdk`), reverted to PyPI pin (`"t3nets-sdk>=0.1,<0.2"`) after 0.1.2 published. |
| `adapters/shared/handlers/webhooks.py` | `normalize_sender_id` + `lookup_routing_override` module helpers. `_route_channel_message` checks the override before invoking the rule engine; on a hit, synthesises a `RouteMatch` and feeds it into the existing dispatch path so audio handling, async dispatch, and param enrichment all keep working. |
| `adapters/shared/handlers/settings.py` | `GET /api/settings` returns `channel_routing_overrides`. `POST /api/settings` validates the override dict (string keys with a colon, value must be a known skill). |
| `adapters/local/settings.html` | Channels tab: new "Per-Sender Routing Overrides" panel — table of existing overrides, channel/sender/skill picker with client-side normalisation, add/remove handlers. |
| `infra/aws/modules/compute/main.tf` | Added `api_base_url` variable + `API_BASE_URL` env var entry on the ECS task definition. |
| `infra/aws/main.tf` | Wired `api_base_url = local.custom_dashboard_url` into the compute module. |
| `adapters/aws/lambda_handler.py` | `_offload_audio_to_s3` always uploads (no size guard). Docstring spells out why: WhatsApp/Telegram require a URL, dashboard plays from URL with base64 fallback, so always-offload is correct for every channel. |

---

## Key Decisions

- **Routing override key format `"{channel}:{sender_id}"`** (digits-only for whatsapp/telegram). Flat key was chosen over nested dict because the lookup path is one map access per inbound message. WhatsApp inbound sender is `972…@s.whatsapp.net`; `normalize_sender_id` strips the `@suffix` and any non-digit characters.
- **Override skill receives empty `params`**, then `_enrich_match_params` injects the raw message text into the `text` param if the skill schema declares it. `voice_say` declares `text` so this works for it; any skill that needs other params would have to derive them from the text itself.
- **Settings UI lives in the Channels tab**, not Skills — overrides are a routing concern (who, not what). Decision argued in the exploratory exchange before implementation.
- **SDK in-tree install Dockerfile workaround**: deliberate, time-bounded. Used to ship the field same-day. Reverted to PyPI as soon as 0.1.2 published.
- **`API_BASE_URL` over `Host` header threading**: the alternative (pass the dashboard request `Host` header through `IntegrationHandlers.on_credentials_saved`) would have worked too but required a wider signature change across three handler classes. Env var is contained.
- **Always-offload audio over channel-specific upload** in `_route_whatsapp`: the upload code already exists in the Lambda; doing it in the router would duplicate the logic and run it on every result not just audio. The dashboard pays a small latency cost (~50–100ms) but already supports URL playback, so no regression.
- **Path A (restore-then-fix) over Path B (fix-forward)** during the voice debug. After my speculative changes broke three subsystems simultaneously, Ellie's pushback shifted the strategy to restoring yesterday's last-known-good state first, then layering the real fix on top. Saved as feedback memory `feedback_verify_before_changing.md`.

---

## Verification

| Gate | Result |
|------|--------|
| `pytest tests/` | 368 passed (was 360 + 8 new routing-override tests) |
| `ruff check adapters/ sdk/ tests/` | Clean |
| `ruff format --check` | Clean |
| `mypy adapters/` | 11 errors, all pre-existing variance/type issues — no new errors from this session |
| Local boot — `python -m adapters.local.dev_server` | Boots cleanly; `GET /api/settings` returns `channel_routing_overrides`, `POST` round-trips and rejects unknown skill names |
| AWS dev deploy | Two `./scripts/deploy.sh` runs (Dockerfile in-tree SDK build, then PyPI build after 0.1.2 publish). Both succeeded, ECS service stable on rev 29. |
| AWS smoke test | `/api/health` 200, `/api/auth/me` 401 without token, `/api/channels/whatsapp/webhook/{hash}` 401 without signature |
| End-to-end WhatsApp voice | Tested with `שלום 😃💐` (short) and longer Hebrew sentences. Both arrive as voice notes consistently. Lambda logs confirm every result now offloads to S3 (`audio offloaded to S3 (NNN bytes)`). |

---

## Commit Sequence

| Commit | Subject |
|---|---|
| `a1df22b` | infra: wire API_BASE_URL to ECS so webhook auto-registration uses the custom domain |
| `35b3c20` | feat: per-sender channel routing overrides |
| `66b3447` | docs(sdk): changelog entry for 0.1.2 (channel_routing_overrides) |
| `728ae0b` | chore: Dockerfile back to PyPI SDK pin after 0.1.2 publish |
| `93693c6` | fix: always offload audio to S3 (drop SQS_MAX_BYTES threshold) |

Plus tag `sdk/v0.1.2` (PyPI release verified live: https://pypi.org/project/t3nets-sdk/0.1.2/).

---

## Operational Footprint

- **Chatterbox EC2** (`i-021844569ef2e999d`, g4dn.xlarge in private subnet `10.0.10.194`) is now **running** — was stopped at session start. Cost ~$0.526/hr on-demand. Stop with `cd ~/projects/chatterbox-service && ./deploy-aws.sh stop` when done testing.
- **S3 audio prefix**: `t3nets-dev-static/{tenant_id}/audio/{uuid}.wav` now receives a small file (~200KB WAV) per voice synthesis with a 1h presigned URL TTL. No lifecycle policy in place yet — these accumulate. Consider adding `aws s3api put-bucket-lifecycle-configuration` to expire `**/audio/*` after a day.
- **Whapi webhook URL** is now `https://www.t3nets.dev/api/channels/whatsapp/webhook/849cd1505ade0a38` (kuchnir tenant). API Gateway direct URL would also work but custom domain is the canonical one.
- **PyPI**: `t3nets-sdk 0.1.2` released via the existing `sdk/v*` tag → GitHub Actions OIDC trusted publish (manual approval in `pypi` Environment).

---

## Voice debug — what actually went wrong and how it was untangled

Worth recording because it took most of the session and surfaced a useful pattern:

1. **Reported symptom**: "WhatsApp returns text + nikud but no audio for short messages."
2. **First (wrong) hypothesis driven into action**: I assumed the bug was in Chatterbox (inline-only mode) and changed three things in parallel — IAM policy, Chatterbox env vars (`S3_BUCKET`, `INLINE_MAX_BYTES=0`), and the voiceher worker (to accept `audio_url`). Each individually could have been fine, but together they broke the Lambda build because the voiceher `build_lambdas.sh` predates the SDK extraction and was missing `t3nets-sdk` + manylinux wheels.
3. **Pushback**: Ellie flagged that I was changing things that worked yesterday without first verifying *what* worked yesterday. She asked for an actual plan and proposed restoring the last-known-good state before any forward fix.
4. **Investigation (after pushback)**: pulled yesterday's CloudWatch logs. Every yesterday voice_say invocation logged `audio offloaded to S3 (NNN bytes)` — confirming the threshold was always being crossed by the longer test inputs. Today's short messages produced sub-threshold audio that stayed inline.
5. **Path A (chosen)**: restored Chatterbox to original env, removed the IAM policy I added, reverted the voiceher worker + build script, and rebuilt the Lambda using the t3nets `scripts/deploy.sh` packaging pattern (inline build command, no file changes). After this, yesterday's exact behaviour was back: long voice works, short doesn't.
6. **Real fix**: removed the `SQS_MAX_BYTES` threshold check in `_offload_audio_to_s3`. Same input that previously varied (because Claude restyle is non-deterministic and produces variable-length nikud → variable audio size → straddles the threshold) now uniformly offloads. Verified end-to-end on dev.

Lesson saved to memory at `feedback_verify_before_changing.md`: when something used to work, check logs first, restore known-good before fixing forward, one change at a time.

---

## What's Next

Roadmap-level: Phase 7 closed (server slim). Backlog items not yet started:

- **Phase 8 (Dashboard & UX)** — open: full SPA client-side routing, mobile-responsive layout, conversation history browser.
- **Phase 10 (Expand Skills)** — open: meeting prep (Calendar), email triage (Gmail/Outlook), skill marketplace page.
- **Phase 11 (Multi-cloud)** — open.

Smaller follow-ups surfaced this session:

- **S3 audio lifecycle policy**: add a rule to expire `audio/*` and `*/audio/*` keys after 1 day so the bucket doesn't accumulate WAVs indefinitely.
- **`voiceher/build_lambdas.sh`** in the yoltalk repo is broken (missing `t3nets-sdk` install + non-manylinux pyyaml/pydantic) — it predates the SDK extraction. Either fix it to mirror `t3nets/scripts/deploy.sh`'s two-phase install, or delete it in favour of running t3nets's deploy script with `voice_say` added to `DOMAIN_SKILLS`. Not urgent because the practice gets loaded from S3 at Lambda cold start; the in-Lambda framework is what the ZIP packs.
- **Dashboard chat audio rendering**: a couple of session log entries showed inline-base64 dashboard runs where Ellie observed "text only." With always-offload now in place this should be moot, but worth a focused test on chat.html's URL+base64 fallback path next time someone touches that file.
