"""
Ping skill — lightweight model and system health check.

No integrations required. Returns system info so the AI model can confirm
it's alive and responding. Written against the new SDK contract
(`SkillContext` + `SkillResult`) as the reference implementation other
built-in skills will be migrated to opportunistically.
"""

import platform
import sys
from datetime import datetime, timezone
from typing import Any

from t3nets_sdk.contracts import SkillContext, SkillResult


async def execute(ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
    """Return basic system info plus a human-readable one-liner."""
    now = datetime.now(timezone.utc)
    echo = params.get("echo", "")

    data: dict[str, Any] = {
        "status": "ok",
        "timestamp": now.isoformat(),
        "timestamp_human": now.strftime("%A, %B %d, %Y at %H:%M:%S UTC"),
        "python_version": (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        "platform": platform.system(),
        "message": "Pong! System is healthy and responding.",
    }

    if echo:
        data["echo"] = echo

    # User asked for --raw: let the router JSON-dump `data`. Skip rendering.
    if ctx.raw:
        return SkillResult.ok(data)

    render_prompt = (
        "Format this as a friendly health-check reply. Lead with a bold "
        "**Pong!** and a ✅ status line. Then a short markdown section with "
        "bold labels for Time, Platform, and Python version. If an `echo` "
        "field is present, quote it back on its own line. Keep it upbeat "
        "and skimmable — two or three short lines, no tables."
    )
    return SkillResult.ok(data, render_prompt=render_prompt)
