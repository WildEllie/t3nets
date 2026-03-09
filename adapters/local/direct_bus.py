"""
Local Event Bus — Direct function call.

For local development. No EventBridge/SQS.
When a skill is invoked, it's called directly and the result
is fed back to Claude immediately (synchronous flow).
"""

import asyncio
import inspect
import logging
from typing import Any

from agent.interfaces.event_bus import EventBus
from agent.interfaces.secrets_provider import SecretsProvider
from agent.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


class DirectBus(EventBus):
    """
    Calls skill workers directly instead of publishing to a queue.
    Results are returned synchronously — no response handler needed.
    """

    def __init__(
        self,
        skills: SkillRegistry,
        secrets: SecretsProvider,
        context: dict[str, Any] | None = None,
    ):
        self.skills = skills
        self.secrets = secrets
        self.context: dict[str, Any] = context or {}
        self._pending_results: dict[str, dict[str, Any]] = {}

    async def publish(
        self,
        source: str,
        detail_type: str,
        detail: dict[str, Any],
    ) -> None:
        """
        Instead of publishing to EventBridge, execute the skill immediately.
        Store the result for the router to pick up.
        """
        if detail_type != "skill.invoke":
            logger.warning(f"DirectBus ignoring event type: {detail_type}")
            return

        skill_name = detail["skill_name"]
        tenant_id = detail["tenant_id"]
        params = detail.get("params", {})
        request_id = detail["request_id"]

        logger.info(f"DirectBus: executing skill '{skill_name}' for tenant '{tenant_id}'")

        try:
            # Get the worker function
            worker_fn = self.skills.get_worker(skill_name)

            # Get tenant's secrets for this skill's integration
            skill = self.skills.get_skill(skill_name)
            secrets: dict[str, Any] = {}
            if skill and skill.requires_integration:
                try:
                    secrets = await self.secrets.get(tenant_id, skill.requires_integration)
                except Exception as e:
                    logger.error(f"Failed to get secrets for {skill.requires_integration}: {e}")
                    self._pending_results[request_id] = {
                        "error": f"Integration not configured: {skill.requires_integration}"
                    }
                    return

            # Build runtime context (includes tenant_id for this request)
            runtime_ctx = {**self.context, "tenant_id": tenant_id}

            # Execute the skill worker — pass context if it accepts 3+ args
            sig = inspect.signature(worker_fn)
            if len(sig.parameters) >= 3:
                result = worker_fn(params, secrets, runtime_ctx)
            else:
                result = worker_fn(params, secrets)

            # Support async workers (coroutines)
            if asyncio.iscoroutine(result):
                result = await result

            self._pending_results[request_id] = result

            logger.info(f"DirectBus: skill '{skill_name}' completed for request {request_id[:8]}")

        except Exception as e:
            logger.error(f"DirectBus: skill '{skill_name}' failed: {e}")
            self._pending_results[request_id] = {"error": str(e)}

    def get_result(self, request_id: str) -> dict[str, Any] | None:
        """Retrieve and consume a skill result. Used by the local router."""
        return self._pending_results.pop(request_id, None)
