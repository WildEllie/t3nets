"""
Tests for `t3nets_sdk.contracts` — `SkillContext`, `SkillResult`, `Worker`.

These cover the public surface a practice author writes against: the
result envelope's success/fail constructors, its `to_dict()` shape (which
is what the router serializes and what the rest of the stack was
historically seeing as a plain dict), and the `Worker` protocol check.
"""

from __future__ import annotations

import asyncio
import logging

from t3nets_sdk.contracts import SkillContext, SkillResult, Worker


class TestSkillResultOk:
    def test_ok_from_dict(self) -> None:
        r = SkillResult.ok({"sprint": 42})
        assert r.success is True
        assert r.error is None
        assert r.data == {"sprint": 42}

    def test_ok_from_kwargs(self) -> None:
        r = SkillResult.ok(sprint=42, issues=[1, 2])
        assert r.success is True
        assert r.data == {"sprint": 42, "issues": [1, 2]}

    def test_ok_merges_dict_and_kwargs(self) -> None:
        r = SkillResult.ok({"a": 1, "b": 2}, b=99, c=3)
        assert r.data == {"a": 1, "b": 99, "c": 3}

    def test_ok_empty(self) -> None:
        r = SkillResult.ok()
        assert r.success is True
        assert r.data == {}

    def test_bool_true(self) -> None:
        assert bool(SkillResult.ok({"x": 1})) is True

    def test_to_dict_returns_data(self) -> None:
        r = SkillResult.ok({"sprint": 42})
        assert r.to_dict() == {"sprint": 42}

    def test_to_dict_is_a_copy(self) -> None:
        payload = {"sprint": 42}
        r = SkillResult.ok(payload)
        r.to_dict()["mutated"] = True
        assert "mutated" not in r.data


class TestSkillResultFail:
    def test_fail_sets_error(self) -> None:
        r = SkillResult.fail("boom")
        assert r.success is False
        assert r.error == "boom"
        assert r.data == {}

    def test_fail_with_structured_data(self) -> None:
        r = SkillResult.fail("rate_limited", retry_after=30)
        assert r.data == {"retry_after": 30}

    def test_bool_false(self) -> None:
        assert bool(SkillResult.fail("nope")) is False

    def test_to_dict_surfaces_error_first(self) -> None:
        r = SkillResult.fail("boom", retry_after=30)
        out = r.to_dict()
        assert out["error"] == "boom"
        assert out["retry_after"] == 30

    def test_to_dict_falls_back_when_error_is_none(self) -> None:
        r = SkillResult(success=False, data={}, error=None)
        assert r.to_dict() == {"error": "skill failed"}


class TestSkillContext:
    def test_defaults(self) -> None:
        ctx = SkillContext(tenant_id="t1")
        assert ctx.tenant_id == "t1"
        assert ctx.secrets == {}
        assert ctx.blob_store is None
        assert isinstance(ctx.logger, logging.Logger)
        assert ctx.extras == {}

    def test_full_construction(self) -> None:
        ctx = SkillContext(
            tenant_id="t1",
            secrets={"url": "https://x", "token": "abc"},
            logger=logging.getLogger("custom"),
            extras={"request_id": "r1"},
        )
        assert ctx.secrets["token"] == "abc"
        assert ctx.logger.name == "custom"
        assert ctx.extras["request_id"] == "r1"


class TestWorkerProtocol:
    def test_async_new_contract_matches_protocol(self) -> None:
        async def execute(ctx: SkillContext, params: dict) -> SkillResult:
            return SkillResult.ok({"echo": params})

        # runtime_checkable Protocols only verify `__call__` exists; the
        # structural check here is that a round-trip invocation returns a
        # SkillResult, which is really what downstream cares about.
        assert callable(execute)
        result = asyncio.run(execute(SkillContext(tenant_id="t"), {"k": 1}))
        assert isinstance(result, SkillResult)
        assert result.data == {"echo": {"k": 1}}

    def test_runtime_checkable_isinstance(self) -> None:
        async def execute(ctx: SkillContext, params: dict) -> SkillResult:
            return SkillResult.ok()

        assert isinstance(execute, Worker)
