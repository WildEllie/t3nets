# T3nets Changes Required for VoiceHer Practice

## Architecture Principle

Chatterbox (the voice synthesis model) is an **external AI service** — like Bedrock or Ollama.
It runs separately, accessed via URL + API token. The VoiceHer skill is a thin client
that calls this service, same as sprint_status calls Jira.

```
User types → t3nets router → voice_say skill (Lambda)
                                    ↓
                              1. Claude API (restyle + nikud)
                              2. POST to Chatterbox service (synthesize)
                                    ↓
                              audio response → channel adapter → user hears voice
```

The Chatterbox service is NOT part of t3nets. It's a standalone HTTP service
(Docker container on GPU, Modal.com, or any inference endpoint) that t3nets
connects to like any other integration.

## Changes Needed in T3nets

### 1. Integration Type: Voice Service

VoiceHer uses `requires_integration: voiceher` in its skill.yaml. The admin
configures the Chatterbox service URL + API token via the existing integrations
settings page, same as Jira credentials.

**Secrets Manager entry** (per tenant, at `/t3nets/{stage}/{tenant_id}/voiceher`):
```json
{
  "anthropic_api_key": "sk-ant-...",
  "claude_model": "claude-sonnet-4-20250514",
  "chatterbox_url": "https://voiceher-gpu.example.com",
  "chatterbox_api_token": "vhr_...",
  "ref_speaker_url": "https://voiceher-gpu.example.com/assets/ref_speaker.wav"
}
```

**No t3nets code change needed** — the existing secrets flow handles this.
The skill worker reads `secrets["chatterbox_url"]` and `secrets["anthropic_api_key"]`
and makes HTTP calls to both services. The admin configures these via the
existing integrations settings page.

### 2. Async Skill with Callback (Long-Running Synthesis)

Synthesis takes 10-30s. For the synchronous `/api/skill/voice_say` path
(dashboard pages), this is fine — the browser waits. But for async channels
(Telegram, Teams), the skill needs to:

1. Submit synthesis request to Chatterbox service
2. Get back a `request_id`
3. Store `request_id` in DynamoDB pending-requests table
4. Chatterbox calls back when done: `POST /api/callback/{request_id}`
5. Callback handler looks up the pending request, sends result to channel

**New endpoint needed:**
```
POST /api/callback/{request_id}
Body: {"audio_b64": "...", "nikud_text": "...", "status": "ok"}
```

**Callback handler flow:**
```python
async def handle_callback(request: Request) -> Response:
    """POST /api/callback/{request_id} — external service completion."""
    request_id = request.path_params["request_id"]
    body = await request.json()

    # Look up who's waiting for this result
    pending = await pending_store.get(request_id)
    if not pending:
        return Response(status_code=404)

    # Mark as completed
    await pending_store.complete(request_id, result=body)

    # Route result to the original channel
    await result_router.deliver(
        tenant_id=pending["tenant_id"],
        channel_type=pending["reply_channel"],
        reply_target=pending["reply_target"],
        session_id=pending["session_id"],
        result=body,
    )
    return Response(status_code=200)
```

**DynamoDB:** Uses the existing `pending-requests` table (already deployed for
async Lambda skills). The callback handler looks up the request_id, marks it
completed, and routes the result — same flow as Lambda skill completion via SQS.

**Security:** The callback endpoint must validate requests. Options:
- Include a `callback_secret` in the request to the external service;
  the service echoes it back. The handler verifies it matches.
- Or: use the request_id as a short-lived unguessable token (UUID4).

**This pattern is generic** — any slow external service can use callbacks.
Not VoiceHer-specific.

### 3. Skill Response Type: Audio

Skills currently return JSON dicts that Claude interprets as text.
VoiceHer returns audio. The channel adapters need to handle this.

**Convention** — skill returns:
```python
{
    "type": "audio",
    "audio_b64": "UklGRi...",          # Base64 WAV
    "text": "שָׁלוֹם, אֵיךְ אַתֶּם?",  # Display text / caption
    "format": "wav",
}
```

**Channel adapter handling:**

| Channel | Behavior |
|---------|----------|
| Dashboard page | Page JS decodes base64, creates `<audio>` element, auto-plays |
| Dashboard chat | Send via WebSocket as `{"type": "audio", ...}`, JS handles playback |
| Telegram | `bot.send_voice(chat_id, voice=audio_bytes, caption=text)` |
| Teams | Upload audio as attachment via Graph API |

**Router change** — in `result_router.py`:
```python
async def deliver(self, ..., result: dict):
    if result.get("type") == "audio":
        await self._deliver_audio(channel_type, reply_target, result)
    else:
        await self._deliver_text(channel_type, reply_target, result)
```

### 4. Practice Assets in BlobStore

