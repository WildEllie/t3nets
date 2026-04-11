"""
`t3nets practice validate` — lint a practice repository.

Runs the same pydantic validators `install_zip()` uses (so errors match
what an install would surface) plus filesystem checks: every skill
referenced in `practice.yaml` must have a `skill.yaml` + `worker.py`,
every page file must exist, every hook file must exist.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from t3nets_sdk.manifest import ManifestError, parse_practice_yaml, parse_skill_yaml


def collect_errors(practice_dir: Path) -> list[str]:
    """Return a list of validation errors. Empty list = valid."""
    errors: list[str] = []

    manifest_path = practice_dir / "practice.yaml"
    if not manifest_path.exists():
        return [f"practice.yaml not found at {manifest_path}"]

    try:
        manifest = parse_practice_yaml(manifest_path.read_text())
    except ManifestError as e:
        return [str(e)]

    for skill_name in manifest.skills:
        skill_dir = practice_dir / "skills" / skill_name
        skill_yaml = skill_dir / "skill.yaml"
        worker_py = skill_dir / "worker.py"
        if not skill_yaml.exists():
            errors.append(f"skill '{skill_name}': missing skill.yaml at {skill_yaml}")
        else:
            try:
                parse_skill_yaml(skill_yaml.read_text())
            except ManifestError as e:
                errors.append(f"skill '{skill_name}': {e}")
        if not worker_py.exists():
            errors.append(f"skill '{skill_name}': missing worker.py at {worker_py}")

    for page in manifest.pages:
        page_path = practice_dir / page.file
        if not page_path.exists():
            errors.append(f"page '{page.slug}': file not found at {page_path}")

    for hook_name, hook_file in manifest.hooks.items():
        hook_path = practice_dir / hook_file
        if not hook_path.exists():
            errors.append(f"hook '{hook_name}': file not found at {hook_path}")

    return errors


def run(args: argparse.Namespace) -> int:
    practice_dir = Path(args.dir).resolve()
    errors = collect_errors(practice_dir)
    if errors:
        print(
            f"Practice validation failed ({len(errors)} error(s)):",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print(f"OK — practice at {practice_dir} is valid")
    return 0
