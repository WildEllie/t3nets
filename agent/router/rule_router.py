"""
Rule-Based Router — fast, free routing for obvious requests.

Two-tier routing:
  Tier 1: Pattern matching on skill triggers (free, <1ms)
  Tier 2: Claude AI routing (paid, ~1-3 seconds)

If Tier 1 matches with high confidence, skip Claude for routing entirely.
Claude is still used to FORMAT the response after the skill returns data.
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

from agent.skills.registry import SkillRegistry, SkillDefinition

logger = logging.getLogger(__name__)


@dataclass
class RouteMatch:
    """Result of rule-based routing."""
    skill_name: str
    action: str
    params: dict = field(default_factory=dict)
    confidence: float = 0.0  # 0.0 to 1.0
    raw_mode: bool = False   # --raw flag detected


@dataclass
class RuleSet:
    """Rules for matching a single skill."""
    skill_name: str
    supports_raw: bool = False
    # Exact trigger phrases (case-insensitive)
    triggers: list[str] = field(default_factory=list)
    # Regex patterns for skill matching
    patterns: list[re.Pattern] = field(default_factory=list)
    # Action routing rules: list of (regex_pattern, action_name)
    action_rules: list[tuple[re.Pattern, str]] = field(default_factory=list)


# --- Action routing rules per skill ---
# These are REGEX patterns, not exact substrings.
# First match wins, so put specific patterns before general ones.

SKILL_ACTION_RULES: dict[str, list[tuple[str, str]]] = {
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
        (r"\brelease\b", "status"),
        (r"\bburn\s*down\b", "status"),
        (r"\bvelocity\b", "status"),
        (r"\bscope\b", "status"),
        (r"\bleft\b.*\bsprint\b", "status"),
        (r"\bsprint\b.*\bleft\b", "status"),
    ],
    "meeting_prep": [
        (r"\b(next|upcoming)\s+meeting\b", "prepare"),
        (r"\bmeeting\b.*\b(prep|prepare|brief|ready)\b", "prepare"),
        (r"\bbrief\s*(me|ing)\b", "prepare"),
        (r"\bagenda\b", "agenda"),
        (r"\btopics?\b.*\bmeeting\b", "agenda"),
    ],
    "email_triage": [
        (r"\b(urgent|important|priority)\b.*\b(email|message|mail)\b", "priority"),
        (r"\b(email|message|mail)\b.*\b(urgent|important|priority)\b", "priority"),
        (r"\binbox\b", "summary"),
        (r"\b(unread|new)\b.*\b(email|message|mail)\b", "summary"),
        (r"\bemail\b", "summary"),
        (r"\bmail\b", "summary"),
    ],
}

# --- Skill detection patterns (broader than action rules) ---
# These determine IF a skill should handle the message.
# Action rules above determine WHICH action within the skill.

SKILL_PATTERNS: dict[str, list[str]] = {
    "sprint_status": [
        r"\bsprint\b",
        r"\bblock(ed|er|ing|s)?\b",
        r"\bon\s*track\b",
        r"\brelease\s*(status|ready)?\b",
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
    "meeting_prep": [
        r"\bmeeting\b",
        r"\bagenda\b",
        r"\bbrief(ing)?\b",
        r"\b(next|upcoming)\s+(call|sync|standup|meeting)\b",
    ],
    "email_triage": [
        r"\b(inbox|email|mail)\b",
        r"\bunread\b",
        r"\b(urgent|important)\s+(email|message)\b",
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
        self._compiled_patterns: dict[str, list[re.Pattern]] = {}
        self._build_rule_sets()

    def _build_rule_sets(self):
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
            rule_set.patterns = [
                re.compile(p, re.IGNORECASE) for p in pattern_strings
            ]

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
                params = self._extract_params(text_lower, rule_set, action)
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

        logger.info(
            f"RuleBasedRouter: no confident match "
            f"(best: {best_match.skill_name + ' @ ' + f'{best_confidence:.2f}' if best_match else 'none'}), "
            f"falling back to Claude"
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
        pattern_hits = sum(
            1 for p in rule_set.patterns if p.search(text_lower)
        )
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

    def _extract_params(self, text_lower: str, rule_set: RuleSet, action: str) -> dict:
        """Extract parameters from the message text."""
        params = {"action": action}

        # Extract email addresses (for "mine" action)
        if action == "mine":
            email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', text_lower)
            if email_match:
                params["assignee_email"] = email_match.group()

        return params

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
