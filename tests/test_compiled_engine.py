"""
Unit tests for the CompiledRuleEngine and related functions.
"""

import pytest

from agent.router.compiled_engine import (
    CompiledRuleEngine,
    is_conversational,
    strip_raw_flag,
)
from agent.router.models import SkillRules, TenantRuleSet
from agent.skills.registry import SkillDefinition, SkillRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def skill_registry() -> SkillRegistry:
    """Minimal skill registry with sprint_status and release_notes."""
    registry = SkillRegistry()
    registry.register(
        SkillDefinition(
            name="sprint_status",
            description="Get current sprint status from Jira.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["status", "blockers", "mine"]}
                },
                "required": ["action"],
            },
            requires_integration="jira",
            supports_raw=True,
            triggers=["sprint status", "are we on track"],
            action_descriptions={
                "status": "Full sprint overview",
                "blockers": "Only blocked items",
                "mine": "My tickets",
            },
        )
    )
    registry.register(
        SkillDefinition(
            name="release_notes",
            description="Summarize Jira releases.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list_releases", "summarize"]},
                    "release_name": {"type": "string"},
                },
                "required": ["action"],
            },
            requires_integration="jira",
            supports_raw=True,
            triggers=["release notes", "changelog"],
            action_descriptions={
                "list_releases": "List all fix versions",
                "summarize": "Summarize a specific release",
            },
        )
    )
    registry.register(
        SkillDefinition(
            name="ping",
            description="Quick health check and model test.",
            parameters={"type": "object", "properties": {}, "required": []},
            requires_integration=None,
            supports_raw=True,
            triggers=["ping", "are you alive"],
        )
    )
    return registry


@pytest.fixture
def rule_set(skill_registry: SkillRegistry) -> TenantRuleSet:
    """Synthetic TenantRuleSet for testing (no AI call needed)."""
    return TenantRuleSet(
        tenant_id="test",
        version=1,
        generated_at="2026-03-04T00:00:00Z",
        generation_model="test",
        skill_rules={
            "sprint_status": SkillRules(
                skill_name="sprint_status",
                detection_patterns=[
                    r"\bsprint\b",
                    r"\bblock(ed|er|ing)?\b",
                    r"\bon\s*track\b",
                    r"\bticket\b",
                    r"\bjira\b",
                ],
                action_rules=[
                    (r"\bblock(ed|er|ing)?\b", "blockers"),
                    (r"\bmy\b.*\b(ticket|task|issue)\b", "mine"),
                    (r"\bassigned to me\b", "mine"),
                    (r"\bsprint\b", "status"),
                ],
                disambiguation_notes="Sprint patterns avoid colliding with release_notes.",
            ),
            "release_notes": SkillRules(
                skill_name="release_notes",
                detection_patterns=[
                    r"\brelease\s*notes?\b",
                    r"\bchangelog\b",
                    r"\bwhat.*ship(ped)?\b",
                ],
                action_rules=[
                    (r"\blist\b.*\brelease\b", "list_releases"),
                    (r"\brelease\s*notes?\b", "summarize"),
                    (r"\bchangelog\b", "summarize"),
                ],
                disambiguation_notes="Release patterns avoid sprint status collision.",
            ),
            "ping": SkillRules(
                skill_name="ping",
                detection_patterns=[r"\bping\b", r"\bare you alive\b"],
                action_rules=[],
            ),
        },
        disabled_skill_catchers={
            "meeting_prep": [r"\bmeeting\b", r"\bagenda\b"],
        },
    )


@pytest.fixture
def engine(rule_set: TenantRuleSet, skill_registry: SkillRegistry) -> CompiledRuleEngine:
    return CompiledRuleEngine(rule_set, skill_registry)


# ---------------------------------------------------------------------------
# strip_raw_flag
# ---------------------------------------------------------------------------


def test_strip_raw_flag_present():
    text, is_raw = strip_raw_flag("sprint status --raw")
    assert is_raw
    assert "--raw" not in text
    assert "sprint status" in text


def test_strip_raw_flag_absent():
    text, is_raw = strip_raw_flag("sprint status")
    assert not is_raw
    assert text == "sprint status"


