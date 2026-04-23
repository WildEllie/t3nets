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
        raw: The user appended `--raw` to the request. Workers that
            render their own output should skip the rendering step — the
            router will JSON-dump the raw data for the user.
        extras: Freeform dict for host-specific extensions. Practice
            authors should not rely on specific keys — use `secrets`,
            `blob_store`, and `tenant_id` for anything portable.
    """

    tenant_id: str
    secrets: dict[str, Any] = field(default_factory=dict)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("t3nets.skill"))
    blob_store: Optional[BlobStore] = None
    raw: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


#: Reserved key used by `to_dict()` to carry the worker-rendered text
#: across the transport boundary (SQS, in-memory dict handoff). Renderers
#: pop it off before surfacing the payload; Tier 3 (Claude tool-use) also
#: strips it before feeding the data back to the model.
TEXT_KEY = "__t3nets_text__"

#: Reserved key for the worker-supplied formatting prompt. Used by the
#: router's AI formatter when the worker didn't render its own text.
RENDER_PROMPT_KEY = "__t3nets_render_prompt__"

META_KEYS = (TEXT_KEY, RENDER_PROMPT_KEY)


@dataclass
class SkillResult:
    """Typed result envelope returned by a skill worker.

    Prefer constructing via `SkillResult.ok(...)` or `SkillResult.fail(...)`
    rather than the raw constructor — they keep the `success` flag and the
    `error` field in sync.

    Rendering model — skills decide how their output reaches the user:

    * `text` set → the router sends the worker-rendered string verbatim
      (no Bedrock round-trip). Honor `ctx.raw` in the worker and skip
      rendering when it's true.
    * `text` None, `render_prompt` set → the router calls its AI
      formatter with the worker's prompt + the structured `data`. Use
      this when the skill wants to steer the formatting without pulling
      Bedrock into the skill itself.
    * Both None → the router falls back to a generic "format this
      clearly" prompt (legacy behavior, back-compat for older skills).
    """

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    text: Optional[str] = None
    render_prompt: Optional[str] = None

    @classmethod
    def ok(
        cls,
        data: Optional[dict[str, Any]] = None,
        *,
        text: Optional[str] = None,
        render_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> "SkillResult":
        """Build a successful result.

        `data` and `**kwargs` are merged (kwargs win) so both styles work:

            SkillResult.ok({"sprint": sprint})
            SkillResult.ok(sprint=sprint, issues=issues)

        Pass `text=` to render for the user directly, or `render_prompt=`
        to steer the router's AI formatter without invoking Bedrock in
        the skill itself.
        """
        merged: dict[str, Any] = {}
        if data:
            merged.update(data)
        merged.update(kwargs)
        return cls(
            success=True,
            data=merged,
            error=None,
            text=text,
            render_prompt=render_prompt,
        )

    @classmethod
    def fail(cls, error: str, **data: Any) -> "SkillResult":
        """Build a failed result. `error` is the user-facing message; any
        `**data` is attached as structured context."""
        return cls(success=False, data=dict(data), error=error)

    def to_dict(self) -> dict[str, Any]:
        """Flatten to a JSON-serializable dict for the transport layer.

        Failed results surface `error` alongside any structured `data`.
        `text` and `render_prompt`, when set, are attached under reserved
        dunder keys (`TEXT_KEY`, `RENDER_PROMPT_KEY`) so they survive the
        SQS / cross-process boundary. Consumers use the helpers
        `pop_render_meta()` / `strip_render_meta()` to pluck or remove
        them.
        """
        out: dict[str, Any] = dict(self.data) if self.success else {}
        if not self.success:
            out["error"] = self.error or "skill failed"
            out.update(self.data)
        if self.text is not None:
            out[TEXT_KEY] = self.text
        if self.render_prompt is not None:
            out[RENDER_PROMPT_KEY] = self.render_prompt
        return out

    def __bool__(self) -> bool:
        return self.success


def pop_render_meta(payload: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Extract and remove the rendering metadata from a transport dict.

    Returns `(text, render_prompt)` — either may be None. Mutates
    `payload` so callers are left with pure skill data suitable for
    downstream consumers (Claude tool-use, JSON dumps, etc.).
    """
    text = payload.pop(TEXT_KEY, None)
    render_prompt = payload.pop(RENDER_PROMPT_KEY, None)
    return text, render_prompt


def strip_render_meta(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `payload` with the rendering metadata removed.

    Use this when you need to inspect the pure skill data without
    mutating the original (e.g. feeding a tool result back to Claude).
    """
    return {k: v for k, v in payload.items() if k not in META_KEYS}


@runtime_checkable
class Worker(Protocol):
    """Structural type for a skill worker callable.

    Practice authors write `async def execute(ctx, params) -> SkillResult`
    and don't usually need to reference this directly — it exists so the
    registry can type-check wrappers and so tests can assert against it.
    """

    async def __call__(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult: ...


__all__ = [
    "META_KEYS",
    "RENDER_PROMPT_KEY",
    "SkillContext",
    "SkillResult",
    "TEXT_KEY",
    "Worker",
    "pop_render_meta",
    "strip_render_meta",
]
