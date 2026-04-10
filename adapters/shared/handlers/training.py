"""
Shared training and rules admin handlers.

Extracted from adapters/aws/server.py and adapters/local/dev_server.py.
Covers:
  - GET    /api/admin/training          — list training examples
  - PATCH  /api/admin/training/{id}     — annotate an example
  - DELETE /api/admin/training/{id}     — delete an example
  - POST   /api/admin/rules/rebuild     — trigger rule rebuild
  - GET    /api/admin/rules/status      — current rule-set info
"""

from __future__ import annotations

from typing import Any

from agent.interfaces.rule_store import RuleStore
from agent.interfaces.training_store import TrainingStore
from agent.router.models import TenantRuleSet


class TrainingHandlers:
    """Reusable handler logic for training-data and rules admin endpoints.

    Parameters
    ----------
    training_store:
        Persistent store for routing training examples.
    rule_store:
        Persistent store for AI-generated rule sets.
    compiled_engines:
        Mutable mapping of ``tenant_id -> CompiledRuleEngine`` kept by the
        host server.  Only read here (for the ``/rules/status`` response).
    rebuild_rules_fn:
        Async callable ``(tenant_id) -> None`` that triggers a background
        rule rebuild.  The host server owns the actual implementation
        because it needs access to the AI provider and skill registry.
    """

    def __init__(
        self,
        training_store: TrainingStore,
        rule_store: RuleStore,
        compiled_engines: dict[str, Any],
        rebuild_rules_fn: Any,  # Callable[[str], Coroutine[Any, Any, None]]
    ) -> None:
        self._training = training_store
        self._rules = rule_store
        self._engines = compiled_engines
        self._rebuild_rules = rebuild_rules_fn

    # ------------------------------------------------------------------
    # Training examples
    # ------------------------------------------------------------------

    async def list_training(
        self,
        tenant_id: str,
        limit: int = 50,
        unannotated: bool = False,
    ) -> tuple[dict[str, Any], int]:
        """Return recent training examples for *tenant_id*."""
        examples = await self._training.list_examples(tenant_id, limit=limit)
        if unannotated:
            examples = [e for e in examples if not e.admin_override_skill]
        return {
            "examples": [
                {
                    "example_id": e.example_id,
                    "message_text": e.message_text,
                    "timestamp": e.timestamp,
                    "matched_skill": e.matched_skill,
                    "matched_action": e.matched_action,
                    "was_disabled_skill": e.was_disabled_skill,
                    "confidence": e.confidence,
                    "admin_override_skill": e.admin_override_skill,
                    "admin_override_action": e.admin_override_action,
                }
                for e in examples
            ],
            "count": len(examples),
        }, 200

    async def annotate_training(
        self,
        tenant_id: str,
        example_id: str,
        body: dict[str, Any],
    ) -> tuple[dict[str, Any], int]:
        """Set admin override on a single training example."""
        skill = body.get("skill", "")
        action = body.get("action", "")
        found = await self._training.annotate_example(tenant_id, example_id, skill, action)
        if not found:
            return {"error": "Example not found"}, 404
        return {"example_id": example_id, "annotated": True}, 200

    async def delete_training(
        self,
        tenant_id: str,
        example_id: str,
    ) -> tuple[dict[str, Any], int]:
        """Remove a single training example."""
        found = await self._training.delete_example(tenant_id, example_id)
        if not found:
            return {"error": "Example not found"}, 404
        return {"example_id": example_id, "deleted": True}, 200

    # ------------------------------------------------------------------
    # Rules admin
    # ------------------------------------------------------------------

    async def rebuild_rules(
        self,
        tenant_id: str,
    ) -> tuple[dict[str, Any], int]:
        """Kick off an async rule rebuild and return immediately.

        The caller is responsible for scheduling ``self._rebuild_rules``
        in the background (e.g. via ``_fire_and_forget``) **before** or
        **after** calling this method.  This method only builds the
        JSON response — it does **not** start the rebuild itself so that
        the host server retains full control over task scheduling.
        """
        return {"rebuilding": True, "tenant_id": tenant_id}, 200

    async def rules_status(
        self,
        tenant_id: str,
    ) -> tuple[dict[str, Any], int]:
        """Return current rule-set metadata for *tenant_id*."""
        rule_set: TenantRuleSet | None = await self._rules.load_rule_set(tenant_id)
        engine = self._engines.get(tenant_id)
        return {
            "tenant_id": tenant_id,
            "version": rule_set.version if rule_set else 0,
            "generated_at": rule_set.generated_at if rule_set else None,
            "skill_count": len(rule_set.rules) if rule_set else 0,
            "engine_loaded": engine is not None,
        }, 200
