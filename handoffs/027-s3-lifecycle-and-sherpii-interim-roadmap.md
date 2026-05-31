# Handoff: S3 audio lifecycle + Sherpii interim roadmap

**Date:** 2026-05-31
**Status:** Two shipped pieces; one design discussion paused mid-plan (WhatsApp sender authorisation).
**Commits:** `87c5057`, `df758f8` (t3nets) + `fbd249d` (yoltalk/voiceher)

---

## What Was Done

### 1. S3 lifecycle TTL for generated audio (shipped)

The voice_say Lambda's S3 audio uploads accumulated indefinitely after the always-offload change in `93693c6`. Added a 1-day lifecycle rule on `t3nets-{stage}-static`.

S3 lifecycle `Prefix` is start-of-key only — no glob — so the per-tenant key shape `{tenant}/audio/{uuid}.wav` couldn't be matched by a single rule. Flipped to `audio/{tenant}/{uuid}.wav` so one rule (`Prefix: audio/`) covers every tenant.

**Files changed**

| File | Change |
|------|--------|
| `adapters/aws/lambda_handler.py` | `_offload_audio_to_s3` writes `audio/{tenant_id}/{uuid}.wav` (was `{tenant_id}/audio/…`). Docstring explains why. |
| `infra/aws/modules/cdn/main.tf` | New `aws_s3_bucket_lifecycle_configuration.static` resource. `expire-generated-audio` rule, `Filter.Prefix = "audio/"`, 1-day expiration + noncurrent-version expiration + abort-incomplete-multipart. |

**Deploy:** voice_say Lambda repackaged + uploaded inline (same Step 4 pattern from handoff 026). `terraform apply` applied the lifecycle rule. Verified via `aws s3api get-bucket-lifecycle-configuration` — rule live.

Old objects at `{tenant}/audio/*` won't match the new rule and sit until manually cleaned up. Their presigned URLs already expired (1h TTL), so they're harmless — just stale bytes. No cleanup attempted this session.

### 2. voiceher `build_lambdas.sh` repaired (shipped, yoltalk repo)

The voiceher script for rebuilding voice_say / voice_config Lambdas predated the Phase 6d SDK extraction. Two bugs:

- `pip3 install pyyaml -t ...` produced macOS arm64 wheels for `pydantic_core` → `ImportModuleError` at Lambda cold start.
- Never installed `t3nets-sdk` → `No module named 't3nets_sdk'`.

Replaced the single-line install with the same two-phase pattern t3nets uses in `scripts/deploy.sh`:

```bash
pip3 install --platform manylinux2014_x86_64 --python-version 3.12 \
    --only-binary=:all: --implementation cp \
    pyyaml pydantic -t "${BUILD_DIR}" --upgrade --quiet
pip3 install "t3nets-sdk>=0.1,<0.2" -t "${BUILD_DIR}" --no-deps --upgrade --quiet
```

Also added an upload step (auto `aws lambda update-function-code` after the ZIP). Skippable with `DEPLOY=0`; stage prefix overridable with `T3NETS_LAMBDA_PREFIX`.

**Verified:** ran `./build_lambdas.sh` from voiceher root — both `t3nets-dev-skill-voice_say` and `t3nets-dev-skill-voice_config` rebuilt (5.2MB each, was 208KB without deps) and uploaded cleanly. Committed as `fbd249d` in `WildEllie/yoltalk` (not pushed — local on `main`; awaiting your call to push).

### 3. Interim roadmap: Sherpii ↔ t3nets alignment (shipped)

New doc at `docs/ROADMAP-INTERIM-SHERPII.md`, committed as `df758f8`. Sits alongside the canonical `docs/ROADMAP.md`; time-bound until Phase 2.1 (language-neutral contract) lands and absorbs the rest.

**Structure:**
- **Phase 1** (Sherpii repo) — short-term alignment to current t3nets-sdk practice contract: schema fix, pyproject, test doubles, `t3nets practice package`, dev-loop docs.
- **Phase 2** (t3nets platform) — eight capability requests Sherpii needs. Critical path 2.1 → 2.2 → (2.7 → 2.8). Quick wins (2.3, 2.5, 2.6) parallelisable inside the current SDK.

