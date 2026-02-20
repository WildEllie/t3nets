"""
Event Bus Interface

Cloud-agnostic abstraction for async event publishing.
Implementations: EventBridgeBus (AWS), DirectBus (local), etc.
"""

from abc import ABC, abstractmethod


class EventBus(ABC):
    """
    Abstract base class for event publishing.

    Used by the router to dispatch skill invocations asynchronously.
    The local implementation calls skills directly (no queue).
    """

    @abstractmethod
    async def publish(
        self,
        source: str,
        detail_type: str,
        detail: dict,
    ) -> None:
        """
        Publish an event.

        Args:
            source: Event source (e.g., "agent.router")
            detail_type: Event type (e.g., "skill.invoke")
            detail: Event payload (must include tenant_id)
        """
        ...

    async def publish_skill_invocation(
        self,
        tenant_id: str,
        skill_name: str,
        params: dict,
        session_id: str,
        request_id: str,
        reply_channel: str,
        reply_target: str,
    ) -> None:
        """
        Convenience method for the most common event type.
        """
        await self.publish(
            source="agent.router",
            detail_type="skill.invoke",
            detail={
                "tenant_id": tenant_id,
                "skill_name": skill_name,
                "params": params,
                "session_id": session_id,
                "request_id": request_id,
                "reply_channel": reply_channel,
                "reply_target": reply_target,
            },
        )
