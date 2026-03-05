"""
Training Store Interface — persistence for AI routing decisions.

Every Tier 2 (Claude) routing decision is logged as a TrainingExample.
Admins can annotate these in Phase 5b to improve rule accuracy over time.

Implementations: SQLiteTrainingStore (local), DynamoDBTrainingStore (AWS).
"""

from abc import ABC, abstractmethod

from agent.router.models import TrainingExample


class TrainingStore(ABC):
    """Abstract base for storing training examples from AI routing decisions."""

    @abstractmethod
    async def log_example(self, example: TrainingExample) -> None:
        """Save a training example. Silently ignored on duplicate example_id."""
        ...

    @abstractmethod
    async def list_examples(
        self,
        tenant_id: str,
        limit: int = 100,
    ) -> list[TrainingExample]:
        """
        Return recent training examples for a tenant, newest first.
        Used by Phase 5b admin tools and rule recalculation.
        """
        ...

    @abstractmethod
    async def annotate_example(
        self,
        tenant_id: str,
        example_id: str,
        skill: str,
        action: str,
    ) -> bool:
        """Set admin_override_skill + admin_override_action. Returns False if not found."""
        ...

    @abstractmethod
    async def delete_example(self, tenant_id: str, example_id: str) -> bool:
        """Remove a training example. Returns False if not found."""
        ...
