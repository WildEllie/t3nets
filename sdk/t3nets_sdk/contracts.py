"""
Skill worker contracts: `SkillContext`, `SkillResult`, `Worker`.

These are the types a practice author codes against when writing a skill
worker. They replace the historical untyped contract
`execute(params: dict, secrets: dict) -> dict` with a typed, async-first
surface that exposes the runtime bits a worker actually needs (secrets,
logger, tenant id, blob store) without dragging in any cloud SDKs.

Kept as stdlib dataclasses — pydantic is reserved for install-time manifest
parsing so the hot path (router → worker) stays fast and dependency-light.

Naming note: `SkillResult.success` is the boolean field on the instance;
`SkillResult.ok(...)` and `SkillResult.fail(...)` are the classmethod
constructors. Using two names avoids the Python-level collision that a
single `ok` identifier would create between an instance attribute and a
classmethod.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from t3nets_sdk.interfaces import BlobStore


@dataclass
class SkillContext:
    """Runtime handles passed to every skill worker.

    Attributes:
        tenant_id: ID of the tenant this invocation belongs to. Workers
            that talk to tenant-scoped stores must pass it through.
        secrets: Resolved secret bundle for the skill's required
            integration (e.g. the Jira `{url, token}` dict). Empty dict
            when the skill has no `requires_integration`.
        logger: A stdlib logger scoped to the skill. Practice authors
            should use this instead of creating their own.
        blob_store: Scoped blob store handle for persisting artifacts
            (audio, screenshots, exported files). `None` when the host
            hasn't wired one up (e.g. some unit tests).
        extras: Freeform dict for host-specific extensions. Practice
            authors should not rely on specific keys — use `secrets`,
            `blob_store`, and `tenant_id` for anything portable.
    """

    tenant_id: str
    secrets: dict[str, Any] = field(default_factory=dict)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("t3nets.skill"))
    blob_store: Optional[BlobStore] = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillResult:
    """Typed result envelope returned by a skill worker.

    Prefer constructing via `SkillResult.ok(...)` or `SkillResult.fail(...)`
    rather than the raw constructor — they keep the `success` flag and the
    `error` field in sync.
    """

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @classmethod
    def ok(cls, data: Optional[dict[str, Any]] = None, **kwargs: Any) -> "SkillResult":
        """Build a successful result. `data` and `**kwargs` are merged,
        with kwargs taking precedence, so both styles work:

            SkillResult.ok({"sprint": sprint})
            SkillResult.ok(sprint=sprint, issues=issues)
        """
        merged: dict[str, Any] = {}
        if data:
            merged.update(data)
        merged.update(kwargs)
        return cls(success=True, data=merged, error=None)

    @classmethod
    def fail(cls, error: str, **data: Any) -> "SkillResult":
        """Build a failed result. `error` is the user-facing message; any
        `**data` is attached as structured context."""
        return cls(success=False, data=dict(data), error=error)

    def to_dict(self) -> dict[str, Any]:
        """Flatten to a JSON-serializable dict for the legacy transport
        layer. Failed results surface `error` alongside any structured
        `data` the worker attached."""
        if self.success:
            return dict(self.data)
        out: dict[str, Any] = {"error": self.error or "skill failed"}
        out.update(self.data)
        return out

    def __bool__(self) -> bool:
        return self.success


@runtime_checkable
class Worker(Protocol):
    """Structural type for a skill worker callable.

    Practice authors write `async def execute(ctx, params) -> SkillResult`
    and don't usually need to reference this directly — it exists so the
    registry can type-check wrappers and so tests can assert against it.
    """

    async def __call__(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult: ...


__all__ = [
    "SkillContext",
    "SkillResult",
    "Worker",
]
