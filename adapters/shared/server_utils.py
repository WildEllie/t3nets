"""Shared server utilities — constants and pure functions used by both local and AWS servers."""

import json

# Integration field schemas — defines the config form per integration type.
# Used by GET /api/integrations to tell the frontend which fields to render.
INTEGRATION_SCHEMAS: dict = {
    "jira": {
        "label": "Jira",
        "fields": [
            {
                "key": "url",
                "label": "Jira URL",
                "type": "url",
                "required": True,
                "placeholder": "https://yourteam.atlassian.net",
            },
            {
                "key": "email",
                "label": "Email",
                "type": "email",
                "required": True,
                "placeholder": "admin@company.com",
            },
            {
                "key": "api_token",
                "label": "API Token",
                "type": "password",
                "required": True,
                "placeholder": "Your Jira API token",
            },
            {
                "key": "project_key",
                "label": "Project Key",
                "type": "text",
                "required": True,
                "placeholder": "PROJ",
            },
            {
                "key": "board_id",
                "label": "Board ID",
                "type": "text",
                "required": False,
                "placeholder": "Optional — for sprint queries",
            },
        ],
    },
    "github": {
        "label": "GitHub",
        "fields": [
            {
                "key": "token",
                "label": "Personal Access Token",
                "type": "password",
                "required": True,
                "placeholder": "ghp_...",
            },
            {
                "key": "org",
                "label": "Organization",
                "type": "text",
                "required": True,
                "placeholder": "your-org",
            },
        ],
    },
    "teams": {
        "label": "Microsoft Teams",
        "fields": [
            {
                "key": "app_id",
                "label": "Bot App ID",
                "type": "text",
                "required": True,
                "placeholder": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            },
            {
                "key": "app_secret",
                "label": "Bot App Secret",
                "type": "password",
                "required": True,
                "placeholder": "Your bot client secret",
            },
            {
                "key": "azure_tenant_id",
                "label": "Azure AD Tenant ID",
                "type": "text",
                "required": False,
                "placeholder": "Leave blank for multi-tenant bots",
            },
        ],
    },
    "telegram": {
        "label": "Telegram",
        "fields": [
            {
                "key": "bot_token",
                "label": "Bot Token",
                "type": "password",
                "required": True,
                "placeholder": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            },
            {
                "key": "webhook_secret",
                "label": "Webhook Secret",
                "type": "text",
                "required": False,
                "placeholder": "Optional — auto-generated if blank",
            },
        ],
    },
}


def _format_raw_json(data: dict) -> str:  # type: ignore[type-arg]
    """Format raw JSON for dashboard display."""
    return json.dumps(data, indent=2, default=str)


def _strip_metadata(messages: list[dict]) -> list[dict]:  # type: ignore[type-arg]
    """Strip metadata from conversation history before sending to the AI provider."""
    return [{"role": m["role"], "content": m["content"]} for m in messages]


def _uptime_human(seconds: float) -> str:
    """Convert seconds to human-readable uptime."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    elif s < 3600:
        return f"{s // 60}m {s % 60}s"
    elif s < 86400:
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h {m}m"
    else:
        d = s // 86400
        h = (s % 86400) // 3600
        return f"{d}d {h}h"
