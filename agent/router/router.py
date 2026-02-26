"""
Router — the brain of T3nets.

Receives every message, resolves tenant, loads context, calls Claude,
and either responds directly or dispatches skill execution.
"""

import logging

from agent.models.message import ChannelType, InboundMessage, OutboundMessage
from agent.models.context import RequestContext
from agent.channels.base import ChannelRegistry
from agent.skills.registry import SkillRegistry
from agent.interfaces.ai_provider import AIProvider
from agent.interfaces.conversation_store import ConversationStore
from agent.interfaces.event_bus import EventBus
from agent.interfaces.tenant_store import TenantStore

logger = logging.getLogger(__name__)


class Router:
    """
    Central message handler. Cloud-agnostic — all dependencies injected.
    """

    def __init__(
        self,
        channels: ChannelRegistry,
        skills: SkillRegistry,
        ai: AIProvider,
        memory: ConversationStore,
        events: EventBus,
        tenants: TenantStore,
    ):
        self.channels = channels
        self.skills = skills
        self.ai = ai
        self.memory = memory
        self.events = events
        self.tenants = tenants

    async def handle_message(
        self,
        channel_type: ChannelType,
        raw_event: dict,
    ) -> None:
        """
        Main entry point. Handles a single inbound message end-to-end.
        """

        # 1. Resolve tenant
        tenant = await self._resolve_tenant(channel_type, raw_event)
        if not tenant or not tenant.is_active():
            logger.warning(f"Tenant not found or inactive for {channel_type.value}")
            return

        # 2. Parse inbound message
        adapter = self.channels.get(channel_type)
        message = adapter.parse_inbound(raw_event)

        # 3. Resolve user within tenant
        user = await self.tenants.get_user_by_channel_identity(
            tenant.tenant_id, channel_type.value, message.channel_user_id
        )
        if not user:
            # Unknown user — could auto-create or ignore
            logger.info(f"Unknown user {message.channel_user_id} for tenant {tenant.tenant_id}")
            # For prototype: create a basic user record
            from agent.models.tenant import TenantUser
            user = TenantUser(
                user_id=message.channel_user_id,
                tenant_id=tenant.tenant_id,
                email=message.user_email or "",
                display_name=message.user_display_name,
                role="member",
                channel_identities={channel_type.value: message.channel_user_id},
            )

        # 4. Build request context
        ctx = RequestContext(
            tenant=tenant,
            user=user,
            channel=channel_type,
            conversation_id=message.conversation_id,
        )
        logger.info(f"{ctx.log_prefix()} Handling: {message.text[:100]}")

        # 5. Acknowledge (if channel supports it)
        await adapter.send_acknowledgment(message.conversation_id)

        # 6. Load conversation history
        history = await self.memory.get_conversation(
            ctx.tenant_id,
            ctx.conversation_id,
            max_turns=tenant.settings.max_conversation_history,
        )

        # 7. Get tenant's enabled skills as Claude tools
        tools = self.skills.get_tools_for_tenant(ctx)

        # 8. Build system prompt
        system_prompt = self._build_system_prompt(ctx)

        # 9. Call Claude
        messages = history + [{"role": "user", "content": message.text}]
        response = await self.ai.chat(
            model=tenant.settings.ai_model,
            system=system_prompt,
            messages=messages,
            tools=tools,
            max_tokens=tenant.settings.max_tokens_per_message,
        )

        logger.info(
            f"{ctx.log_prefix()} AI response: "
            f"tool_use={response.has_tool_use}, "
            f"tokens={response.input_tokens}+{response.output_tokens}"
        )

        # 10a. Direct response (no tool needed)
        if not response.has_tool_use:
            outbound = OutboundMessage(
                channel=channel_type,
                conversation_id=message.conversation_id,
                recipient_id=message.channel_user_id,
                text=response.text or "",
            )
            await adapter.send_response(outbound)
            await self.memory.save_turn(
                ctx.tenant_id,
                ctx.conversation_id,
                message.text,
                response.text or "",
            )

        # 10b. Skill invocation (async via event bus)
        else:
            for tool_call in response.tool_calls:
                logger.info(
                    f"{ctx.log_prefix()} Invoking skill: {tool_call.tool_name}"
                )
                await self.events.publish_skill_invocation(
                    tenant_id=ctx.tenant_id,
                    skill_name=tool_call.tool_name,
                    params=tool_call.tool_params,
                    session_id=ctx.conversation_id,
                    request_id=ctx.request_id,
                    reply_channel=channel_type.value,
                    reply_target=message.channel_user_id,
                )

    async def _resolve_tenant(self, channel_type, raw_event):
        """Resolve which tenant this message belongs to."""
        try:
            if channel_type in (ChannelType.DASHBOARD, ChannelType.API):
                tenant_id = raw_event.get("_tenant_id", "")
                if tenant_id:
                    return await self.tenants.get_tenant(tenant_id)
            else:
                # Channel-specific resolution
                channel_id_map = {
                    ChannelType.TEAMS: lambda e: e.get("recipient", {}).get("id", ""),
                    ChannelType.SLACK: lambda e: e.get("team_id", ""),
                    ChannelType.WHATSAPP: lambda e: e.get("To", ""),
                }
                extractor = channel_id_map.get(channel_type)
                if extractor:
                    channel_specific_id = extractor(raw_event)
                    return await self.tenants.get_by_channel_id(
                        channel_type.value, channel_specific_id
                    )
        except Exception as e:
            logger.error(f"Tenant resolution failed: {e}")
        return None

    def _build_system_prompt(self, ctx: RequestContext) -> str:
        """Build the system prompt with tenant context."""
        base = f"""You are an AI assistant for {ctx.tenant.name} on the T3nets platform.

You are talking to {ctx.user.display_name} ({ctx.user.email}).

{ctx.tenant.settings.system_prompt_override or "Be direct, helpful, and action-oriented. Flag risks early. Suggest actions."}

Communication channel: {ctx.channel.value}
"""
        # Channel-specific adaptations
        if ctx.channel == ChannelType.SMS:
            base += "\nKeep responses concise — under 160 characters when possible."
        elif ctx.channel == ChannelType.VOICE:
            base += "\nUse natural spoken language. Simplify numbers. Keep it brief."

        base += """
When you need to take action or fetch data, use the available tools.
When you can answer directly from conversation context, do so without tools.
Be honest about what you don't know."""

        return base
