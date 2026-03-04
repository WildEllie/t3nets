"""
Compiled Rule Engine — in-memory regex routing for a specific tenant.

Replaces the hand-maintained RuleBasedRouter with AI-generated, per-tenant patterns.
Loaded from a TenantRuleSet stored in the rule store.

Usage:
    engine = CompiledRuleEngine(rule_set, skills)
    match = engine.match(text, tenant.settings.enabled_skills)
    if match:
        # execute match.skill_name with match.params
    else:
        disabled = engine.check_disabled_skill(text)
        if disabled:
            # inform user that skill is unavailable
        else:
            # fall through to Claude (Tier 2)
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from agent.router.models import TenantRuleSet
from agent.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


@dataclass
class RouteMatch:
    """Result of compiled-engine routing."""

    skill_name: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    raw_mode: bool = False


# ---------------------------------------------------------------------------
# Module-level helpers (same for all tenants)
# ---------------------------------------------------------------------------

_RAW_FLAG_PATTERN = re.compile(r"\s*--raw\s*", re.IGNORECASE)

_CONVERSATIONAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"^(hi|hey|hello|yo|sup|howdy|good\s*(morning|afternoon|evening))[\s!?.]*$",
        r"^(thanks|thank you|thx|ty|cheers|appreciated)[\s!?.]*$",
        r"^(bye|goodbye|see you|later|cya)[\s!?.]*$",
        r"^(yes|no|yep|nope|yeah|nah|ok|okay|sure|got it)[\s!?.]*$",
        r"^(help|what can you do|who are you|what are you)[\s!?.]*$",
        r"^(lol|haha|heh|nice|cool|great|awesome)[\s!?.]*$",
    ]
]


def strip_raw_flag(text: str) -> tuple[str, bool]:
    """
    Detect and strip the --raw debug flag from a message.
    Returns (cleaned_text, is_raw).
    """
    if _RAW_FLAG_PATTERN.search(text):
        cleaned = _RAW_FLAG_PATTERN.sub(" ", text).strip()
        return cleaned, True
    return text, False


def is_conversational(text: str) -> bool:
    """
    Detect purely conversational messages (greetings, thanks, etc.)
    that should go to Claude directly without skill routing.
    """
    text_lower = text.lower().strip()
    return any(p.match(text_lower) for p in _CONVERSATIONAL_PATTERNS)


# ---------------------------------------------------------------------------
# CompiledRuleEngine
# ---------------------------------------------------------------------------


class CompiledRuleEngine:
    """
    In-memory compiled regex engine loaded from an AI-generated TenantRuleSet.

    Each tenant gets its own engine, optimized for its specific enabled skill
    combination. This is the Tier 1 router: $0 cost, <1ms latency.
    """

    def __init__(self, rule_set: TenantRuleSet, skills: SkillRegistry) -> None:
        self._rule_set = rule_set
        self._skills = skills

        # Compiled detection patterns per skill: skill_name → [pattern, ...]
        self._detection: dict[str, list[re.Pattern[str]]] = {}
        # Compiled action rules per skill: skill_name → [(pattern, action), ...]
        self._action_rules: dict[str, list[tuple[re.Pattern[str], str]]] = {}
        # Compiled disabled-skill catcher patterns: skill_name → [pattern, ...]
        self._disabled: dict[str, list[re.Pattern[str]]] = {}

        self._compile()

        total_detection = sum(len(v) for v in self._detection.values())
        total_disabled = sum(len(v) for v in self._disabled.values())
        logger.info(
            f"CompiledRuleEngine[{rule_set.tenant_id}]: "
            f"{len(self._detection)} skills, "
            f"{total_detection} detection patterns, "
            f"{total_disabled} disabled-skill catcher patterns"
        )

    def _compile(self) -> None:
        """Compile all regex strings into pattern objects."""
        for skill_name, rules in self._rule_set.skill_rules.items():
            self._detection[skill_name] = [
                re.compile(p, re.IGNORECASE) for p in rules.detection_patterns
            ]
            self._action_rules[skill_name] = [
                (re.compile(p, re.IGNORECASE), action) for p, action in rules.action_rules
            ]
        for skill_name, patterns in self._rule_set.disabled_skill_catchers.items():
            self._disabled[skill_name] = [re.compile(p, re.IGNORECASE) for p in patterns]

    def match(self, text: str, enabled_skills: list[str]) -> Optional[RouteMatch]:
        """
        Try to match a message to an enabled skill using compiled patterns.

        Scoring: count how many detection patterns fire for each skill.
        The skill with the most hits wins (ties go to the first in the list).
        Returns None when no patterns fire → caller falls through to AI.
        """
        text_lower = text.lower().strip()
        best_name: Optional[str] = None
        best_hits = 0

        for skill_name in enabled_skills:
            patterns = self._detection.get(skill_name)
            if not patterns:
                continue
            hits = sum(1 for p in patterns if p.search(text_lower))
            if hits > best_hits:
                best_hits = hits
                best_name = skill_name

        if not best_name:
            logger.info("CompiledRuleEngine: no match → AI fallback")
            return None

        action = self._determine_action(text_lower, best_name)
        params = self._extract_params(text, text_lower, best_name, action)
        logger.info(f"CompiledRuleEngine: {best_name}/{action} (hits={best_hits})")
        return RouteMatch(skill_name=best_name, action=action, params=params)

    def check_disabled_skill(self, text: str) -> Optional[str]:
        """
        Check if a message clearly targets a disabled skill.

        Returns the skill name if a catcher pattern fires, None otherwise.
        Called after match() returns None to provide a helpful "not enabled"
        response without a full Claude call.
        """
        text_lower = text.lower().strip()
        for skill_name, patterns in self._disabled.items():
            if any(p.search(text_lower) for p in patterns):
                return skill_name
        return None

    def supports_raw(self, skill_name: str) -> bool:
        """Check if a skill supports --raw debug output."""
        skill = self._skills.get_skill(skill_name)
        return skill.supports_raw if skill else False

    # --- Private helpers ---

    def _determine_action(self, text_lower: str, skill_name: str) -> str:
        """Select the action for a matched skill. First matching action rule wins."""
        for pattern, action in self._action_rules.get(skill_name, []):
            if pattern.search(text_lower):
                return action
        # Fallback: last action rule (broadest/default), or "status"
        rules = self._action_rules.get(skill_name, [])
        return rules[-1][1] if rules else "status"

    def _extract_params(
        self,
        text_original: str,
        text_lower: str,
        skill_name: str,
        action: str,
    ) -> dict[str, Any]:
        """Extract skill parameters from the message text."""
        params: dict[str, Any] = {"action": action}

        # Extract assignee email for "mine" action
        if action == "mine":
            email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text_lower)
            if email_match:
                params["assignee_email"] = email_match.group()

        # Extract release name for release_notes summarize action
        if skill_name == "release_notes" and action == "summarize":
            release_name = _extract_release_name(text_original)
            if release_name:
                params["release_name"] = release_name

        return params


def _extract_release_name(text: str) -> str:
    """Extract a release/version name from user text."""
    # Quoted names first: "Nova 2.0", 'v1.5'
    quoted = re.search(r'["\']([^"\']+)["\']', text)
    if quoted:
        return quoted.group(1)

    # Explicit version patterns: v1.0, 2.5.0, v2-rc1
    version_match = re.search(r"\bv?\d+\.\d+(?:\.\d+)?(?:-\w+)?\b", text)
    if version_match:
        return version_match.group()

    # "release <name>" or "version <name>"
    named = re.search(
        r"\b(?:release|version)\s+([A-Za-z0-9][A-Za-z0-9._\- ]*)",
        text,
    )
    if named:
        candidate = named.group(1).strip()
        for stop in ("for", "of", "in", "to", "from", "with", "and", "notes", "note"):
            idx = candidate.lower().find(f" {stop} ")
            if idx >= 0:
                candidate = candidate[:idx].strip()
                break
        if candidate:
            return candidate

    return ""
