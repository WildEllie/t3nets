"""Webhook registration helpers — Telegram and WhatsApp.

Called from the IntegrationHandlers credentials-saved hook to register the
external service's webhook endpoint pointing at this server. The token-hash
URL slug is also persisted as a tenant channel mapping (handled by the caller).
"""

import hashlib
import logging
import os
from typing import Any

from agent.channels.telegram import TelegramAdapter

logger = logging.getLogger("t3nets.aws.webhook_registration")


def _resolve_base_url(request_headers: dict[str, str]) -> str:
    host = request_headers.get("host", "")
    if host:
        return f"https://{host}"
    return os.environ.get("API_BASE_URL", "")


def register_telegram_webhook(request_headers: dict[str, str], creds: dict[str, Any]) -> None:
    bot_token = creds.get("bot_token", "")
    if not bot_token:
        return
    try:
        base_url = _resolve_base_url(request_headers)
        if not base_url:
            logger.warning("Cannot register Telegram webhook: no Host header or API_BASE_URL")
            return
        token_hash = hashlib.sha256(bot_token.encode()).hexdigest()[:16]
        webhook_url = f"{base_url}/api/channels/telegram/webhook/{token_hash}"
        adapter = TelegramAdapter(bot_token, creds.get("webhook_secret", ""))
        result = adapter.register_webhook(webhook_url)
        logger.info(f"Telegram webhook registration: {result}")
    except Exception as e:
        logger.error(f"Failed to register Telegram webhook: {e}")


def register_whatsapp_webhook(request_headers: dict[str, str], creds: dict[str, Any]) -> None:
    from agent.channels.whatsapp import WhatsAppAdapter

    api_token = creds.get("api_token", "")
    if not api_token:
        return
    try:
        base_url = _resolve_base_url(request_headers)
        if not base_url:
            logger.warning("Cannot register WhatsApp webhook: no Host header or API_BASE_URL")
            return
        token_hash = hashlib.sha256(api_token.encode()).hexdigest()[:16]
        webhook_url = f"{base_url}/api/channels/whatsapp/webhook/{token_hash}"
        webhook_secret = creds.get("webhook_secret", "")
        if not webhook_secret:
            import secrets as _secrets

            webhook_secret = _secrets.token_urlsafe(24)
            creds["webhook_secret"] = webhook_secret
        adapter = WhatsAppAdapter(api_token, webhook_secret)
        result = adapter.register_webhook(webhook_url)
        logger.info(f"WhatsApp webhook registration: {result}")
    except Exception as e:
        logger.error(f"Failed to register WhatsApp webhook: {e}")
