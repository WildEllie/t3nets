"""
Tests for `t3nets practice run-local` and the `--extra-practice-dir` flag
on the dev server.

These validate:
- The practice registry loads practices from extra directories.
- `run-local` validates the practice before attempting to start.
- `run-local` constructs the correct subprocess command for the dev server.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from t3nets_sdk.cli.main import build_parser
from t3nets_sdk.contracts import SkillContext

from agent.practices.registry import PracticeRegistry
from agent.skills.registry import SkillRegistry


def _scaffold_practice(root: Path) -> Path:
    """Create a minimal valid practice at root/my-practice."""
    parser = build_parser()
    args = parser.parse_args(["practice", "init", "my-practice", "--dir", str(root)])
    args.func(args)
    return root / "my-practice"


def _run(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


# ---------- PracticeRegistry extra-dir loading ----------


class TestExtraPracticeDirLoading:
    def test_load_builtin_loads_from_extra_directory(self, tmp_path: Path) -> None:
        practice_root = _scaffold_practice(tmp_path)
        registry = PracticeRegistry()
        registry.load_builtin(practice_root.parent)
        assert registry.get("my-practice") is not None

    def test_extra_practice_skills_register(self, tmp_path: Path) -> None:
        practice_root = _scaffold_practice(tmp_path)
        practice_reg = PracticeRegistry()
        practice_reg.load_builtin(practice_root.parent)
        skill_reg = SkillRegistry()
        practice_reg.register_skills(skill_reg)
        assert skill_reg.get_skill("example") is not None

    async def test_extra_practice_skill_is_callable(self, tmp_path: Path) -> None:
        practice_root = _scaffold_practice(tmp_path)
        practice_reg = PracticeRegistry()
        practice_reg.load_builtin(practice_root.parent)
        skill_reg = SkillRegistry()
        practice_reg.register_skills(skill_reg)
        worker = skill_reg.get_worker("example")
        result = await worker(SkillContext(tenant_id="t1"), {"message": "hi"})
        assert result.success is True
        assert result.data == {"echo": "hi"}


# ---------- run-local CLI ----------


class TestRunLocal:
    def test_rejects_invalid_practice(self, tmp_path: Path) -> None:
        rc = _run(["practice", "run-local", "--dir", str(tmp_path)])
        assert rc != 0

    def test_rejects_broken_practice(self, tmp_path: Path) -> None:
        practice_root = _scaffold_practice(tmp_path)
        (practice_root / "skills/example/worker.py").unlink()
        rc = _run(["practice", "run-local", "--dir", str(practice_root)])
        assert rc != 0

    @patch("t3nets_sdk.cli.run_local.subprocess.run")
    def test_constructs_correct_subprocess_command(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        practice_root = _scaffold_practice(tmp_path)
        mock_run.return_value = MagicMock(returncode=0)

        rc = _run(["practice", "run-local", "--dir", str(practice_root)])
        assert rc == 0
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "adapters.local.dev_server"]
        assert "--extra-practice-dir" in cmd
        assert str(practice_root) in cmd

    @patch("t3nets_sdk.cli.run_local.subprocess.run")
    def test_forwards_port_flag(self, mock_run: MagicMock, tmp_path: Path) -> None:
        practice_root = _scaffold_practice(tmp_path)
        mock_run.return_value = MagicMock(returncode=0)

        _run(["practice", "run-local", "--dir", str(practice_root), "--port", "9090"])
        cmd = mock_run.call_args[0][0]
        assert "--port" in cmd
        assert "9090" in cmd
