"""
Practice Registry tests.

Verifies:
- Loading built-in practices from directory
- ZIP validation and installation
- Skill registration into SkillRegistry
- get_pages_for_tenant resolution
- Asset upload on install
- Install hook execution
"""

import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.models.tenant import Tenant, TenantSettings
from agent.practices.registry import PracticeRegistry
from agent.skills.registry import SkillRegistry


def _make_practice_zip(
    name="test_practice",
    skills=None,
    pages=None,
    assets=None,
    hooks=None,
    extra_files=None,
):
    """Create a practice ZIP in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        manifest = {
            "name": name,
            "display_name": f"Test {name}",
            "description": "A test practice",
            "version": "1.0.0",
            "skills": [s["name"] for s in (skills or [])],
            "pages": pages or [],
            "assets": assets or [],
            "hooks": hooks or {},
        }
        import yaml  # type: ignore[import-untyped]

        zf.writestr("practice.yaml", yaml.dump(manifest))

        for skill in skills or []:
            skill_yaml = {
                "name": skill["name"],
                "description": f"Test skill {skill['name']}",
                "parameters": {"type": "object", "properties": {}},
            }
            zf.writestr(f"skills/{skill['name']}/skill.yaml", yaml.dump(skill_yaml))
            worker_code = skill.get(
                "worker_code", "def execute(params, secrets): return {'ok': True}"
            )
            zf.writestr(f"skills/{skill['name']}/worker.py", worker_code)

        for page in pages or []:
            zf.writestr(page["file"], f"<html><body>{page['title']}</body></html>")

        for asset in assets or []:
            zf.writestr(f"assets/{asset}", f"asset-content-{asset}")

        if hooks and hooks.get("on_install"):
            hook_code = (extra_files or {}).get(hooks["on_install"], "def on_install(ctx): pass")
            zf.writestr(hooks["on_install"], hook_code)

    buf.seek(0)
    return buf.read()


@pytest.fixture
def tmp_dir():
    tmp = tempfile.mkdtemp()
    data_dir = Path(tmp) / "data"
    data_dir.mkdir()
    yield data_dir
    shutil.rmtree(tmp, ignore_errors=True)


class TestPracticeRegistrySync:
    """Synchronous tests (no install_zip)."""

    def test_load_builtin(self):
        registry = PracticeRegistry()
        practices_dir = Path(__file__).parent.parent / "agent" / "practices"
        registry.load_builtin(practices_dir)
        practices = registry.list_all()
        names = [p.name for p in practices]
        assert "dev-jira" in names

    def test_load_builtin_dev_jira(self):
        registry = PracticeRegistry()
        practices_dir = Path(__file__).parent.parent / "agent" / "practices"
        registry.load_builtin(practices_dir)
        practice = registry.get("dev-jira")
        assert practice is not None
        assert "sprint_status" in practice.skills
        assert "release_notes" in practice.skills

    def test_register_skills(self):
        registry = PracticeRegistry()
        practices_dir = Path(__file__).parent.parent / "agent" / "practices"
        registry.load_builtin(practices_dir)

        skill_reg = SkillRegistry()
        registry.register_skills(skill_reg)
        names = skill_reg.list_skill_names()
        assert "sprint_status" in names
        assert "release_notes" in names

    def test_nonexistent_practice(self):
        registry = PracticeRegistry()
        assert registry.get("nonexistent") is None
        assert registry.get_page_path("nonexistent", "page") is None


class TestPracticeRegistryAsync:
    """Async tests that use install_zip."""

    @pytest.mark.asyncio
    async def test_install_zip(self, tmp_dir):
        zip_bytes = _make_practice_zip(
            name="mytest",
            skills=[{"name": "hello"}],
        )
        registry = PracticeRegistry()
        practice = await registry.install_zip(zip_bytes, tmp_dir)
        assert practice.name == "mytest"
        assert "hello" in practice.skills
        assert registry.get("mytest") is not None

    @pytest.mark.asyncio
    async def test_install_zip_registers_skills(self, tmp_dir):
        zip_bytes = _make_practice_zip(
            name="ziptest",
            skills=[{"name": "greet", "worker_code": "def execute(p, s): return {'msg': 'hi'}"}],
        )
        registry = PracticeRegistry()
        await registry.install_zip(zip_bytes, tmp_dir)

        skill_reg = SkillRegistry()
        registry.register_skills(skill_reg)
        # After step 5 get_worker returns a normalized async wrapper that
        # presents the new (SkillContext, params) -> SkillResult contract.
        # Legacy workers like this one are wrapped transparently.
        from t3nets_sdk.contracts import SkillContext

        worker = skill_reg.get_worker("greet")
        result = await worker(SkillContext(tenant_id="t1"), {})
        assert result.success is True
        assert result.data == {"msg": "hi"}

    @pytest.mark.asyncio
    async def test_install_zip_missing_manifest(self, tmp_dir):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "no manifest here")
        buf.seek(0)

        registry = PracticeRegistry()
        with pytest.raises(ValueError, match="practice.yaml"):
            await registry.install_zip(buf.read(), tmp_dir)

    @pytest.mark.asyncio
    async def test_install_zip_missing_skill_files(self, tmp_dir):
        buf = io.BytesIO()
        import yaml  # type: ignore[import-untyped]

        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("practice.yaml", yaml.dump({"name": "bad", "skills": ["missing"]}))
        buf.seek(0)

        registry = PracticeRegistry()
        with pytest.raises(ValueError, match="Missing skill.yaml"):
            await registry.install_zip(buf.read(), tmp_dir)

    @pytest.mark.asyncio
    async def test_install_zip_path_traversal(self, tmp_dir):
        buf = io.BytesIO()
        import yaml  # type: ignore[import-untyped]

        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("practice.yaml", yaml.dump({"name": "safe"}))
            zf.writestr("../../etc/passwd", "hacked")
        buf.seek(0)

        registry = PracticeRegistry()
        with pytest.raises(ValueError, match="Invalid path"):
            await registry.install_zip(buf.read(), tmp_dir)

    @pytest.mark.asyncio
    async def test_get_pages_for_tenant(self, tmp_dir):
        zip_bytes = _make_practice_zip(
            name="paged",
            skills=[],
            pages=[
                {
                    "slug": "dash",
                    "title": "Dashboard",
                    "nav_label": "Dash",
                    "nav_order": 10,
                    "file": "pages/dash.html",
                }
            ],
        )
        registry = PracticeRegistry()
        await registry.install_zip(zip_bytes, tmp_dir)

        tenant = Tenant(
            tenant_id="t1",
            name="Test",
            settings=TenantSettings(primary_practice="paged"),
        )
        pages = registry.get_pages_for_tenant(tenant)
        assert len(pages) == 1
        assert pages[0]["slug"] == "dash"
        assert pages[0]["url"] == "/p/paged/dash"

    @pytest.mark.asyncio
    async def test_get_page_path(self, tmp_dir):
        zip_bytes = _make_practice_zip(
            name="pathtest",
            skills=[],
            pages=[
                {
                    "slug": "main",
                    "title": "Main",
                    "nav_label": "Main",
                    "nav_order": 1,
                    "file": "pages/main.html",
                }
            ],
        )
        registry = PracticeRegistry()
        await registry.install_zip(zip_bytes, tmp_dir)

        path = registry.get_page_path("pathtest", "main")
        assert path is not None
        assert path.exists()