def test_strip_raw_flag_case_insensitive():
    _, is_raw = strip_raw_flag("ping --RAW")
    assert is_raw


# ---------------------------------------------------------------------------
# is_conversational
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "hi",
        "hey",
        "hello",
        "thanks",
        "thank you",
        "bye",
        "yes",
        "no",
        "ok",
        "lol",
    ],
)
def test_is_conversational_true(text: str):
    assert is_conversational(text)


@pytest.mark.parametrize(
    "text",
    [
        "sprint status",
        "show me the release notes",
        "what's blocked?",
        "ping",
        "are we on track for the sprint?",
    ],
)
def test_is_conversational_false(text: str):
    assert not is_conversational(text)


# ---------------------------------------------------------------------------
# CompiledRuleEngine.match
# ---------------------------------------------------------------------------


def test_match_sprint_status(engine: CompiledRuleEngine):
    match = engine.match("what's the sprint status?", ["sprint_status", "release_notes", "ping"])
    assert match is not None
    assert match.skill_name == "sprint_status"
    assert match.action == "status"


def test_match_sprint_blockers(engine: CompiledRuleEngine):
    match = engine.match("show me what's blocked", ["sprint_status", "release_notes"])
    assert match is not None
    assert match.skill_name == "sprint_status"
    assert match.action == "blockers"


def test_match_release_notes(engine: CompiledRuleEngine):
    match = engine.match("show me the release notes for v2.0", ["sprint_status", "release_notes"])
    assert match is not None
    assert match.skill_name == "release_notes"
    assert match.action == "summarize"


def test_match_release_name_extracted(engine: CompiledRuleEngine):
    match = engine.match("release notes for v2.5.0", ["sprint_status", "release_notes"])
    assert match is not None
    assert match.params.get("release_name") == "v2.5.0"


def test_match_ping(engine: CompiledRuleEngine):
    match = engine.match("ping", ["sprint_status", "release_notes", "ping"])
    assert match is not None
    assert match.skill_name == "ping"


def test_match_no_match(engine: CompiledRuleEngine):
    match = engine.match("what's the weather in London?", ["sprint_status", "release_notes"])
    assert match is None


def test_match_disabled_skill_not_returned(engine: CompiledRuleEngine):
    # sprint_status is not in enabled_skills — should not match
    match = engine.match("sprint status", ["release_notes"])
    assert match is None or match.skill_name != "sprint_status"


def test_match_respects_enabled_skills(engine: CompiledRuleEngine):
    match = engine.match("are we on track for the sprint?", ["ping"])
    assert match is None or match.skill_name == "ping"


# ---------------------------------------------------------------------------
# CompiledRuleEngine.check_disabled_skill
# ---------------------------------------------------------------------------


def test_check_disabled_skill_matches(engine: CompiledRuleEngine):
    result = engine.check_disabled_skill("can you prepare my meeting agenda?")
    assert result == "meeting_prep"


def test_check_disabled_skill_no_match(engine: CompiledRuleEngine):
    result = engine.check_disabled_skill("show me the sprint")
    assert result is None


# ---------------------------------------------------------------------------
# CompiledRuleEngine.supports_raw
# ---------------------------------------------------------------------------


def test_supports_raw_true(engine: CompiledRuleEngine):
    assert engine.supports_raw("sprint_status")


def test_supports_raw_unknown_skill(engine: CompiledRuleEngine):
    assert not engine.supports_raw("nonexistent_skill")


# ---------------------------------------------------------------------------
# Empty engine (no rules built yet)
# ---------------------------------------------------------------------------


def test_empty_engine_no_match(skill_registry: SkillRegistry):
    empty_rule_set = TenantRuleSet(
        tenant_id="empty",
        version=0,
        generated_at="",
        skill_rules={},
        disabled_skill_catchers={},
    )
    engine = CompiledRuleEngine(empty_rule_set, skill_registry)
    assert engine.match("sprint status", ["sprint_status"]) is None
    assert engine.check_disabled_skill("meeting prep") is None
