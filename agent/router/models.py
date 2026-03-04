"""
Router data models — AI-generated rule sets and training data.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class SkillRules:
    """AI-generated routing rules for a single skill."""

    skill_name: str
    detection_patterns: list[str]  # regex strings that detect this skill is being requested
    action_rules: list[tuple[str, str]]  # (regex_pattern, action_name) — first match wins
    disambiguation_notes: str = ""  # AI's reasoning for pattern choices


@dataclass
class TenantRuleSet:
    """Complete AI-generated rule set for a tenant's enabled skill combination."""

    tenant_id: str
    version: int
    generated_at: str  # ISO timestamp
    skill_rules: dict[str, SkillRules]  # skill_name → SkillRules
    disabled_skill_catchers: dict[str, list[str]]  # skill_name → detection patterns
    generation_model: str = ""


@dataclass
class TrainingExample:
    """
    A Tier 2 (AI) routing decision saved as training data for future rule improvement.

    Accumulates when Claude handles requests that the compiled engine didn't catch.
    Admins can annotate these in Phase 5b to drive rule recalculation.
    """

    tenant_id: str
    example_id: str
    message_text: str
    timestamp: str  # ISO timestamp
    matched_skill: Optional[str] = None  # skill AI chose (None = freeform chat response)
    matched_action: Optional[str] = None
    was_disabled_skill: bool = False
    confidence: Optional[float] = None
    admin_override_skill: Optional[str] = None  # set in Phase 5b by admin
    admin_override_action: Optional[str] = None  # set in Phase 5b by admin
