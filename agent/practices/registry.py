"""
Practice Registry — manages installed practices.

Loads practice manifests (practice.yaml) from built-in and uploaded
directories, registers their skills with the SkillRegistry, and
provides page resolution for the dev server.

Implementation is split across:
- ``installer.py`` — manifest loading, ZIP install, BlobStore restore, hooks
- ``deployer.py`` — AWS Lambda + EventBridge deployment
- ``assets.py`` — page/nav resolution for tenants

This module owns the in-memory ``_practices`` state and the public API.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from agent.interfaces.blob_store import BlobStore
from agent.models.practice import PracticeDefinition
from agent.practices import assets, deployer, installer
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

            practice = installer.load_manifest(manifest, built_in=True)
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

            practice = installer.load_manifest(manifest, built_in=False)
            if practice:
                self._practices[practice.name] = practice
                logger.info(f"Loaded uploaded practice: {practice.name}")

    async def restore_from_blob_store(
        self,
        blob_store: BlobStore,
        tenant_id: str,
        data_dir: Path,
        installed_versions: dict[str, str] | None = None,
    ) -> int:
        """Download and extract uploaded practices from BlobStore on startup."""
        return await installer.restore_from_blob_store(
            self._practices, blob_store, tenant_id, data_dir, installed_versions
        )

    async def install_zip(
        self,
        zip_bytes: bytes,
        data_dir: Path,
        blob_store: BlobStore | None = None,
        tenant_id: str = "",
        installed_versions: dict[str, str] | None = None,
    ) -> PracticeDefinition:
        """Validate and extract a practice ZIP, register the result."""
        return await installer.install_zip(
            self._practices,
            zip_bytes,
            data_dir,
            blob_store=blob_store,
            tenant_id=tenant_id,
            installed_versions=installed_versions,
        )

    def get(self, name: str) -> PracticeDefinition | None:
        """Get a practice by name."""
        return self._practices.get(name)

    def list_all(self) -> list[PracticeDefinition]:
        """List all installed practices."""
        return list(self._practices.values())

    def get_pages_for_tenant(self, tenant: Any) -> list[dict[str, Any]]:
        """Get pages available to a tenant based on their practice configuration."""
        return assets.get_pages_for_tenant(self._practices, tenant)

    def get_page_path(self, practice_name: str, page_slug: str) -> Path | None:
        """Resolve a practice page to its filesystem path."""
        return assets.get_page_path(self._practices, practice_name, page_slug)

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

    async def deploy_skill_lambdas(
        self,
        practice: PracticeDefinition,
        config: dict[str, Any],
    ) -> list[str]:
        """Deploy Lambda functions + EventBridge rules for each skill with a lambda.zip."""
        return await deployer.deploy_skill_lambdas(practice, config)

    async def ensure_skill_lambdas(
        self,
        config: dict[str, Any],
    ) -> int:
        """Check that Lambdas exist for all practice skills. Deploy if missing."""
        return await deployer.ensure_skill_lambdas(self._practices, config)

    # --- Private helpers ---

    def _load_practice_skills(
        self,
        skills_dir: Path,
        practice: PracticeDefinition,
        skill_registry: SkillRegistry,
    ) -> None:
        """Load skills from a practice using worker_path."""
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
            logger.info(f"Registered practice skill: {skill.name} (practice: {practice.name})")
