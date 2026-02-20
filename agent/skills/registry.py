"""
Skill Registry — manages available skills and converts them to Claude tool definitions.
"""

import importlib
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from agent.interfaces.ai_provider import ToolDefinition
from agent.models.context import RequestContext


@dataclass
class SkillDefinition:
    """A loaded skill — metadata + worker function."""

    name: str
    description: str
    parameters: dict                    # JSON Schema for tool input
    requires_integration: Optional[str] # e.g., "jira", "github"
    triggers: list[str] = field(default_factory=list)
    worker_module: str = ""             # Python module path for the worker


class SkillRegistry:
    """
    Manages skills. Loads skill.yaml files, provides tool definitions
    for Claude, and resolves workers for execution.
    """

    def __init__(self):
        self._skills: dict[str, SkillDefinition] = {}

    def register(self, skill: SkillDefinition) -> None:
        """Register a skill."""
        self._skills[skill.name] = skill

    def load_from_directory(self, skills_dir: Path) -> None:
        """
        Scan a directory for skills. Each subdirectory with a skill.yaml
        is loaded as a skill.
        """
        if not skills_dir.exists():
            return

        for skill_path in skills_dir.iterdir():
            if not skill_path.is_dir():
                continue

            yaml_file = skill_path / "skill.yaml"
            if not yaml_file.exists():
                continue

            with open(yaml_file) as f:
                config = yaml.safe_load(f)

            skill = SkillDefinition(
                name=config["name"],
                description=config["description"],
                parameters=config.get("parameters", {}),
                requires_integration=config.get("requires_integration"),
                triggers=config.get("triggers", []),
                worker_module=config.get(
                    "worker_module",
                    f"agent.skills.{config['name']}.worker",
                ),
            )
            self.register(skill)

    def get_tools_for_tenant(self, ctx: RequestContext) -> list[ToolDefinition]:
        """
        Get Claude tool definitions for skills enabled by this tenant.
        Only returns skills whose required integrations are connected.
        """
        tools = []
        for skill_name in ctx.tenant.settings.enabled_skills:
            if skill_name not in self._skills:
                continue

            skill = self._skills[skill_name]
            tools.append(
                ToolDefinition(
                    name=skill.name,
                    description=skill.description,
                    input_schema=skill.parameters,
                )
            )
        return tools

    def get_skill(self, skill_name: str) -> Optional[SkillDefinition]:
        """Get a skill by name."""
        return self._skills.get(skill_name)

    def get_worker(self, skill_name: str) -> Callable:
        """
        Dynamically import and return the skill's worker execute() function.
        Used by the local adapter (DirectBus) to call skills without Lambda.
        """
        skill = self._skills.get(skill_name)
        if not skill:
            raise SkillNotFound(f"Skill '{skill_name}' not registered")

        module = importlib.import_module(skill.worker_module)
        if not hasattr(module, "execute"):
            raise SkillNotFound(
                f"Skill '{skill_name}' worker module has no execute() function"
            )
        return module.execute

    def list_skills(self) -> list[SkillDefinition]:
        """List all registered skills."""
        return list(self._skills.values())

    def list_skill_names(self) -> list[str]:
        return list(self._skills.keys())


class SkillNotFound(Exception):
    pass
