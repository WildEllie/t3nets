"""
Manifest validators for `practice.yaml` and `skill.yaml`.

Pydantic models that mirror the YAML schemas a practice author writes. They
exist so the platform — and `t3nets validate` in step 4 — can give clear,
field-level errors instead of crashing deep in the loader.

Pydantic is intentionally only used here, on the install/validate path. The
request hot path (RequestContext, Tenant, etc.) stays as plain dataclasses to
keep cold-start fast and dependency surface small.
"""

from __future__ import annotations

from typing import Any, Optional

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Practice and skill names: lowercase letters, digits, dashes, underscores.
# Mirrors the original `name.replace("-", "_").replace("_", "").isalnum()`
# check but enforces the spelling explicitly.
_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"


class _StrictModel(BaseModel):
    """Base model that rejects unknown keys so typos surface as errors."""

    model_config = ConfigDict(extra="forbid")


class SkillManifest(_StrictModel):
    """Validated `skill.yaml` contents."""

    name: str = Field(pattern=_NAME_PATTERN)
    description: str
    triggers: list[str] = Field(default_factory=list)
    requires_integration: Optional[str] = None
    supports_raw: bool = False
    action_descriptions: dict[str, str] = Field(default_factory=dict)
    # JSON Schema for parameters — we don't validate the schema's *shape*
    # here, only that it's a dict. Step 4's `t3nets validate` can run a
    # full JSON Schema check.
    parameters: dict[str, Any] = Field(default_factory=dict)


class PracticePageManifest(_StrictModel):
    """A single entry under `pages:` in `practice.yaml`."""

    slug: str
    title: str
    file: str
    nav_label: Optional[str] = None
    nav_order: int = 0
    type: str = "dashboard"
    description: str = ""
    requires_skills: list[str] = Field(default_factory=list)


class PracticeManifest(_StrictModel):
    """Validated `practice.yaml` contents."""

    name: str = Field(pattern=_NAME_PATTERN)
    display_name: Optional[str] = None
    description: str = ""
    version: str = "1.0.0"
    icon: str = ""
    integrations: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    pages: list[PracticePageManifest] = Field(default_factory=list)
    assets: list[str] = Field(default_factory=list)
    hooks: dict[str, str] = Field(default_factory=dict)
    system_prompt_addon: str = ""


class ManifestError(ValueError):
    """Raised when a manifest fails validation. Subclass of ValueError so
    callers that already `except ValueError` keep working."""


def _format_error(filename: str, exc: ValidationError) -> str:
    lines = [f"Invalid {filename}:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def parse_practice_yaml(text: str) -> PracticeManifest:
    """Parse and validate a `practice.yaml` document.

    Args:
        text: Raw YAML source.

    Returns:
        A `PracticeManifest`.

    Raises:
        ManifestError: If the YAML is malformed or fails schema validation.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ManifestError(f"Invalid practice.yaml: {e}") from e
    if not isinstance(data, dict):
        raise ManifestError("Invalid practice.yaml: top-level must be a mapping")
    try:
        return PracticeManifest.model_validate(data)
    except ValidationError as e:
        raise ManifestError(_format_error("practice.yaml", e)) from e


def parse_skill_yaml(text: str) -> SkillManifest:
    """Parse and validate a `skill.yaml` document.

    Args:
        text: Raw YAML source.

    Returns:
        A `SkillManifest`.

    Raises:
        ManifestError: If the YAML is malformed or fails schema validation.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ManifestError(f"Invalid skill.yaml: {e}") from e
    if not isinstance(data, dict):
        raise ManifestError("Invalid skill.yaml: top-level must be a mapping")
    try:
        return SkillManifest.model_validate(data)
    except ValidationError as e:
        raise ManifestError(_format_error("skill.yaml", e)) from e


__all__ = [
    "ManifestError",
    "PracticeManifest",
    "PracticePageManifest",
    "SkillManifest",
    "parse_practice_yaml",
    "parse_skill_yaml",
]
