"""
Rule-Based Router — fast, free routing for obvious requests.

Two-tier routing:
  Tier 1: Pattern matching on skill triggers (free, <1ms)
  Tier 2: Claude AI routing (paid, ~1-3 seconds)

If Tier 1 matches with high confidence, skip Claude for routing entirely.
Claude is still used to FORMAT the response after the skill returns data.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from agent.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


@dataclass
class RouteMatch:
    """Result of rule-based routing."""

    skill_name: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0  # 0.0 to 1.0
    raw_mode: bool = False  # --raw flag detected


@dataclass
class RuleSet:
    """Rules for matching a single skill."""

    skill_name: str
    supports_raw: bool = False
    # Exact trigger phrases (case-insensitive)
    triggers: list[str] = field(default_factory=list)
    # Regex patterns for skill matching
    patterns: list[re.Pattern[str]] = field(default_factory=list)
    # Action routing rules: list of (regex_pattern, action_name)
    action_rules: list[tuple[re.Pattern[str], str]] = field(default_factory=list)


# --- Action routing rules per skill ---
# These are REGEX patterns, not exact substrings.
# First match wins, so put specific patterns before general ones.

SKILL_ACTION_RULES: dict[str, list[tuple[str, str]]] = {
    "ping": [
        (r".*", "ping"),  # single action — any match routes here
    ],
    "sprint_status": [
        # Blockers
        (r"\bblock(ed|er|ing|s)?\b", "blockers"),
        (r"\bimpediment\b", "blockers"),
        (r"\bstuck\b", "blockers"),
        (r"\bflagged\b", "blockers"),
        # Mine / personal tickets
        (r"\bmy\b.*\b(ticket|task|work|item|issue|story|bug|stor(y|ies))\b", "mine"),
        (r"\b(ticket|task|work|item|issue)s?\b.*\b(for me|assigned to me|mine)\b", "mine"),
        (r"\bwhat\b.*\b(am i|i am|i'm)\b.*\b(working|doing)\b", "mine"),
        (r"\bassigned to me\b", "mine"),
        (r"\bmy\b.*\bjira\b", "mine"),
        (r"\bjira\b.*\bmy\b", "mine"),
        (r"\bwhat.*\bi\s+(have|need|should)\b", "mine"),
        # Status (default — broadest patterns last)
        (r"\bstatus\b", "status"),
        (r"\bsprint\b", "status"),
        (r"\btrack\b", "status"),
        (r"\bprogress\b", "status"),
        (r"\boverview\b", "status"),
        (r"\bhealth\b", "status"),
        (r"\bremaining\b", "status"),
        (r"\bhow.*we\b", "status"),
        (r"\bare we\b", "status"),
        (r"\brelease\s*(status|ready|readiness)\b", "status"),
        (r"\bburn\s*down\b", "status"),
        (r"\bvelocity\b", "status"),
        (r"\bscope\b", "status"),
        (r"\bleft\b.*\bsprint\b", "status"),
        (r"\bsprint\b.*\bleft\b", "status"),
    ],
    "release_notes": [
        # List releases
        (r"\blist\b.*\brelease", "list_releases"),
        (r"\bshow\b.*\b(version|release)s\b", "list_releases"),
        (r"\bwhat\s+(version|release)s\b", "list_releases"),
        (r"\bfix\s*version", "list_releases"),
        (r"\ball\b.*\brelease", "list_releases"),
        (r"\bavailable\b.*\brelease", "list_releases"),
        # Summarize (more specific patterns first)
        (r"\brelease\s*notes?\b", "summarize"),
        (r"\bchangelog\b", "summarize"),
        (r"\bwhat\b.*\bship(ped)?\b", "summarize"),
        (r"\bwhat'?s?\s+in\b.*\b(release|version)\b", "summarize"),
        (r"\bgenerate\b.*\brelease\b", "summarize"),
        (r"\bsummariz\w*\b.*\brelease\b", "summarize"),
        (r"\brelease\b.*\bsummar\w*\b", "summarize"),
        # Fallback
        (r"\brelease\b", "summarize"),
    ],
}

# --- Skill detection patterns (broader than action rules) ---
# These determine IF a skill should handle the message.
# Action rules above determine WHICH action within the skill.

SKILL_PATTERNS: dict[str, list[str]] = {
    "ping": [
        r"\bping\b",
        r"\btest\s*model\b",
        r"\bare\s*you\s*alive\b",
        r"\bhealth\s*check\b",
        r"\bmodel\s*test\b",
    ],
    "sprint_status": [
        r"\bsprint\b",
        r"\bblock(ed|er|ing|s)?\b",
        r"\bon\s*track\b",
        r"\brelease\s*(status|ready|readiness)\b",
        r"\bticket\b",
        r"\btask\b",
        r"\bstory\b",
        r"\bbug\b",
        r"\bissue\b",
        r"\bjira\b",
        r"\bhow.*doing\b.*\b(sprint|team)\b",
        r"\bwhat'?s?\s+(left|remaining|blocked)\b",
        r"\bare we\s+(on track|going to|behind|ahead)\b",
        r"\bburn\s*down\b",
        r"\bvelocity\b",
        r"\bscope\b",
        r"\bmy\b.*\b(ticket|task|work|item|issue|story|bug)\b",
        r"\b(ticket|task|work|item|issue)s?\b.*\b(mine|for me|assigned)\b",
        r"\bmy\b.*\bjira\b",
        r"\bjira\b.*\b(work|ticket|task|item)\b",
        r"\bwhat\b.*\b(am i|i am|i'm)\b.*\b(working|doing)\b",
        r"\bwhat.*\bi\s+(have|need)\b.*\b(do|finish|complete)\b",
        r"\bsprint\s*(status|health|progress|overview|update)\b",
        r"\b(status|health|progress)\b.*\bsprint\b",
        r"\bdelivery\b.*\b(timeline|risk|status)\b",
    ],
    "release_notes": [
        r"\brelease\s*notes?\b",
        r"\bchangelog\b",
        r"\bwhat\b.*\bship(ped)?\b",
        r"\bwhat'?s?\s+in\b.*\b(release|version)\b",
        r"\blist\b.*\breleases?\b",
        r"\bfix\s*version\b",
        r"\bwhat\s+(did we|have we)\s+release\b",
        r"\bwhat\s+versions?\b",
        r"\bgenerate\b.*\b(release|changelog)\b",
        r"\bsummariz\w*\b.*\brelease\b",
        r"\brelease\b.*\bsummar\w*\b",
        r"\brelease\b(?!\s*(status|ready|readiness)\b)",
    ],
}


# Regex to detect and strip --raw flag
RAW_FLAG_PATTERN = re.compile(r"\s*--raw\s*", re.IGNORECASE)


def strip_raw_flag(text: str) -> tuple[str, bool]:
    """
    Check for --raw flag in message text.
    Returns (cleaned_text, is_raw).
    """
    if RAW_FLAG_PATTERN.search(text):
        cleaned = RAW_FLAG_PATTERN.sub(" ", text).strip()
        return cleaned, True
    return text, False


class RuleBasedRouter:
    """
    Fast pattern-matching router. Checks user messages against
    skill triggers and patterns before falling back to Claude.
    """

    def __init__(self, skills: SkillRegistry, confidence_threshold: float = 0.5):
        self.skills = skills
        self.confidence_threshold = confidence_threshold
        self._rule_sets: dict[str, RuleSet] = {}
        self._compiled_patterns: dict[str, list[re.Pattern[str]]] = {}
        self._build_rule_sets()

    def _build_rule_sets(self) -> None:
        """Build rule sets from skill definitions and built-in patterns."""
        for skill in self.skills.list_skills():
            rule_set = RuleSet(
                skill_name=skill.name,
                supports_raw=skill.supports_raw,
            )

            # Triggers from skill.yaml
            rule_set.triggers = [t.lower().strip() for t in skill.triggers]

            # Compile detection patterns
            pattern_strings = SKILL_PATTERNS.get(skill.name, [])
            rule_set.patterns = [re.compile(p, re.IGNORECASE) for p in pattern_strings]

            # Compile action routing rules
            action_rule_strings = SKILL_ACTION_RULES.get(skill.name, [])
            rule_set.action_rules = [
                (re.compile(pattern, re.IGNORECASE), action)
                for pattern, action in action_rule_strings
            ]

            self._rule_sets[skill.name] = rule_set

        total_rules = sum(
            len(rs.triggers) + len(rs.patterns) + len(rs.action_rules)
            for rs in self._rule_sets.values()
        )
        logger.info(
            f"RuleBasedRouter: loaded {len(self._rule_sets)} skill rule sets "
            f"({total_rules} total rules)"
        )

    def match(self, text: str, enabled_skills: list[str]) -> Optional[RouteMatch]:
        """
        Try to match a user message to a skill using rules.

        Returns:
            RouteMatch if confident, None if Claude should handle it
        """
        text_lower = text.lower().strip()
        best_match: Optional[RouteMatch] = None
        best_confidence = 0.0

        for skill_name, rule_set in self._rule_sets.items():
            if skill_name not in enabled_skills:
                continue

            confidence = self._score_match(text_lower, rule_set)

            if confidence > best_confidence:
                best_confidence = confidence
                action = self._determine_action(text_lower, rule_set)
                params = self._extract_params(text_lower, rule_set, action, text)
                best_match = RouteMatch(
                    skill_name=skill_name,
                    action=action,
                    params=params,
                    confidence=confidence,
                )

        if best_match and best_match.confidence >= self.confidence_threshold:
            logger.info(
                f"RuleBasedRouter: matched '{best_match.skill_name}' "
                f"action='{best_match.action}' confidence={best_match.confidence:.2f}"
            )
            return best_match

        best_summary = f"{best_match.skill_name} @ {best_confidence:.2f}" if best_match else "none"
        logger.info(
            f"RuleBasedRouter: no confident match (best: {best_summary}), falling back to Claude"
        )
        return None

    def supports_raw(self, skill_name: str) -> bool:
        """Check if a skill supports --raw output."""
        rule_set = self._rule_sets.get(skill_name)
        if rule_set:
            return rule_set.supports_raw
        # Also check skill definition directly (for AI-routed skills)
        skill = self.skills.get_skill(skill_name)
        return skill.supports_raw if skill else False

    def _score_match(self, text_lower: str, rule_set: RuleSet) -> float:
        """Score how well a message matches a rule set. Returns 0.0 to 1.0."""
        score = 0.0

        # Exact trigger match (highest confidence)
        for trigger in rule_set.triggers:
            if trigger in text_lower:
                ratio = len(trigger) / max(len(text_lower), 1)
                score = max(score, 0.7 + (ratio * 0.3))

        # Regex pattern match — count how many hit
        pattern_hits = sum(1 for p in rule_set.patterns if p.search(text_lower))
        if pattern_hits > 0:
            # 1 hit = 0.5, 2 hits = 0.65, 3+ hits = 0.75+
            pattern_score = min(0.5 + (pattern_hits * 0.15), 0.9)
            score = max(score, pattern_score)

        return min(score, 1.0)

    def _determine_action(self, text_lower: str, rule_set: RuleSet) -> str:
        """Determine which action to invoke using regex patterns."""
        for pattern, action in rule_set.action_rules:
            if pattern.search(text_lower):
                return action

        # Fallback: last action in the list (usually the broadest/default)
        if rule_set.action_rules:
            return rule_set.action_rules[-1][1]
        return "status"

    def _extract_params(
        self, text_lower: str, rule_set: RuleSet, action: str, text_original: str = ""
    ) -> dict[str, Any]:
        """Extract parameters from the message text."""
        params = {"action": action}

        # Extract email addresses (for "mine" action)
        if action == "mine":
            email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text_lower)
            if email_match:
                params["assignee_email"] = email_match.group()

        # Extract release name for release_notes skill (use original case)
        if rule_set.skill_name == "release_notes" and action == "summarize":
            release_name = self._extract_release_name(text_original or text_lower)
            if release_name:
                params["release_name"] = release_name

        return params

    @staticmethod
    def _extract_release_name(text: str) -> str:
        """Extract a release/version name from user text."""
        # Match quoted release names first: "Nova 2.0", 'Sprint 5', etc.
        quoted = re.search(r'["\']([^"\']+)["\']', text)
        if quoted:
            return quoted.group(1)

        # Match version patterns: v1.0, v2.5.0, 1.0.0, etc.
        version_match = re.search(r"\bv?\d+\.\d+(?:\.\d+)?(?:-\w+)?\b", text)
        if version_match:
            return version_match.group()

        # Match "release <name>" or "version <name>" patterns
        named = re.search(
            r"\b(?:release|version)\s+([A-Za-z0-9][A-Za-z0-9._\- ]*)",
            text,
        )
        if named:
            candidate = named.group(1).strip()
            # Stop at common filler words
            for stop in ("for", "of", "in", "to", "from", "with", "and", "notes", "note"):
                idx = candidate.lower().find(f" {stop} ")
                if idx >= 0:
                    candidate = candidate[:idx].strip()
                    break
            if candidate:
                return candidate

        return ""

    def is_conversational(self, text: str) -> bool:
        """
        Detect if a message is purely conversational (greetings, thanks, etc.)
        These should go straight to Claude without skill routing.
        """
        text_lower = text.lower().strip()

        conversational_patterns = [
            r"^(hi|hey|hello|yo|sup|howdy|good\s*(morning|afternoon|evening))[\s!?.]*$",
            r"^(thanks|thank you|thx|ty|cheers|appreciated)[\s!?.]*$",
            r"^(bye|goodbye|see you|later|cya)[\s!?.]*$",
            r"^(yes|no|yep|nope|yeah|nah|ok|okay|sure|got it)[\s!?.]*$",
            r"^(help|what can you do|who are you|what are you)[\s!?.]*$",
            r"^(lol|haha|heh|nice|cool|great|awesome)[\s!?.]*$",
        ]

        return any(re.match(p, text_lower) for p in conversational_patterns)
