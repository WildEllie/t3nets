"""Shared integration handlers for GET/POST/test integration endpoints.

Used by both ``adapters.aws.server`` and ``adapters.local.dev_server`` to
avoid duplicating ~200 lines of near-identical handler logic.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from adapters.shared.server_utils import INTEGRATION_SCHEMAS
from agent.interfaces.secrets_provider import SecretsProvider

logger = logging.getLogger(__name__)

# Type alias for the optional post-save callback.
# Args: (tenant_id, integration_name, merged_credentials)
OnCredentialsSaved = Callable[[str, str, dict[str, Any]], Awaitable[None]] | None


class IntegrationHandlers:
    """Encapsulates integration CRUD + test handlers.

    Each public method corresponds to a single HTTP endpoint and receives the
    pre-resolved ``tenant_id`` (auth is handled by the calling server).

    Parameters
    ----------
    secrets:
        A :class:`SecretsProvider` implementation (AWS Secrets Manager, local
        env-file, etc.).
    on_credentials_saved:
        Optional async callback invoked **after** credentials are merged and
        persisted.  Servers use this to wire in webhook registration, channel
        mapping writes, and other adapter-specific side-effects.
    """

    def __init__(
        self,
        secrets: SecretsProvider,
        on_credentials_saved: OnCredentialsSaved = None,
    ) -> None:
        self._secrets = secrets
        self._on_credentials_saved = on_credentials_saved

    # ------------------------------------------------------------------
    # GET /api/integrations
    # ------------------------------------------------------------------

    async def list_integrations(self, request: Request, tenant_id: str) -> Response:
        """Return every known integration with its connection status."""
        try:
            connected = await self._secrets.list_integrations(tenant_id)
            result = [
                {
                    "name": name,
                    "label": schema["label"],
                    "connected": name in connected,
                    "fields": schema["fields"],
                }
                for name, schema in INTEGRATION_SCHEMAS.items()
            ]
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ------------------------------------------------------------------
    # GET /api/integrations/{name}
    # ------------------------------------------------------------------

    async def get_integration(self, request: Request, tenant_id: str) -> Response:
        """Return current config for a single integration (passwords masked)."""
        try:
            integration_name = request.path_params["name"]
            if integration_name not in INTEGRATION_SCHEMAS:
                return JSONResponse(
                    {"error": f"Unknown integration: {integration_name}"},
                    status_code=404,
                )
            schema = INTEGRATION_SCHEMAS[integration_name]
            connected = False
            config: dict[str, Any] = {}
            try:
                stored = await self._secrets.get(tenant_id, integration_name)
                connected = True
                password_keys = {f["key"] for f in schema["fields"] if f["type"] == "password"}
                for key, value in stored.items():
                    if key in password_keys and value:
                        config[key] = "\u2022" * 8  # masked bullets
                    else:
                        config[key] = value
            except Exception:
                pass  # no stored secrets yet
            return JSONResponse(
                {
                    "name": integration_name,
                    "label": schema["label"],
                    "connected": connected,
                    "config": config,
                    "fields": schema["fields"],
                }
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ------------------------------------------------------------------
    # POST /api/integrations/{name}
    # ------------------------------------------------------------------

    async def post_integration(self, request: Request, tenant_id: str) -> Response:
        """Save integration credentials using partial-merge semantics.

        Merge rules (from the AWS implementation):
        - ``null`` / missing key  -> preserve existing value
        - blank (whitespace-only) -> intentionally clear the value
        - non-empty value         -> update
        """
        try:
            integration_name = request.path_params["name"]
            body = await request.json()

            # Allow body to override tenant_id (admin use-case).
            if body.get("tenant_id"):
                tenant_id = body["tenant_id"]

            # -- partial merge with existing secrets --
            try:
                existing = await self._secrets.get(tenant_id, integration_name)
            except Exception:
                existing = {}

            merged: dict[str, Any] = dict(existing)
            for key, value in body.items():
                if key == "tenant_id":
                    continue  # metadata, not a credential field
                if value is None:
                    pass  # null -> preserve existing
                elif isinstance(value, str) and value.strip() == "":
                    merged[key] = ""  # blank -> intentionally clear
                else:
                    merged[key] = value  # non-empty -> update

            await self._secrets.put(tenant_id, integration_name, merged)
            logger.info(
                "Stored %s credentials for tenant %s",
                integration_name,
                tenant_id,
            )

            # Notify the server-specific callback (webhook registration, etc.)
            if self._on_credentials_saved is not None:
                try:
                    await self._on_credentials_saved(tenant_id, integration_name, merged)
                except Exception:
                    logger.exception(
                        "on_credentials_saved callback failed for %s/%s",
                        tenant_id,
                        integration_name,
                    )

            return JSONResponse({"ok": True})
        except Exception as e:
            logger.exception("Integration endpoint error")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ------------------------------------------------------------------
    # POST /api/integrations/{name}/test
    # ------------------------------------------------------------------

    async def test_integration(self, request: Request, tenant_id: str) -> Response:
        """Run a connectivity test against the external service."""
        try:
            integration_name = request.path_params["name"]
            body = await request.json()
            result = _test_integration(integration_name, body)
            return JSONResponse(result, status_code=200 if result.get("ok") else 400)
        except Exception as e:
            logger.exception("Integration test error")
            return JSONResponse({"error": str(e)}, status_code=500)


# =====================================================================
# Integration-specific test helpers (pure functions, no cloud imports)
# =====================================================================


def _test_integration(name: str, creds: dict[str, Any]) -> dict[str, Any]:
    """Dispatch to the correct per-integration test function."""
    dispatch: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "jira": _test_jira,
        "telegram": _test_telegram,
        "whatsapp": _test_whatsapp,
    }
    handler = dispatch.get(name)
    if handler is None:
        return {"ok": False, "error": f"Testing not supported for '{name}'"}
    return handler(creds)


def _test_jira(creds: dict[str, Any]) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    url = creds.get("url", "").rstrip("/")
    email = creds.get("email", "")
    api_token = creds.get("api_token", "")
    if not all([url, email, api_token]):
        return {
            "ok": False,
            "error": "url, email, and api_token are required",
        }
    try:
        auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        req = urllib.request.Request(
            f"{url}/rest/api/3/myself",
            headers={
                "Authorization": f"Basic {auth}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return {
                "ok": True,
                "user": data.get("emailAddress", email),
                "display_name": data.get("displayName", ""),
            }
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"Jira returned {e.code}: {e.reason}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _test_telegram(creds: dict[str, Any]) -> dict[str, Any]:
    from agent.channels.telegram import TelegramAdapter

    bot_token = creds.get("bot_token", "")
    if not bot_token:
        return {"ok": False, "error": "Bot token is required"}
    adapter = TelegramAdapter(bot_token)
    info = adapter.get_bot_info()
    if "error" in info:
        return {"ok": False, "error": info["error"]}
    return {
        "ok": True,
        "bot_name": f"@{info.get('username', '')}",
        "display_name": info.get("first_name", ""),
    }


def _test_whatsapp(creds: dict[str, Any]) -> dict[str, Any]:
    from agent.channels.whatsapp import WhatsAppAdapter

    api_token = creds.get("api_token", "")
    if not api_token:
        return {"ok": False, "error": "API token is required"}
    adapter = WhatsAppAdapter(api_token)
    health = adapter.get_health()
    if "error" in health:
        return {"ok": False, "error": health["error"]}
    return {
        "ok": True,
        "status": health.get("status", {}).get("text", "connected"),
        "phone": health.get("contacts", {}).get("phone", ""),
    }
