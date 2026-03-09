"""
Practice models — team experience bundles.

A Practice bundles skills + pages into an installable package.
Examples: Engineering (sprint tools), CBT (therapy session recording).
"""

from dataclasses import dataclass, field


@dataclass
class PracticePage:
    """A console page bundled in a practice."""

    slug: str  # URL segment, e.g. "sprint"
    title: str  # "Sprint Board"
    nav_label: str  # Short label for nav sidebar
    nav_order: int  # Sort position in nav (0 = hidden from nav)
    file: str  # Relative path within practice, e.g. "pages/sprint.html"
    page_type: str = "dashboard"  # "dashboard" (read-only) | "interactive"
    description: str = ""
    requires_skills: list[str] = field(default_factory=list)


@dataclass
class PracticeDefinition:
    """A loaded practice — metadata + references to skills and pages."""

    name: str  # "engineering"
    display_name: str  # "Engineering"
    description: str
    version: str = "1.0.0"
    icon: str = ""
    integrations: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    pages: list[PracticePage] = field(default_factory=list)
    system_prompt_addon: str = ""
    built_in: bool = False  # True for practices in the codebase
    base_path: str = ""  # Filesystem path for resolving pages/skills
