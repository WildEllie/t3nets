"""
`t3nets practice package` — build a distributable `practice.zip`.

Validates first (fails loudly if the practice is broken), then zips the
directory with `practice.yaml` at the archive root so it drops straight
into the platform's `install_zip()` path. Dev/CI noise (`.git`, caches,
build output, tests) is excluded — the archive should only contain files
the platform actually needs at install time.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

from t3nets_sdk.cli.validate import collect_errors

_EXCLUDE_DIRS = {
    ".git",
    ".github",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "tests",
}
_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
_EXCLUDE_NAMES = {".DS_Store"}


def _is_excluded(rel: Path) -> bool:
    if any(part in _EXCLUDE_DIRS for part in rel.parts):
        return True
    if rel.name in _EXCLUDE_NAMES:
        return True
    if rel.suffix in _EXCLUDE_SUFFIXES:
        return True
    if rel.name.endswith(".egg-info"):
        return True
    return False


def run(args: argparse.Namespace) -> int:
    practice_dir = Path(args.dir).resolve()
    errors = collect_errors(practice_dir)
    if errors:
        print(
            f"Cannot package — practice validation failed ({len(errors)} error(s)):",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    if args.output:
        output = Path(args.output).resolve()
    else:
        output = practice_dir / "dist" / "practice.zip"
    output.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(practice_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.resolve() == output:
                continue
            rel = path.relative_to(practice_dir)
            if _is_excluded(rel):
                continue
            zf.write(path, arcname=str(rel))
            count += 1

    print(f"Packaged {count} files -> {output}")
    return 0
