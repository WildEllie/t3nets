"""
Practice installer — manifest loading, ZIP install/extract/validation,
BlobStore restore, and on_install hooks.

Stateless helpers that operate on a `_practices` dict (mutated by callers).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import zipfile
from io import BytesIO
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from t3nets_sdk.manifest import parse_practice_yaml

from agent.interfaces.blob_store import BlobStore
from agent.models.practice import PracticeDefinition, PracticePage

logger = logging.getLogger(__name__)


def load_manifest(manifest_path: Path, built_in: bool) -> PracticeDefinition | None:
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
            assets=data.get("assets", []),
            hooks=data.get("hooks", {}),
            system_prompt_addon=data.get("system_prompt_addon", ""),
            built_in=built_in,
            base_path=str(manifest_path.parent),
        )
    except Exception as e:
        logger.error(f"Failed to load practice manifest {manifest_path}: {e}")
        return None


def parse_version(version: str) -> tuple[int, ...]:
    """Parse a version string like '1.2.3' into a comparable tuple."""
    try:
        return tuple(int(x) for x in version.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def find_manifest_in_zip(zf: zipfile.ZipFile) -> str | None:
    """Find practice.yaml in a ZIP — at root or one level deep."""
    names = zf.namelist()

    if "practice.yaml" in names:
        return "practice.yaml"

    for name in names:
        parts = name.split("/")
        if len(parts) == 2 and parts[1] == "practice.yaml":
            return name

    return None


async def install_zip(
    practices: dict[str, PracticeDefinition],
    zip_bytes: bytes,
    data_dir: Path,
    blob_store: BlobStore | None = None,
    tenant_id: str = "",
    installed_versions: dict[str, str] | None = None,
) -> PracticeDefinition:
    """
    Validate and extract a practice ZIP to the data directory.
    Optionally uploads assets to BlobStore and runs install hooks.

    Mutates `practices` to register the installed PracticeDefinition.
    Raises ValueError if validation fails.
    """
    buf = BytesIO(zip_bytes)
    try:
        zf = zipfile.ZipFile(buf)
    except zipfile.BadZipFile as e:
        raise ValueError(f"Invalid ZIP file: {e}") from e

    manifest_path = find_manifest_in_zip(zf)
    if not manifest_path:
        raise ValueError("ZIP must contain practice.yaml at root or in a single subdirectory")

    prefix = ""
    parts = manifest_path.split("/")
    if len(parts) > 1:
        prefix = "/".join(parts[:-1]) + "/"

    # Parse + validate manifest via the SDK pydantic schema. ManifestError
    # is a ValueError subclass, so existing callers that `except ValueError`
    # keep working.
    manifest = parse_practice_yaml(zf.read(manifest_path).decode("utf-8"))
    name = manifest.name

    new_version = manifest.version
    if installed_versions and name in installed_versions:
        cur_version = installed_versions[name]
        new_v = parse_version(new_version)
        cur_v = parse_version(cur_version)
        if new_v == cur_v:
            raise ValueError(f"Practice '{name}' v{new_version} is already installed")
        if new_v < cur_v:
            raise ValueError(
                f"Downgrade not allowed: '{name}' v{new_version} < installed v{cur_version}"
            )
        logger.info(f"Upgrading practice '{name}': v{cur_version} → v{new_version}")

    names = set(zf.namelist())
    for skill_name in manifest.skills:
        skill_yaml = f"{prefix}skills/{skill_name}/skill.yaml"
        skill_worker = f"{prefix}skills/{skill_name}/worker.py"
        if skill_yaml not in names:
            raise ValueError(f"Missing skill.yaml for skill '{skill_name}'")
        if skill_worker not in names:
            raise ValueError(f"Missing worker.py for skill '{skill_name}'")

    for page in manifest.pages:
        page_file = f"{prefix}{page.file}"
        if page_file not in names:
            raise ValueError(f"Missing page file: {page.file}")

    for member in names:
        if ".." in member or member.startswith("/"):
            raise ValueError(f"Invalid path in ZIP: {member}")

    dest = data_dir / "practices" / name
    dest.mkdir(parents=True, exist_ok=True)

    for member in zf.namelist():
        if member.endswith("/"):
            continue
        relative = member[len(prefix) :] if prefix and member.startswith(prefix) else member
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(zf.read(member))

    zf.close()

    practice = load_manifest(dest / "practice.yaml", built_in=False)
    if not practice:
        raise ValueError("Failed to load installed practice manifest")

    if blob_store and tenant_id:
        zip_key = f"practices/{practice.name}/practice.zip"
        await blob_store.put(tenant_id, zip_key, zip_bytes)
        logger.info(f"Persisted practice ZIP to BlobStore: {zip_key}")

    if blob_store and tenant_id and practice.assets:
        for asset in practice.assets:
            asset_path = dest / "assets" / asset
            if asset_path.exists():
                blob_key = f"practices/{practice.name}/assets/{asset}"
                await blob_store.put(tenant_id, blob_key, asset_path.read_bytes())
                logger.info(f"Uploaded practice asset: {blob_key}")

    if practice.hooks.get("on_install"):
        await run_install_hook(practice, dest, blob_store=blob_store, tenant_id=tenant_id)

    practices[practice.name] = practice
    logger.info(f"Installed practice: {practice.name} v{practice.version}")
    return practice


async def restore_from_blob_store(
    practices: dict[str, PracticeDefinition],
    blob_store: BlobStore,
    tenant_id: str,
    data_dir: Path,
    installed_versions: dict[str, str] | None = None,
) -> int:
    """Download and extract uploaded practices from BlobStore on startup.

    Uses installed_versions (from DynamoDB tenant settings) to know which
    practices to restore. Only downloads ZIPs for practices that aren't
    already extracted locally.

    Returns count of restored practices.
    """
    if not installed_versions:
        return 0

    restored = 0
    for name, version in installed_versions.items():
        dest = data_dir / "practices" / name
        if dest.exists():
            continue

        zip_key = f"practices/{name}/practice.zip"
        try:
            zip_bytes = await blob_store.get(tenant_id, zip_key)
            await install_zip(practices, zip_bytes, data_dir)
            restored += 1
            logger.info(f"Restored practice from S3: {name} v{version}")
        except Exception as e:
            logger.warning(f"Failed to restore practice {name}: {e}")

    return restored


async def run_install_hook(
    practice: PracticeDefinition,
    practice_dir: Path,
    blob_store: BlobStore | None = None,
    tenant_id: str = "",
) -> None:
    """Run the on_install hook for a practice."""
    hook_file = practice.hooks.get("on_install", "")
    if not hook_file:
        return

    hook_path = practice_dir / hook_file
    if not hook_path.exists():
        logger.warning(f"Install hook not found: {hook_path}")
        return

    try:
        spec = importlib.util.spec_from_file_location("install_hook", str(hook_path))
        if spec is None or spec.loader is None:
            logger.error(f"Cannot load install hook: {hook_path}")
            return
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        if hasattr(module, "on_install"):
            ctx = {
                "blob_store": blob_store,
                "tenant_id": tenant_id,
                "practice_dir": str(practice_dir),
            }
            result = module.on_install(ctx)
            if asyncio.iscoroutine(result):
                await result
            logger.info(f"Ran install hook for practice: {practice.name}")
    except Exception as e:
        logger.error(f"Install hook failed for {practice.name}: {e}")
