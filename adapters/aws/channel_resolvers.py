"""AWS channel adapter resolvers — Teams, Telegram, WhatsApp.

Looks up tenant by channel identifier, fetches credentials from Secrets
Manager, and instantiates the appropriate ChannelAdapter. Returns None if
credentials are incomplete or the tenant cannot be resolved.
"""

import logging
from typing import Any

from agent.channels.teams import TeamsAdapter
from agent.channels.telegram import TelegramAdapter

logger = logging.getLogger("t3nets.aws.channel_resolvers")


class ChannelResolvers:
    def __init__(self, tenants: Any, secrets: Any) -> None:
        self.tenants = tenants
        self.secrets = secrets

    async def get_teams(self, bot_app_id: str) -> TeamsAdapter | None:
        try:
            tenant = await self.tenants.get_by_channel_id("teams", bot_app_id)
        except Exception:
            try:
                all_tenants = await self.tenants.list_tenants()
                tenant = None
                for t in all_tenants:
                    try:
                        creds = await self.secrets.get(t.tenant_id, "teams")
                        if creds.get("app_id") == bot_app_id:
                            tenant = t
                            await self.tenants.set_channel_mapping(t.tenant_id, "teams", bot_app_id)
                            break
                    except Exception:
                        continue
                if tenant is None:
                    return None
            except Exception:
                return None

        if tenant is None:
            return None

        try:
            creds = await self.secrets.get(tenant.tenant_id, "teams")
            app_id = creds.get("app_id", "")
            app_secret = creds.get("app_secret", "")
            if not app_id or not app_secret:
                logger.error(f"Incomplete Teams credentials for tenant {tenant.tenant_id}")
                return None
            return TeamsAdapter(app_id, app_secret)
        except Exception as e:
            logger.error(f"Failed to load Teams credentials: {e}")
            return None

    async def get_telegram(self, token_hash: str) -> TelegramAdapter | None:
        if not token_hash or token_hash == "webhook":
            logger.warning("No token hash in Telegram webhook URL")
            return None
        try:
            tenant = await self.tenants.get_by_channel_id("telegram", token_hash)
            creds = await self.secrets.get(tenant.tenant_id, "telegram")
            bot_token = creds.get("bot_token", "")
            if bot_token:
                return TelegramAdapter(bot_token, creds.get("webhook_secret", ""))
        except Exception as e:
            logger.warning(f"Telegram channel mapping lookup failed: {e}")
        return None

    async def get_whatsapp(self, token_hash: str) -> Any:
        from agent.channels.whatsapp import WhatsAppAdapter

        if not token_hash or token_hash == "webhook":
            logger.warning("No token hash in WhatsApp webhook URL")
            return None
        try:
            tenant = await self.tenants.get_by_channel_id("whatsapp", token_hash)
            creds = await self.secrets.get(tenant.tenant_id, "whatsapp")
            api_token = creds.get("api_token", "")
            if api_token:
                return WhatsAppAdapter(api_token, creds.get("webhook_secret", ""))
        except Exception as e:
            logger.warning(f"WhatsApp channel mapping lookup failed: {e}")
        return None
