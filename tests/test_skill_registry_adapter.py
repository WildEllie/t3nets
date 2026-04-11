"""
Tests for `agent.skills.registry` normalized worker loader.

After step 5, `SkillRegistry.get_worker()` returns a normalized callable
that always presents the new `(SkillContext, params) -> SkillResult`
contract, wrapping legacy workers transparently. These tests exercise
every supported worker shape (new async, legacy sync, legacy async,
3-arg legacy with ctx bag) plus the dict-to-SkillResult coercion rules
so a regression in the wrapper fails here, not in integration tests.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest
from t3nets_sdk.contracts import SkillContext, SkillResult

from agent.skills.registry import (
    SkillDefinition,
    SkillNotFoundError,
    SkillRegistry,
    _coerce_result,
    _is_new_contract,
    _normalize_worker,
)


def _write_worker_module(tmp_path: Path, name: str, source: str) -> Path:
    """Write a worker module to tmp_path and make it importable."""
    mod_dir = tmp_path / name
    mod_dir.mkdir()
    (mod_dir / "__init__.py").write_text("")
    (mod_dir / "worker.py").write_text(textwrap.dedent(source))
    sys.path.insert(0, str(tmp_path))
    return mod_dir / "worker.py"


# ---------- _coerce_result ----------


class TestCoerceResult:
    def test_skill_result_passthrough(self) -> None:
        r = SkillResult.ok({"x": 1})
        assert _coerce_result(r) is r

    def test_plain_dict_becomes_ok(self) -> None:
        r = _coerce_result({"sprint": 42})
        assert r.success is True
        assert r.data == {"sprint": 42}

    def test_error_only_dict_becomes_fail(self) -> None:
        r = _coerce_result({"error": "boom"})
        assert r.success is False
        assert r.error == "boom"
        assert r.data == {}

    def test_error_with_extra_data_becomes_fail_with_data(self) -> None:
        r = _coerce_result({"error": "boom", "retry_after": 30})
        assert r.success is False
        assert r.error == "boom"
        assert r.data == {"retry_after": 30}

    def test_non_dict_non_skillresult_wraps_in_value(self) -> None:
        r = _coerce_result("hello")
        assert r.success is True
        assert r.data == {"value": "hello"}


# ---------- _is_new_contract ----------


class TestIsNewContract:
    def test_annotation_match(self) -> None:
        async def fn(ctx: SkillContext, params: dict) -> SkillResult:
            return SkillResult.ok()

        assert _is_new_contract(fn) is True

    def test_ctx_name_without_annotation(self) -> None:
        async def fn(ctx, params):  # type: ignore[no-untyped-def]
            return SkillResult.ok()

        assert _is_new_contract(fn) is True

    def test_legacy_params_secrets(self) -> None:
        def fn(params, secrets):  # type: ignore[no-untyped-def]
            return {}

        assert _is_new_contract(fn) is False

    def test_legacy_three_arg(self) -> None:
        def fn(params, secrets, ctx_bag):  # type: ignore[no-untyped-def]
            return {}

        assert _is_new_contract(fn) is False


# ---------- _normalize_worker ----------


class TestNormalizeWorker:
    async def test_new_contract_async_worker(self) -> None:
        async def execute(ctx: SkillContext, params: dict) -> SkillResult:
            return SkillResult.ok({"echo": params.get("msg")})

        wrapped = _normalize_worker(execute, "t")
        result = await wrapped(SkillContext(tenant_id="t1"), {"msg": "hi"})
        assert result.success is True
        assert result.data == {"echo": "hi"}

    async def test_new_contract_sync_worker_wrapped(self) -> None:
        def execute(ctx: SkillContext, params: dict) -> SkillResult:
            return SkillResult.ok({"n": params["n"] * 2})

        wrapped = _normalize_worker(execute, "t")
        result = await wrapped(SkillContext(tenant_id="t1"), {"n": 21})
        assert result.data == {"n": 42}

    async def test_legacy_two_arg_sync(self) -> None:
        def execute(params, secrets):  # type: ignore[no-untyped-def]
            return {"saw_secret": secrets.get("token")}

        wrapped = _normalize_worker(execute, "t")
        ctx = SkillContext(tenant_id="t1", secrets={"token": "abc"})
        result = await wrapped(ctx, {})
        assert result.success is True
        assert result.data == {"saw_secret": "abc"}

    async def test_legacy_two_arg_async(self) -> None:
        async def execute(params, secrets):  # type: ignore[no-untyped-def]
            return {"ok": True, "from": params.get("src")}

        wrapped = _normalize_worker(execute, "t")
        result = await wrapped(SkillContext(tenant_id="t1"), {"src": "x"})
        assert result.data == {"ok": True, "from": "x"}

    async def test_legacy_three_arg_receives_ctx_bag(self) -> None:
        captured: dict = {}

        def execute(params, secrets, ctx_bag):  # type: ignore[no-untyped-def]
            captured.update(ctx_bag)
            return {"ok": True}

        wrapped = _normalize_worker(execute, "t")
        ctx = SkillContext(tenant_id="t1", extras={"request_id": "r1"})
        await wrapped(ctx, {})
        assert captured["tenant_id"] == "t1"
        assert captured["request_id"] == "r1"
        # blob_store is None here; just assert the key is present
        assert "blob_store" in captured

    async def test_legacy_dict_with_error_becomes_fail(self) -> None:
        def execute(params, secrets):  # type: ignore[no-untyped-def]
            return {"error": "integration missing"}

        wrapped = _normalize_worker(execute, "t")
        result = await wrapped(SkillContext(tenant_id="t1"), {})
        assert result.success is False
        assert result.error == "integration missing"

    async def test_legacy_worker_exception_becomes_fail(self) -> None:
        def execute(params, secrets):  # type: ignore[no-untyped-def]
            raise RuntimeError("kaboom")

        wrapped = _normalize_worker(execute, "t")
        result = await wrapped(SkillContext(tenant_id="t1"), {})
        assert result.success is False
        assert "kaboom" in (result.error or "")


# ---------- SkillRegistry.get_worker end-to-end ----------


class TestRegistryGetWorker:
    def test_unknown_skill(self) -> None:
        reg = SkillRegistry()
        with pytest.raises(SkillNotFoundError):
            reg.get_worker("nope")

    async def test_worker_path_legacy_worker_via_filesystem_load(self, tmp_path: Path) -> None:
        worker_py = tmp_path / "worker.py"
        worker_py.write_text(
            "def execute(params, secrets):\n    return {'echoed': params.get('x')}\n"
        )
        reg = SkillRegistry()
        reg.register(
            SkillDefinition(
                name="echo",
                description="legacy echo",
                parameters={},
                requires_integration=None,
                worker_path=str(worker_py),
            )
        )
        wrapped = reg.get_worker("echo")
        result = await wrapped(SkillContext(tenant_id="t1"), {"x": "hi"})
        assert result.data == {"echoed": "hi"}

    async def test_worker_path_new_contract_via_filesystem_load(self, tmp_path: Path) -> None:
        worker_py = tmp_path / "worker.py"
        worker_py.write_text(
            "from t3nets_sdk.contracts import SkillContext, SkillResult\n"
            "async def execute(ctx: SkillContext, params):\n"
            "    return SkillResult.ok({'tenant': ctx.tenant_id})\n"
        )
        reg = SkillRegistry()
        reg.register(
            SkillDefinition(
                name="new",
                description="new-contract",
                parameters={},
                requires_integration=None,
                worker_path=str(worker_py),
            )
        )
        wrapped = reg.get_worker("new")
        result = await wrapped(SkillContext(tenant_id="acme"), {})
        assert result.data == {"tenant": "acme"}


# ---------- built-in ping skill migrated to new contract ----------


class TestBuiltinPingMigrated:
    """`ping` is the reference migration for step 5. If this regresses,
    the migration pattern for sprint_status/release_notes is broken too."""

    async def test_ping_returns_skill_result_with_healthy_payload(self) -> None:
        from agent.skills.ping.worker import execute

        result = await execute(SkillContext(tenant_id="t1"), {})
        assert isinstance(result, SkillResult)
        assert result.success is True
        assert result.data["status"] == "ok"
        assert "timestamp" in result.data

    async def test_ping_echoes(self) -> None:
        from agent.skills.ping.worker import execute

        result = await execute(SkillContext(tenant_id="t1"), {"echo": "hello"})
        assert result.data["echo"] == "hello"

    async def test_ping_through_registry_normalization(self) -> None:
        reg = SkillRegistry()
        reg.register(
            SkillDefinition(
                name="ping",
                description="ping",
                parameters={},
                requires_integration=None,
                worker_module="agent.skills.ping.worker",
            )
        )
        wrapped = reg.get_worker("ping")
        result = await wrapped(SkillContext(tenant_id="t1"), {"echo": "x"})
        assert result.success is True
        assert result.data["echo"] == "x"


# Silence unused-import warnings in static analyzers that don't see asyncio
_ = asyncio