The VoiceHer practice ZIP includes `assets/ref_speaker.wav` — the voice
reference clip. On practice install, assets are copied to BlobStore.

**practice.yaml:**
```yaml
assets:
  - ref_speaker.wav
```

**Install flow addition:**
```python
# In PracticeRegistry.install():
for asset in practice.assets:
    asset_path = practice_dir / "assets" / asset
    if asset_path.exists():
        await blob_store.put(
            tenant_id,
            f"practices/{practice.name}/assets/{asset}",
            asset_path.read_bytes(),
        )
```

Workers access: `blob_store.get(tenant_id, "practices/voiceher/assets/ref_speaker.wav")`

### 5. Practice Install Hooks

**practice.yaml:**
```yaml
hooks:
  on_install: setup.py
```

On install, t3nets loads `setup.py` from the practice and calls:
```python
await hook.on_install(blob_store, tenant_id)
```

VoiceHer uses this to seed `voiceher/config.json` with default
exaggeration/cfg_weight values.

### 6. Practice Dependencies

VoiceHer's skill worker uses **only stdlib** (`urllib.request`, `json`, `re`,
`logging`). It has **zero pip dependencies** — both Claude API and Chatterbox
service calls are plain HTTP via `urllib`.

This means no `dependencies` field is needed in `practice.yaml` for VoiceHer.
However, other practices may need pip packages, so the `dependencies` mechanism
is still worth building as a general t3nets feature:

**practice.yaml (general pattern):**
```yaml
dependencies:
  - "some-package>=1.0"
```

**Install flow:** `pip install` declared dependencies into the Lambda layer
or ECS container. VoiceHer doesn't use this.

## Chatterbox Service (Separate Deployment)

This is NOT a t3nets change — it's a standalone service. Deployed independently.

**API contract:**

```
POST /synthesize
Headers: Authorization: Bearer {api_token}
Body: {
  "text": "שָׁלוֹם",
  "language": "he",
  "exaggeration": 0.35,
  "cfg_weight": 0.0,
  "ref_speaker": "default"       # or a URL/key for custom ref
}
Response (sync): {
  "audio_b64": "UklGRi...",
  "format": "wav",
  "sample_rate": 24000,
  "duration_ms": 2340
}

POST /synthesize/async
Body: same + "callback_url": "https://t3nets-api/api/callback/{request_id}"
Response: {"request_id": "req_abc123", "status": "processing"}
→ later: POST to callback_url with audio result

GET /health
Response: {"status": "ok", "model_loaded": true, "gpu": "A100"}
```

**Deployment options:**
- Docker container on any GPU VM (EC2 g4dn, GCP T4, etc.)
- Modal.com serverless GPU (~$0.001/sec)
- Replicate.com (Chatterbox is available as a Replicate model)
- Local Mac with MPS (current `--serve` mode)

## Summary of T3nets Changes

| # | Change | Scope | Effort | Blocks VoiceHer? |
|---|--------|-------|--------|-----------------|
| 1 | Integration type: `voiceher` | Existing secrets flow | None | No (works today) |
| 2 | Callback endpoint `/api/callback/{id}` | Generic async pattern, uses existing pending-requests table | Medium | Only for Telegram/Teams |
| 3 | Audio response type in channel adapters | Router + Telegram + Teams | Medium | Only for Telegram/Teams |
| 4 | Practice assets → BlobStore on install | PracticeRegistry | Small | Yes |
| 5 | Practice install hooks | PracticeRegistry | Small | No (can seed manually) |
| 6 | Practice dependencies (pip) | Install flow | Small | No (VoiceHer has zero deps) |

**Not needed in t3nets:**
- GPU/sidecar infrastructure (Chatterbox runs externally)
- Heavy ML libraries (skill worker uses only stdlib `urllib`)
- New DynamoDB tables (reuses existing pending-requests)
- Binary asset serving at runtime (audio goes through channel adapters as base64)

## VoiceHer Skill Worker (Updated Architecture)

```python
def execute(params, secrets, ctx):
    text = params["text"]

    # 1. Restyle + nikud (runs in Lambda, lightweight)
    styled_text = call_claude_restyle(text)   # ~1s, uses ANTHROPIC_API_KEY

    # 2. Synthesize (calls external Chatterbox service)
    chatterbox_url = secrets["chatterbox_url"]
    api_token = secrets["chatterbox_api_token"]
    audio = call_chatterbox(chatterbox_url, api_token, styled_text)  # ~10-30s

    # 3. Return audio for channel adapter to deliver
    return {
        "type": "audio",
        "audio_b64": audio["audio_b64"],
        "text": styled_text,
        "format": "wav",
    }
```

The skill worker has ZERO ML dependencies. It's just HTTP calls.