**Locked decisions** (your input this session):
- **2.1 transport: gRPC** — strong typing, streaming, generated stubs in every target language.
- **2.7 audit event taxonomy: practice-declared** — t3nets owns transport/storage only; healthcare-specific event types stay in Sherpii.

**Cross-cutting notes** captured in the doc: multi-language runtime work (container Lambdas) is the real meat under 2.1; today's `scripts/build_lambda_base.sh` is Python-only.

---

## Files Modified / Created

| Repo | Path | Status |
|---|---|---|
| t3nets | `adapters/aws/lambda_handler.py` | Modified — new key shape |
| t3nets | `infra/aws/modules/cdn/main.tf` | Modified — lifecycle rule |
| t3nets | `docs/ROADMAP-INTERIM-SHERPII.md` | Created |
| yoltalk | `voiceher/build_lambdas.sh` | Modified — manylinux + sdk + auto-upload |
| yoltalk | `voiceher/skills/voice_say/lambda.zip` | Rebuilt |
| yoltalk | `voiceher/skills/voice_config/lambda.zip` | Rebuilt |

---

## What's Paused

### WhatsApp sender authorisation check

Plan presented, decisions taken, Step 0 investigation done, **no code shipped**. Pick this back up when you're ready.

**Goal:** when an inbound WhatsApp message arrives, drop it (silently, with a CloudWatch WARNING) unless the sender's phone number matches a `TenantUser.channel_identities["whatsapp"]` in the tenant's user list.

**Decisions locked this session:**
- **A. Default-on** — `TenantSettings.whatsapp_restrict_to_users: bool = True`.
- **B. WhatsApp only for now** — Telegram comes later; explicit `whatsapp_*` field, not a generic `channel_auth_required` dict.
- **C. Single phone number per user** — `channel_identities["whatsapp"]: str` (digits-only after normalisation).

**Step 0 investigation result:** scanned `/ecs/t3nets-dev-router` logs for 7 days — zero WhatsApp activity (last actual message was 2026-05-20). Couldn't observe Whapi probe behaviour from history. Did partial code-level audit:
- `handle_whatsapp_webhook` always returns 200 (to prevent Whapi retry storms).
- `parse_inbound` only extracts text-type messages from the `messages` array; everything else parses to an empty `text` field. So non-message events naturally no-op without auth-check interference.
- Route entry is POST-only — no GET verification handshake to worry about.

So the security check can run inside `_handle_whatsapp_message` without breaking probes.

**Sequence from the plan (waiting on your go):**

1. Backfill kuchnir's existing user(s) with `channel_identities = {"whatsapp": "972545245926"}` directly in DynamoDB. Avoids self-locking the moment enforcement turns on.
2. SDK 0.1.3 — add `TenantSettings.whatsapp_restrict_to_users: bool = True`. Bump, publish via `sdk/v0.1.3` tag, manual approval in `pypi` GitHub Environment.
3. Dockerfile in-tree workaround during dev iteration → revert to PyPI pin after publish (same dance as 0.1.2).
4. `settings.py` handler — accept the toggle in POST, surface in GET.
5. AdminAPI — `PATCH /api/admin/tenants/{tid}/users/{uid}` accepting `{"channel_identities": {…}}`, normalising any `whatsapp` value to digits-only before persisting. Mirror in `LocalAdminAPI`.
6. Authoriser helper + check — `is_authorized_whatsapp_sender(tenant, users, sender_id)` in `adapters/shared/handlers/webhooks.py` next to `normalize_sender_id`. Call from `_handle_whatsapp_message` only when the setting is True. Miss → log WARNING + return early.
7. Settings UI checkbox — Channels tab, WhatsApp card area, "Restrict to team members (recommended)".
8. Unit tests for the authoriser.
9. Deploy + verify with authorised + unauthorised numbers.

