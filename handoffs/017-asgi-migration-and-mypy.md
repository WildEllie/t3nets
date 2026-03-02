# Handoff: uvicorn ASGI Migration + Strict Mypy Compliance

**Date:** 2026-03-03
**Status:** Complete, pushed to main
**Commit:** `5d913e5`

---

## What Was Done

Two engineering quality sessions combined into one commit:

1. **Migrated both servers from `ThreadedHTTPServer` to uvicorn ASGI + Starlette** — eliminating the `asyncio.run()`-per-request pattern that created and destroyed an event loop on every single request.
2. **Fixed all 284 mypy strict-mode errors** across `agent/` and `adapters/` — 0 errors remaining across 60 source files.
3. **Added `THIRD_PARTY_LICENSES`** with BSD-3-Clause attribution for uvicorn (required for binary redistribution in Docker/ECS).

---

## ASGI Migration

### Why

Both `dev_server.py` and `aws/server.py` used Python stdlib `ThreadedHTTPServer` (thread-per-request). Each request called `asyncio.run(coro)`, which creates a brand-new event loop, runs the coroutine, then tears it down. This prevents any real async concurrency — each request blocks a thread and gets its own ephemeral event loop.

### New Stack

- **uvicorn** — ASGI server with a persistent event loop
- **Starlette** — lightweight ASGI framework: `Route`, `Request`, `JSONResponse`, `StreamingResponse`, `FileResponse`, `CORSMiddleware`

### Pattern

Every `_handle_*` method became a module-level `async def`:

| Old | New |
|-----|-----|
| `asyncio.run(coro)` | `await coro` |
| `self._read_json()` | `await request.json()` |
| `self._json_response(data, status)` | `return JSONResponse(data, status_code=status)` |
| `self._serve_file("chat.html", ...)` | `return FileResponse(path)` |
| `self.send_error(404)` | `return Response(status_code=404)` |
| `self.headers.get("X-Auth")` | `request.headers.get("x-auth")` |
| `parse_qs(urlparse(self.path).query)` | `request.query_params` |
| `do_OPTIONS` | `CORSMiddleware` |

### SSE: `_QueueBridge`

`SSEConnectionManager` (`agent/sse.py`) uses `threading.Lock` and accepts file-like objects with `write()`/`flush()`. Rather than rewriting it (and its 10 tests), a `_QueueBridge` adapter bridges sync `write()` calls into an `asyncio.Queue` via `loop.call_soon_threadsafe()`. The async SSE generator reads from the queue with a 15-second `asyncio.wait_for` timeout — on timeout it yields a keepalive comment, replacing `start_keepalive_thread`.

**`agent/sse.py` and its tests are unchanged.**

```python
class _QueueBridge:
    def __init__(self, queue: asyncio.Queue[bytes], loop: asyncio.AbstractEventLoop) -> None:
        self._queue = queue
        self._loop = loop

    def write(self, data: bytes | str) -> int:
        if isinstance(data, str):
            data = data.encode()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, data)
        return len(data)

    def flush(self) -> None:
        pass
```

### AWS-Specific: `WebSocketEventMiddleware`

API Gateway WebSocket events arrive as POSTs with an `X-WS-Route` header. A Starlette ASGI middleware class intercepts these before routing:

```python
class WebSocketEventMiddleware:
    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("method") == "POST":
            headers = dict(scope["headers"])
            ws_route = headers.get(b"x-ws-route", b"").decode()
            if ws_route:
                request = Request(scope, receive)
                response = await _dispatch_ws_event(request, ws_route)
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)
```

### AWS-Specific: `asyncio.to_thread()` for AdminAPI / PlatformAPI

`AdminAPI.handle_request()` and `PlatformAPI.handle_request()` call `asyncio.run()` internally (they were synchronous dispatch tables). Calling them directly from an async handler raises "This event loop is already running". Fix: wrap in `asyncio.to_thread()` which runs them in a thread pool where no event loop is active.

```python
headers = dict(request.headers)  # convert Starlette Headers → dict
data, status = await asyncio.to_thread(
    admin_api.handle_request, method, path, headers, body
)
```

### Server Startup

```python
def main():
    asyncio.run(init())   # async init runs before handing off to uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
```

### Files Changed

| File | Change |
|------|--------|
| `adapters/local/dev_server.py` | Full rewrite — Starlette + uvicorn |
| `adapters/aws/server.py` | Full rewrite — Starlette + uvicorn + WebSocketEventMiddleware |
| `adapters/shared/base_handler.py` | **Deleted** — replaced by Starlette |
| `adapters/shared/server_utils.py` | Unchanged (INTEGRATION_SCHEMAS, helpers still used) |
| `pyproject.toml` | Added `starlette>=0.41`, `uvicorn[standard]>=0.32` to `local` extras |

---

## Mypy Strict Compliance

**Before:** 284 errors across 43 files
**After:** 0 errors across 60 source files

### Error categories fixed

| Category | Fix applied |
|----------|-------------|
| `dict` bare type (158 errors) | `dict[str, Any]` throughout; `from typing import Any` added where needed |
| `boto3` / `botocore` untyped imports | `# type: ignore[import-untyped]` on each import line |
| Missing function annotations | Added `-> None`, `-> str`, `-> tuple[str, str]`, etc. |
| `re.Pattern` bare type | `re.Pattern[str]` |
| `asyncio.Queue` bare type | `asyncio.Queue[bytes]` |
| `AIModel \| None` attribute access | `assert model is not None` after fallback |
| `Tenant \| None` attribute access | Early `return None` guard before attribute access |
| boto3 `Returning Any` | `cast(dict[str, Any], ...)` |
| `tuple` bare type | `tuple[type1, type2]` |
| Unused `# type: ignore` | Removed |

### Notable fixes

- `_resolve_model(tenant: Any) -> tuple[str, str]` — added type annotation + `assert model is not None` to narrow `AIModel | None`
- `extract_auth(headers: Any)` — changed from `dict[str, Any]` to `Any` to accept both plain dicts and Starlette `Headers`
- `adapters/local/sqlite_tenant_store.py` — `_row_to_*` methods use `str(row[N])` casts (sqlite3 rows are `tuple[object, ...]`)
- `adapters/aws/admin_api.py`, `platform_api.py` — `__init__` and `handle_request` fully annotated

---

## License Compliance

`THIRD_PARTY_LICENSES` added at project root with the full BSD-3-Clause text for uvicorn (Copyright © 2017-present, Encode OSS Ltd). Required because Docker/ECS deployments bundle uvicorn — that constitutes binary redistribution under BSD-3.

---

## Verification

```
ruff check adapters/         # All checks passed
mypy agent/ adapters/        # Success: no issues found in 60 source files
pytest                       # 160 passed in 0.24s
```

---

## What's Next

- **Auto-reload**: uvicorn `--reload` is now available for local dev — add to `CLAUDE.md` or a dev script
- **aiosqlite**: Local SQLite stores still use blocking `sqlite3`. For the single-user dev server this is fine (<1ms), but migrating to `aiosqlite` would complete the async story
- **AdminAPI / PlatformAPI refactor**: These still use synchronous `asyncio.run()` internally and run via `asyncio.to_thread()`. A future pass could convert them to true async classes, eliminating the thread-pool hop
