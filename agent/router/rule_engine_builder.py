"""
Rule Engine Builder — uses AI to generate optimized regex rule sets for a tenant.

The AI receives all enabled/disabled skill metadata and produces a TenantRuleSet
with non-overlapping regex patterns tailored to this specific skill combination.

Key advantage over hand-maintained regex: the AI understands the full combination
of enabled skills and generates patterns that minimize cross-skill confusion.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from agent.interfaces.ai_provider import AIProvider, ToolDefinition
from agent.router.models import SkillRules, TenantRuleSet, TrainingExample
from agent.skills.registry import SkillDefinition

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a regex pattern engineer. Your job is to generate Python-compatible regex patterns
that route user chat messages to the correct AI skill handler.

Rules:
- All patterns are used with re.IGNORECASE — no need to duplicate case variants
- Use word-boundary anchors \\b to avoid partial matches (e.g. \\bsprint\\b)
- detection_patterns should be broad enough to catch natural phrasings of the intent
- action_rules should be specific enough to pick the right action (first match wins)
- List action_rules from most specific to most general
- Avoid cross-skill collisions — if two skills share trigger words, use context
- disabled_skill_catchers should catch the most obvious user requests for that skill
"""


class RuleEngineBuilder:
    """
    Generates an AI-optimized TenantRuleSet from skill metadata.

    Call build_rules() when a tenant's skill configuration changes.
    The resulting TenantRuleSet is saved to the RuleStore and compiled
    into a CompiledRuleEngine for $0, <1ms routing.
    """

    async def build_rules(
        self,
        tenant_id: str,
        enabled_skills: list[SkillDefinition],
        disabled_skills: list[SkillDefinition],
        ai: AIProvider,
        model: str,
        training_data: Optional[list[TrainingExample]] = None,
    ) -> TenantRuleSet:
        """
        Generate a TenantRuleSet for this combination of enabled/disabled skills.

        Returns an empty rule set if no skills are enabled.
        Raises ValueError if the AI fails to return a structured rule set.
        """
        if not enabled_skills:
            logger.info(f"No enabled skills for tenant {tenant_id} — empty rule set")
            return TenantRuleSet(
                tenant_id=tenant_id,
                version=1,
                generated_at=datetime.now(timezone.utc).isoformat(),
                skill_rules={},
                disabled_skill_catchers={},
                generation_model=model,
            )

        prompt = self._build_prompt(enabled_skills, disabled_skills, training_data)
        tool = self._build_tool_definition(enabled_skills, disabled_skills)

        logger.info(
            f"Building rules for tenant {tenant_id}: "
            f"enabled={[s.name for s in enabled_skills]}, "
            f"disabled={[s.name for s in disabled_skills]}"
        )

        response = await ai.chat(
            model=model,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            tools=[tool],
            max_tokens=4096,
        )

        if not response.has_tool_use:
            raise ValueError(
                f"RuleEngineBuilder: AI did not call submit_rule_set "
                f"(text response: {response.text[:200] if response.text else 'none'})"
            )

        tc = response.tool_calls[0]
        rule_set = self._parse_response(tenant_id, tc.tool_params, model)
        logger.info(
            f"Rules generated for {tenant_id}: "
            f"{len(rule_set.skill_rules)} skills, "
            f"{sum(len(r.detection_patterns) for r in rule_set.skill_rules.values())} "
            f"detection patterns"
        )
        return rule_set

    # --- Private helpers ---

    def _build_prompt(
        self,
        enabled: list[SkillDefinition],
        disabled: list[SkillDefinition],
        training_data: Optional[list[TrainingExample]],
    ) -> str:
        lines = ["Generate routing rules for this tenant's skill configuration.\n"]

        lines.append("## ENABLED SKILLS (generate detection + action rules for each)\n")
        for skill in enabled:
            lines.append(f"### {skill.name}")
            lines.append(f"Description: {skill.description.strip()}")
            lines.append(f"Example trigger phrases: {', '.join(repr(t) for t in skill.triggers)}")
            if skill.action_descriptions:
                lines.append("Actions:")
                for action, desc in skill.action_descriptions.items():
                    lines.append(f"  - {action}: {desc}")
            lines.append("")

        if disabled:
            lines.append(
                "## DISABLED SKILLS (generate catcher patterns only — "
                "do NOT route these, just catch them to inform the user)\n"
            )
            for skill in disabled:
                lines.append(f"### {skill.name}")
                lines.append(f"Description: {skill.description.strip()}")
                if skill.triggers:
                    lines.append(
                        f"Example triggers: {', '.join(repr(t) for t in skill.triggers[:5])}"
                    )
                lines.append("")

        if training_data:
            lines.append(
                "## TRAINING DATA (Tier 2 messages that weren't caught by rules — "
                "incorporate these into your patterns)\n"
            )
            for ex in training_data[:30]:
                if ex.matched_skill:
                    lines.append(
                        f'- "{ex.message_text}" → {ex.matched_skill}'
                        + (f".{ex.matched_action}" if ex.matched_action else "")
                    )
                else:
                    lines.append(f'- "{ex.message_text}" → (no skill / freeform)')
            lines.append("")

        lines.append("Call submit_rule_set with the complete optimized rule set.")
        return "\n".join(lines)

    def _build_tool_definition(
        self,
        enabled: list[SkillDefinition],
        disabled: list[SkillDefinition],
    ) -> ToolDefinition:
        """Build the structured tool schema for rule set output."""
        skill_rules_props: dict[str, object] = {}
        skill_names: list[str] = []
        for skill in enabled:
            skill_names.append(skill.name)
            actions: list[str] = []
            action_prop = skill.parameters.get("properties", {}).get("action", {})
            actions = action_prop.get("enum", [])
            skill_rules_props[skill.name] = {
                "type": "object",
                "description": f"Routing rules for the '{skill.name}' skill",
                "properties": {
                    "detection_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Regex patterns (\\b word boundaries) that detect the user "
                            "wants this skill. Broad — catches natural phrasings."
                        ),
                    },
                    "action_rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "pattern": {
                                    "type": "string",
                                    "description": "Regex pattern to match",
                                },
                                "action": {
                                    "type": "string",
                                    "enum": actions if actions else ["status"],
                                    "description": "Action to invoke when pattern matches",
                                },
                            },
                            "required": ["pattern", "action"],
                        },
                        "description": (
                            "Rules to pick the action within this skill. "
                            "Most specific patterns first — first match wins."
                        ),
                    },
                    "disambiguation_notes": {
                        "type": "string",
                        "description": (
                            "Brief explanation of how these patterns avoid "
                            "conflicts with other skills."
                        ),
                    },
                },
                "required": ["detection_patterns", "action_rules"],
            }

        disabled_catchers_props: dict[str, object] = {}
        disabled_names: list[str] = []
        for skill in disabled:
            disabled_names.append(skill.name)
            disabled_catchers_props[skill.name] = {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    f"Patterns that catch obvious '{skill.name}' requests "
                    f"(so we can tell the user it's not enabled)."
                ),
            }

        schema: dict[str, object] = {
            "type": "object",
            "properties": {
                "skill_rules": {
                    "type": "object",
                    "description": "Per-skill routing rules for all enabled skills",
                    "properties": skill_rules_props,
                    "required": skill_names,
                },
                "disabled_skill_catchers": {
                    "type": "object",
                    "description": "Catcher patterns for disabled skills",
                    "properties": disabled_catchers_props,
                    "required": disabled_names,
                },
            },
            "required": ["skill_rules", "disabled_skill_catchers"],
        }

        return ToolDefinition(
            name="submit_rule_set",
            description="Submit the complete optimized routing rule set for this tenant.",
            input_schema=schema,
        )

    def _parse_response(self, tenant_id: str, params: dict[str, Any], model: str) -> TenantRuleSet:
        """Parse the AI tool-call output into a TenantRuleSet."""
        skill_rules: dict[str, SkillRules] = {}
        for skill_name, rules in params.get("skill_rules", {}).items():
            action_rules: list[tuple[str, str]] = [
                (r["pattern"], r["action"]) for r in rules.get("action_rules", [])
            ]
            skill_rules[skill_name] = SkillRules(
                skill_name=skill_name,
                detection_patterns=rules.get("detection_patterns", []),
                action_rules=action_rules,
                disambiguation_notes=rules.get("disambiguation_notes", ""),
            )

        disabled_catchers: dict[str, list[str]] = {}
        for skill_name, patterns in params.get("disabled_skill_catchers", {}).items():
            disabled_catchers[skill_name] = [str(p) for p in patterns]

        return TenantRuleSet(
            tenant_id=tenant_id,
            version=1,  # caller increments based on existing version
            generated_at=datetime.now(timezone.utc).isoformat(),
            skill_rules=skill_rules,
            disabled_skill_catchers=disabled_catchers,
            generation_model=model,
        )
