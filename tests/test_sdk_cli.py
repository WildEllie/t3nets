"""
Tests for `t3nets_sdk.cli` — the `t3nets practice init|validate|package`
subcommands that ship in the SDK.

These exercise the CLI through its argparse surface so the same code path
a practice author hits is the one under test. They also assert that the
scaffold `init` emits round-trips through the pydantic manifest validator
and through the `package` → zip flow, so breakage anywhere in step 4 fails
here rather than in a real practice repo.
"""

from __future__ import annotations

import asyncio
import importlib.util
import zipfile
from pathlib import Path

from t3nets_sdk.cli.main import build_parser
from t3nets_sdk.contracts import SkillContext, SkillResult
from t3nets_sdk.manifest import parse_practice_yaml


def _run(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


# ---------- init ----------


class TestInit:
    def test_creates_expected_files(self, tmp_path: Path) -> None:
        rc = _run(["practice", "init", "my-practice", "--dir", str(tmp_path)])
        assert rc == 0
        root = tmp_path / "my-practice"
        assert (root / "practice.yaml").exists()
        assert (root / "skills/example/skill.yaml").exists()
        assert (root / "skills/example/worker.py").exists()
        assert (root / "tests/test_example.py").exists()
        assert (root / "README.md").exists()

    def test_scaffolded_practice_yaml_parses_against_sdk_schema(self, tmp_path: Path) -> None:
        _run(["practice", "init", "demo", "--dir", str(tmp_path)])
        m = parse_practice_yaml((tmp_path / "demo" / "practice.yaml").read_text())
        assert m.name == "demo"
        assert "example" in m.skills

    def test_rejects_invalid_name(self, tmp_path: Path) -> None:
        rc = _run(["practice", "init", "bad name", "--dir", str(tmp_path)])
        assert rc != 0
        assert not (tmp_path / "bad name").exists()

    def test_rejects_existing_destination(self, tmp_path: Path) -> None:
        (tmp_path / "busy").mkdir()
        rc = _run(["practice", "init", "busy", "--dir", str(tmp_path)])
        assert rc != 0

    def test_scaffolded_worker_matches_typed_contract(self, tmp_path: Path) -> None:
        """The scaffolded worker must accept (SkillContext, params) and return
        a SkillResult — this is the runtime contract the platform calls. A
        regression here means new authors hit a TypeError on first invoke."""
        _run(["practice", "init", "demo", "--dir", str(tmp_path)])
        worker_path = tmp_path / "demo" / "skills" / "example" / "worker.py"
        spec = importlib.util.spec_from_file_location("scaffolded_worker", worker_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        ctx = SkillContext(tenant_id="test-tenant")
        result = asyncio.run(module.execute(ctx, {"message": "hi"}))
        assert isinstance(result, SkillResult)
        assert result.success
        assert result.data == {"echo": "hi"}


# ---------- validate ----------


class TestValidate:
    def _scaffold(self, tmp_path: Path, name: str = "my-practice") -> Path:
        _run(["practice", "init", name, "--dir", str(tmp_path)])
        return tmp_path / name

    def test_happy_path_on_fresh_scaffold(self, tmp_path: Path) -> None:
        root = self._scaffold(tmp_path)
        assert _run(["practice", "validate", "--dir", str(root)]) == 0

    def test_missing_practice_yaml(self, tmp_path: Path) -> None:
        assert _run(["practice", "validate", "--dir", str(tmp_path)]) != 0

    def test_missing_skill_worker(self, tmp_path: Path) -> None:
        root = self._scaffold(tmp_path)
        (root / "skills/example/worker.py").unlink()
        assert _run(["practice", "validate", "--dir", str(root)]) != 0

    def test_missing_skill_yaml(self, tmp_path: Path) -> None:
        root = self._scaffold(tmp_path)
        (root / "skills/example/skill.yaml").unlink()
        assert _run(["practice", "validate", "--dir", str(root)]) != 0

    def test_invalid_practice_yaml_name(self, tmp_path: Path) -> None:
        root = self._scaffold(tmp_path)
        (root / "practice.yaml").write_text("name: bad name\n")
        assert _run(["practice", "validate", "--dir", str(root)]) != 0

    def test_missing_page_file(self, tmp_path: Path) -> None:
        root = self._scaffold(tmp_path)
        (root / "practice.yaml").write_text(
            "name: my-practice\n"
            "skills: [example]\n"
            "pages:\n"
            "  - slug: dash\n"
            "    title: Dashboard\n"
            "    file: pages/missing.html\n"
        )
        assert _run(["practice", "validate", "--dir", str(root)]) != 0

    def test_missing_hook_file(self, tmp_path: Path) -> None:
        root = self._scaffold(tmp_path)
        (root / "practice.yaml").write_text(
            "name: my-practice\nskills: [example]\nhooks:\n  on_install: hooks/absent.py\n"
        )
        assert _run(["practice", "validate", "--dir", str(root)]) != 0


# ---------- package ----------


class TestPackage:
    def _scaffold(self, tmp_path: Path, name: str = "my-practice") -> Path:
        _run(["practice", "init", name, "--dir", str(tmp_path)])
        return tmp_path / name

    def test_builds_zip_with_manifest_at_root(self, tmp_path: Path) -> None:
        root = self._scaffold(tmp_path)
        out = tmp_path / "out.zip"
        rc = _run(["practice", "package", "--dir", str(root), "--output", str(out)])
        assert rc == 0
        assert out.exists()
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            assert "practice.yaml" in names
            assert "skills/example/skill.yaml" in names
            assert "skills/example/worker.py" in names
            # Dev-only files don't ship
            assert not any(n.startswith("tests/") for n in names)
            assert not any("__pycache__" in n for n in names)

    def test_default_output_goes_to_dist_practice_zip(self, tmp_path: Path) -> None:
        root = self._scaffold(tmp_path)
        rc = _run(["practice", "package", "--dir", str(root)])
        assert rc == 0
        assert (root / "dist" / "practice.zip").exists()

    def test_excludes_dot_git_and_caches(self, tmp_path: Path) -> None:
        root = self._scaffold(tmp_path)
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        out = tmp_path / "out.zip"
        _run(["practice", "package", "--dir", str(root), "--output", str(out)])
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            assert not any(n.startswith(".git/") for n in names)
            assert not any("__pycache__" in n for n in names)
            assert not any(n.endswith(".pyc") for n in names)

    def test_validation_failure_blocks_package(self, tmp_path: Path) -> None:
        root = self._scaffold(tmp_path)
        (root / "skills/example/worker.py").unlink()
        out = tmp_path / "out.zip"
        rc = _run(["practice", "package", "--dir", str(root), "--output", str(out)])
        assert rc != 0
        assert not out.exists()

    def test_zip_manifest_round_trips_through_sdk_parser(self, tmp_path: Path) -> None:
        root = self._scaffold(tmp_path)
        out = tmp_path / "out.zip"
        _run(["practice", "package", "--dir", str(root), "--output", str(out)])
        with zipfile.ZipFile(out) as zf:
            text = zf.read("practice.yaml").decode("utf-8")
        m = parse_practice_yaml(text)
        assert m.name == "my-practice"
        assert "example" in m.skills
