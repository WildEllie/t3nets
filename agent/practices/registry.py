"""
Practice Registry — manages installed practices.

Loads practice manifests (practice.yaml) from built-in and uploaded
directories, registers their skills with the SkillRegistry, and
provides page resolution for the dev server.
"""

import logging
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from agent.models.practice import PracticeDefinition, PracticePage
from agent.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


class PracticeRegistry:
    """Manages installed practices — built-in and uploaded."""

    def __init__(self) -> None:
        self._practices: dict[str, PracticeDefinition] = {}

    def load_builtin(self, practices_dir: Path) -> None:
        """
        Load built-in practices from the codebase.
        Each subdirectory with a practice.yaml is loaded.
        """
        if not practices_dir.exists():
            return

        for practice_path in sorted(practices_dir.iterdir()):
            if not practice_path.is_dir():
                continue
            manifest = practice_path / "practice.yaml"
            if not manifest.exists():
                continue

            practice = self._load_manifest(manifest, built_in=True)
            if practice:
                self._practices[practice.name] = practice
                logger.info(f"Loaded built-in practice: {practice.name}")

    def load_uploaded(self, data_dir: Path) -> None:
        """
        Load uploaded practices from the data directory.
        Each subdirectory with a practice.yaml is loaded.
        """
        practices_dir = data_dir / "practices"
        if not practices_dir.exists():
            return

        for practice_path in sorted(practices_dir.iterdir()):
            if not practice_path.is_dir():
                continue
            manifest = practice_path / "practice.yaml"
            if not manifest.exists():
                continue

            practice = self._load_manifest(manifest, built_in=False)
            if practice:
                self._practices[practice.name] = practice
                logger.info(f"Loaded uploaded practice: {practice.name}")

    def install_zip(self, zip_bytes: bytes, data_dir: Path) -> PracticeDefinition:
        """
        Validate and extract a practice ZIP to the data directory.
        Returns the installed PracticeDefinition.

        Raises ValueError if validation fails.
        """
        buf = BytesIO(zip_bytes)
        try:
            zf = zipfile.ZipFile(buf)
        except zipfile.BadZipFile as e:
            raise ValueError(f"Invalid ZIP file: {e}") from e

        # Find practice.yaml in the ZIP
        manifest_path = self._find_manifest_in_zip(zf)
        if not manifest_path:
            raise ValueError("ZIP must contain practice.yaml at root or in a single subdirectory")

        # Determine prefix (files might be in a subdirectory)
        prefix = ""
        parts = manifest_path.split("/")
        if len(parts) > 1:
            prefix = "/".join(parts[:-1]) + "/"

        # Parse manifest
        manifest_data = yaml.safe_load(zf.read(manifest_path))
        name = manifest_data.get("name", "")
        if not name or not name.isidentifier():
            raise ValueError(f"Practice name must be a valid identifier, got: '{name}'")

        # Validate skills
        for skill_name in manifest_data.get("skills", []):
            skill_yaml = f"{prefix}skills/{skill_name}/skill.yaml"
            skill_worker = f"{prefix}skills/{skill_name}/worker.py"
            if skill_yaml not in zf.namelist():
                raise ValueError(f"Missing skill.yaml for skill '{skill_name}'")
            if skill_worker not in zf.namelist():
                raise ValueError(f"Missing worker.py for skill '{skill_name}'")

        # Validate pages
        for page in manifest_data.get("pages", []):
            page_file = f"{prefix}{page['file']}"
            if page_file not in zf.namelist():
                raise ValueError(f"Missing page file: {page['file']}")

        # Security: reject path traversal
        for member in zf.namelist():
            if ".." in member or member.startswith("/"):
                raise ValueError(f"Invalid path in ZIP: {member}")

        # Extract to data/practices/{name}/
        dest = data_dir / "practices" / name
        dest.mkdir(parents=True, exist_ok=True)

        for member in zf.namelist():
            if member.endswith("/"):
                continue  # skip directories
            # Strip prefix if present
            relative = member[len(prefix) :] if prefix and member.startswith(prefix) else member
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(member))

        zf.close()

        # Load the installed practice
        practice = self._load_manifest(dest / "practice.yaml", built_in=False)
        if not practice:
            raise ValueError("Failed to load installed practice manifest")

        self._practices[practice.name] = practice
        logger.info(f"Installed practice: {practice.name} v{practice.version}")
        return practice

    def get(self, name: str) -> PracticeDefinition | None:
        """Get a practice by name."""
        return self._practices.get(name)

    def list_all(self) -> list[PracticeDefinition]:
        """List all installed practices."""
        return list(self._practices.values())

    def get_pages_for_tenant(self, tenant: Any) -> list[dict[str, Any]]:
        """
        Get pages available to a tenant based on their practice configuration.
        Returns list of dicts with {slug, title, nav_label, url, practice}.
        """
        pages: list[dict[str, Any]] = []
        settings = tenant.settings

        # Pages from primary practice
        if settings.primary_practice:
            practice = self._practices.get(settings.primary_practice)
            if practice:
                for page in practice.pages:
                    if page.nav_order > 0 and page.nav_label:
                        pages.append(
                            {
                                "slug": page.slug,
                                "title": page.title,
                                "nav_label": page.nav_label,
                                "nav_order": page.nav_order,
                                "url": f"/p/{practice.name}/{page.slug}",
                                "practice": practice.name,
                            }
                        )

        # Pages from addons
        for addon in getattr(settings, "addon_pages", []):
            parts = addon.split("/", 1)
            if len(parts) != 2:
                continue
            practice_name, page_slug = parts
            practice = self._practices.get(practice_name)
            if not practice:
                continue
            for page in practice.pages:
                if page.slug == page_slug and page.nav_label:
                    pages.append(
                        {
                            "slug": page.slug,
                            "title": page.title,
                            "nav_label": page.nav_label,
                            "nav_order": page.nav_order,
                            "url": f"/p/{practice_name}/{page.slug}",
                            "practice": practice_name,
                        }
                    )

        pages.sort(key=lambda p: p["nav_order"])
        return pages

    def get_page_path(self, practice_name: str, page_slug: str) -> Path | None:
        """
        Resolve a practice page to its filesystem path.
        Returns None if not found.
        """
        practice = self._practices.get(practice_name)
        if not practice:
            return None

        for page in practice.pages:
            if page.slug == page_slug:
                return Path(practice.base_path) / page.file

        return None

    def register_skills(self, skill_registry: SkillRegistry) -> None:
        """
        Register all practice skills with the SkillRegistry.
        For each practice, loads skills from its skills/ subdirectory.

        All practice skills use worker_path (filesystem-based loading)
        since they don't follow the agent.skills.{name}.worker module pattern.
        """
        for practice in self._practices.values():
            skills_dir = Path(practice.base_path) / "skills"
            if not skills_dir.exists():
                continue
            self._load_practice_skills(skills_dir, practice, skill_registry)

    # --- Private helpers ---

    def _load_manifest(self, manifest_path: Path, built_in: bool) -> PracticeDefinition | None:
        """Parse a practice.yaml into a PracticeDefinition."""
        try:
            with open(manifest_path) as f:
                data = yaml.safe_load(f)

            pages = []
            for p in data.get("pages", []):
                pages.append(
                    PracticePage(
                        slug=p["slug"],
                        title=p["title"],
                        nav_label=p.get("nav_label", p["title"]),
                        nav_order=p.get("nav_order", 0),
                        file=p["file"],
                        page_type=p.get("type", "dashboard"),
                        description=p.get("description", ""),
                        requires_skills=p.get("requires_skills", []),
                    )
                )

            return PracticeDefinition(
                name=data["name"],
                display_name=data.get("display_name", data["name"]),
                description=data.get("description", ""),
                version=data.get("version", "1.0.0"),
                icon=data.get("icon", ""),
                integrations=data.get("integrations", []),
                skills=data.get("skills", []),
                pages=pages,
                system_prompt_addon=data.get("system_prompt_addon", ""),
                built_in=built_in,
                base_path=str(manifest_path.parent),
            )
        except Exception as e:
            logger.error(f"Failed to load practice manifest {manifest_path}: {e}")
            return None

    def _load_practice_skills(
        self,
        skills_dir: Path,
        practice: PracticeDefinition,
        skill_registry: SkillRegistry,
    ) -> None:
        """Load skills from an uploaded practice using worker_path."""
        from agent.skills.registry import SkillDefinition

        for skill_path in sorted(skills_dir.iterdir()):
            if not skill_path.is_dir():
                continue

            yaml_file = skill_path / "skill.yaml"
            worker_file = skill_path / "worker.py"
            if not yaml_file.exists() or not worker_file.exists():
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
                worker_path=str(worker_file.resolve()),
            )
            skill_registry.register(skill)
            logger.info(f"Registered uploaded skill: {skill.name} (practice: {practice.name})")

    def _find_manifest_in_zip(self, zf: zipfile.ZipFile) -> str | None:
        """Find practice.yaml in a ZIP — at root or one level deep."""
        names = zf.namelist()

        # Direct root
        if "practice.yaml" in names:
            return "practice.yaml"

        # One level deep (e.g., cbt-practice/practice.yaml)
        for name in names:
            parts = name.split("/")
            if len(parts) == 2 and parts[1] == "practice.yaml":
                return name

        return None
