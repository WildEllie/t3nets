"""
Skill Registry — manages available skills and converts them to Claude tool definitions.
"""

import asyncio
import importlib
import importlib.util
import inspect
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import yaml  # type: ignore[import-untyped]
from t3nets_sdk.contracts import SkillContext, SkillResult

from agent.interfaces.ai_provider import ToolDefinition
from agent.models.context import RequestContext

NormalizedWorker = Callable[[SkillContext, dict[str, Any]], Awaitable[SkillResult]]


@dataclass
class SkillDefinition:
    """A loaded skill — metadata + worker function."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for tool input
    requires_integration: Optional[str]  # e.g., "jira", "github"
    supports_raw: bool = False  # Whether --raw debug output is supported
    triggers: list[str] = field(default_factory=list)
    action_descriptions: dict[str, str] = field(default_factory=dict)  # action → description
    worker_module: str = ""  # Python module path for the worker
    worker_path: str = ""  # Filesystem path to worker.py (for uploaded practices)


class SkillRegistry:
    """
    Manages skills. Loads skill.yaml files, provides tool definitions
    for Claude, and resolves workers for execution.
    """

    def __init__(self) -> None:
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
                supports_raw=config.get("supports_raw", False),
                triggers=config.get("triggers", []),
                action_descriptions=config.get("action_descriptions", {}),
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

    def get_worker(self, skill_name: str) -> NormalizedWorker:
        """
        Dynamically import and return a normalized worker callable for a skill.

        The returned callable always presents the new contract:
            async def (ctx: SkillContext, params: dict) -> SkillResult

        Legacy workers written as `execute(params, secrets)` or
        `execute(params, secrets, ctx_dict)` — synchronous or async — are
        wrapped transparently so call sites don't need to branch on
        signature anymore.

        Supports two module-loading modes:
        - worker_path: load from filesystem (uploaded practices)
        - worker_module: load from Python module path (built-in skills)
        """
        skill = self._skills.get(skill_name)
        if not skill:
            raise SkillNotFound(f"Skill '{skill_name}' not registered")

        if skill.worker_path:
            spec = importlib.util.spec_from_file_location(
                f"practice_worker_{skill_name}", skill.worker_path
            )
            if spec is None or spec.loader is None:
                raise SkillNotFound(
                    f"Cannot load worker for '{skill_name}' from {skill.worker_path}"
                )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        else:
            module = importlib.import_module(skill.worker_module)

        if not hasattr(module, "execute"):
            raise SkillNotFound(f"Skill '{skill_name}' worker module has no execute() function")
        return _normalize_worker(module.execute, skill_name)

    def list_skills(self) -> list[SkillDefinition]:
        """List all registered skills."""
        return list(self._skills.values())

    def list_skill_names(self) -> list[str]:
        return list(self._skills.keys())


class SkillNotFound(Exception):
    pass


def _is_new_contract(fn: Callable[..., Any]) -> bool:
    """True if `fn` takes the new `(ctx, params)` contract.

    Uses parameter annotations when available (most reliable), falling back
    to the parameter name `ctx` (convention in the SDK docs and scaffold).
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    params = list(sig.parameters.values())
    if not params:
        return False
    first = params[0]
    if first.annotation is SkillContext:
        return True
    if getattr(first.annotation, "__name__", None) == "SkillContext":
        return True
    return first.name == "ctx"


def _coerce_result(value: Any) -> SkillResult:
    """Normalize whatever a worker returned into a `SkillResult`.

    New-contract workers return a `SkillResult` directly. Legacy workers
    return a plain dict — we treat `{"error": ...}` dicts as failures and
    everything else as a success payload, matching the historical
    convention the router already relies on.
    """
    if isinstance(value, SkillResult):
        return value
    if isinstance(value, dict):
        if "error" in value and len(value) == 1:
            return SkillResult.fail(str(value["error"]))
        if "error" in value:
            rest = {k: v for k, v in value.items() if k != "error"}
            return SkillResult.fail(str(value["error"]), **rest)
        return SkillResult.ok(value)
    return SkillResult.ok({"value": value})


def _normalize_worker(fn: Callable[..., Any], skill_name: str) -> NormalizedWorker:
    """Wrap any supported worker shape in the normalized new-contract callable.

    Supported shapes:
    - async / sync `execute(ctx: SkillContext, params) -> SkillResult | dict`
    - async / sync `execute(params, secrets) -> dict`
    - async / sync `execute(params, secrets, ctx_dict) -> dict`
      where `ctx_dict` is the legacy `{blob_store, tenant_id, ...}` bag.
    """
    if _is_new_contract(fn):

        async def _call_new(ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            result = fn(ctx, params)
            if asyncio.iscoroutine(result):
                result = await result
            return _coerce_result(result)

        return _call_new

    try:
        sig = inspect.signature(fn)
        arity = len(sig.parameters)
    except (TypeError, ValueError):
        arity = 2

    log = logging.getLogger(__name__)

    async def _call_legacy(ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
        # Legacy workers expect the raw secret bundle as a flat dict, plus
        # optionally a ctx bag with blob_store / tenant_id. We reconstruct
        # that bag from the SkillContext so old practices keep working
        # while the new-contract path is rolled out.
        legacy_ctx_dict: dict[str, Any] = {
            "blob_store": ctx.blob_store,
            "tenant_id": ctx.tenant_id,
            **ctx.extras,
        }
        try:
            if arity >= 3:
                result = fn(params, ctx.secrets, legacy_ctx_dict)
            else:
                result = fn(params, ctx.secrets)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as e:  # noqa: BLE001 — mirror prior behavior
            log.error(f"Skill '{skill_name}' raised: {e}")
            return SkillResult.fail(str(e))
        return _coerce_result(result)

    return _call_legacy
