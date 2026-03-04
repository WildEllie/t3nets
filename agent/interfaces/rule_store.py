"""
Rule Store Interface — persistence for AI-generated tenant rule sets.

Implementations: SQLiteRuleStore (local), DynamoDBRuleStore (AWS).
"""

from abc import ABC, abstractmethod
from typing import Optional

from agent.router.models import TenantRuleSet


class RuleStore(ABC):
    """Abstract base for storing and loading AI-generated rule sets."""

    @abstractmethod
    async def save_rule_set(self, rule_set: TenantRuleSet) -> None:
        """Persist a rule set, overwriting any existing one for this tenant."""
        ...

    @abstractmethod
    async def load_rule_set(self, tenant_id: str) -> Optional[TenantRuleSet]:
        """Load the current rule set for a tenant. Returns None if none exists."""
        ...
