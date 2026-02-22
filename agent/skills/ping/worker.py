"""
Ping skill — lightweight model and system health check.

No integrations required. Returns system info so the AI model
can confirm it's alive and responding.
"""

import platform
import sys
from datetime import datetime, timezone


def execute(params: dict, secrets: dict) -> dict:
    """Return basic system info for the model to interpret."""
    now = datetime.now(timezone.utc)
    echo = params.get("echo", "")

    result = {
        "status": "ok",
        "timestamp": now.isoformat(),
        "timestamp_human": now.strftime("%A, %B %d, %Y at %H:%M:%S UTC"),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": platform.system(),
        "message": "Pong! System is healthy and responding.",
    }

    if echo:
        result["echo"] = echo

    return result