**Risks flagged in the original plan:**
- Step 1 ordering matters — backfill before enforcement or you get locked out.
- `list_users()` failure mode: keep it fail-closed (current draft would throw and the webhook returns 500 — safer than fail-open).
- Routing override (from earlier work) does NOT bypass auth. Acceptable per the security intent.

### Agent-swarm execution of Phase 2 quick wins

You asked whether parallel agents with worktrees could attack the Phase 2 list. Reply summary I gave:

- Most of Phase 2 is design or has sequencing dependencies — poor fit for a swarm.
- Three items are cleanly parallel: **2.3 Page helper SDK**, **2.5 Blob listing API**, **2.6 CLI scaffolds** (`add-skill`, `add-page`). Each touches its own file footprint; merge conflicts unlikely.
- **2.4 (practice-level secrets)** has moderate collision risk with manifest/settings code — recommended sequential after the parallel three.
- **2.1 gRPC contract** is a design doc not implementation — one careful agent (or you/me) drafts the `.proto` + JSON Schemas; review locks the contract; *then* fan out implementations downstream.

**Not spawned yet.** Open question: where does the Sherpii repo live? Phase 1 can't be assigned to a worktree without it. I checked `~/projects/` — `sherpa`, `sherpa3`, `yoltalk` exist but no `sherpii`. Maybe a not-yet-cloned repo, maybe a different name. Tell me where and I can include Phase 1 in the parallel wave.

---

## Verification

| Gate | Result |
|------|--------|
| `terraform plan -out` + review | 2 to add (lifecycle config + ping Lambda layer rebuild), 1 change (ping Lambda picks up new `lambda_handler.py`), 1 destroy (old `build_lambda_base` trigger) — expected ripple |
| `terraform apply plan.out` | Successful, ECS stable |
| `aws s3api get-bucket-lifecycle-configuration --bucket t3nets-dev-static` | Rule `expire-generated-audio` live, `Prefix: audio/`, `Days: 1` |
| voice_say + voice_config Lambdas rebuild via fixed voiceher script | Both InProgress → Successful, 5.2MB ZIPs |
| End-to-end WhatsApp voice test (post-changes) | **Not run this session** — no WhatsApp messages sent today. Last verified state was 2026-05-20 (always-offload fix). Recommend a quick test next session to confirm the new `audio/` key shape works under live load. |

---

## Memory Updated

Two feedback entries saved this session:

| File | Capture |
|---|---|
| `~/.claude/.../memory/feedback_no_unnecessary_s3_scans.md` | Don't `aws s3 ls --recursive` to "see what's there" — read the producer code for the key pattern. Wasteful on unknown-size buckets. (Saved after you called out the unnecessary recursive listing.) |
| Existing `feedback_verify_before_changing.md` | Still load-bearing — guided the Path A / Path B fork in the previous voice debug. |

---

## What's Next

**Default option** (pick up where we paused): execute the WhatsApp sender authorisation plan, starting with Step 1 (DynamoDB backfill) so you don't lock yourself out when enforcement goes live.

**Alternative**: green-light the 3-agent swarm wave for 2.3 + 2.5 + 2.6 from the Sherpii interim roadmap (page helper SDK, blob listing, CLI scaffolds). I'd need to draft tight briefs for each before spawning. Phase 1 items wait on you locating the Sherpii repo.

**Backlog from prior handoffs still open:**
- Old `{tenant}/audio/*` S3 objects from before this session's key-shape change — sit forever unless cleaned manually. Harmless (URLs expired). Low priority.
- voiceher commit `fbd249d` is local on `main`; not pushed to `WildEllie/yoltalk`. Was deliberately held — your call to push.
- Chatterbox EC2 (`i-021844569ef2e999d`, g4dn.xlarge in private subnet `10.0.10.194`) is presumably still running from the 2026-05-20 session unless you stopped it. ~$0.526/hr on-demand. `cd ~/projects/chatterbox-service && ./deploy-aws.sh stop` when done testing.
