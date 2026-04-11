"""Shared practice, skill-invoke, and callback handlers.

Extracted from adapters/aws/server.py and adapters/local/dev_server.py.
Uses only agent/ interfaces — no cloud-specific imports.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Coroutine, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from t3nets_sdk.contracts import SkillContext

from agent.interfaces.blob_store import BlobStore
from agent.interfaces.secrets_provider import SecretsProvider
from agent.interfaces.tenant_store import TenantStore
from agent.practices.registry import PracticeRegistry
from agent.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "default"


# ---------------------------------------------------------------------------
# Protocols for pending-request stores (AWS and local have different impls)
# ---------------------------------------------------------------------------


class PendingEntry(Protocol):
    """Minimal read interface for a pending request — works with both
    the AWS ``PendingRequest`` dataclass and the local dict-based store.
    Consumers access fields via ``__getitem__`` *or* attribute access,
    so we expose a mapping-style protocol."""


class PendingRequestStore(Protocol):
    """Abstract pending-request store accepted by *handle_callback*.

    Both ``PendingRequestsStore`` (AWS/DynamoDB) and ``LocalPendingStore``
    satisfy this protocol.
    """

    def get(self, request_id: str) -> Any:  # noqa: ANN401
        """Return the pending entry or ``None``."""
        ...

    def mark_completed(self, request_id: str) -> Any:  # noqa: ANN401
        """Transition a request to *completed*.  Return value is ignored."""
        ...


# Type alias for the optional async post-install hook supplied by each server
PostInstallHook = Callable[
    [Any, str],  # (practice, tenant_id)
    Coroutine[Any, Any, None],
]

# Type alias for a callback delivery function supplied by each server
CallbackDeliveryFn = Callable[
    [dict[str, Any], Any],  # (event_data, pending_entry)
    Coroutine[Any, Any, None],
]


class PracticeHandlers:
    """Shared handlers for practice management, skill invocation, and callbacks.

    Parameters
    ----------
    practices:
        The loaded ``PracticeRegistry``.
    skills:
        The loaded ``SkillRegistry``.
    blobs:
        ``BlobStore`` implementation for the current environment.
    tenants:
        ``TenantStore`` implementation for the current environment.
    secrets:
        ``SecretsProvider`` for fetching integration credentials.
    pending_store:
        Optional pending-request store (``None`` disables callback endpoint).
    post_install_hook:
        Optional async callable ``(practice, tenant_id) -> None`` invoked
        after a practice ZIP is installed.  AWS uses this to deploy Lambdas
        and rebuild rules in the background; local dev can omit it.
    callback_delivery:
        Optional async callable ``(event_data, pending_entry) -> None``
        responsible for delivering the callback result to the user (e.g.
        via SSE).  If ``None``, the callback endpoint just acknowledges.
    """

    def __init__(
        self,
        practices: PracticeRegistry,
        skills: SkillRegistry,
        blobs: BlobStore,
        tenants: TenantStore,
        secrets: SecretsProvider,
        pending_store: PendingRequestStore | None = None,
        post_install_hook: PostInstallHook | None = None,
        callback_delivery: CallbackDeliveryFn | None = None,
    ) -> None:
        self._practices = practices
        self._skills = skills
        self._blobs = blobs
        self._tenants = tenants
        self._secrets = secrets
        self._pending_store = pending_store
        self._post_install_hook = post_install_hook
        self._callback_delivery = callback_delivery

    # ------------------------------------------------------------------
    # GET /api/practices
    # ------------------------------------------------------------------

    async def list_practices(self, request: Request, tenant_id: str) -> Response:
        """Return metadata for all installed practices."""
        result = []
        for p in self._practices.list_all():
            result.append(
                {
                    "name": p.name,
                    "display_name": p.display_name,
                    "description": p.description,
                    "version": p.version,
                    "icon": p.icon,
                    "built_in": p.built_in,
                    "skills": p.skills,
                    "pages": [
                        {
                            "slug": pg.slug,
                            "title": pg.title,
                            "nav_label": pg.nav_label,
                        }
                        for pg in p.pages
                    ],
                }
            )
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # GET /api/practices/pages
    # ------------------------------------------------------------------

    async def list_practice_pages(self, request: Request, tenant_id: str) -> Response:
        """Return pages available to the given tenant."""
        try:
            tenant = await self._tenants.get_tenant(tenant_id)
            pages = self._practices.get_pages_for_tenant(tenant)
            return JSONResponse(pages)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ------------------------------------------------------------------
    # POST /api/practices/upload
    # ------------------------------------------------------------------

    async def upload_practice(self, request: Request, tenant_id: str) -> Response:
        """Upload and install a practice ZIP archive."""
        try:
            body = await request.body()
            data_dir = Path("data")

            # Retrieve installed versions for upgrade-check
            tenant = await self._tenants.get_tenant(tenant_id)
            installed_versions = tenant.settings.installed_practices

            practice = await self._practices.install_zip(
                body,
                data_dir,
                blob_store=self._blobs,
                tenant_id=tenant_id,
                installed_versions=installed_versions,
            )
            self._practices.register_skills(self._skills)

            # Persist version to tenant settings
            tenant.settings.installed_practices[practice.name] = practice.version
            await self._tenants.update_tenant(tenant)

            # Run environment-specific post-install tasks (Lambda deploy, etc.)
            if self._post_install_hook is not None:
                # Schedule as background task so the HTTP response returns fast
                asyncio.ensure_future(self._post_install_hook(practice, tenant_id))

            return JSONResponse(
                {
                    "ok": True,
                    "name": practice.name,
                    "version": practice.version,
                    "skills": practice.skills,
                }
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.error(f"Practice upload failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ------------------------------------------------------------------
    # POST /api/skill/{name}
    # ------------------------------------------------------------------

    async def invoke_skill(self, request: Request, tenant_id: str) -> Response:
        """Invoke a skill synchronously (used by practice pages)."""
        skill_name = request.path_params["name"]
        try:
            body = await request.json()
            worker_fn = self._skills.get_worker(skill_name)

            # Fetch secrets when the skill requires an integration
            skill = self._skills.get_skill(skill_name)
            skill_secrets: dict[str, Any] = {}
            if skill and skill.requires_integration:
                try:
                    skill_secrets = await self._secrets.get(tenant_id, skill.requires_integration)
                except Exception:
                    pass

            skill_ctx = SkillContext(
                tenant_id=tenant_id,
                secrets=skill_secrets,
                logger=logging.getLogger(f"t3nets.skill.{skill_name}"),
                blob_store=self._blobs,
            )
            skill_result = await worker_fn(skill_ctx, body)
            return JSONResponse(skill_result.to_dict())
        except Exception as e:
            logger.error(f"Skill invoke failed ({skill_name}): {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ------------------------------------------------------------------
    # POST /api/callback/{request_id}
    # ------------------------------------------------------------------

    async def handle_callback(self, request: Request, tenant_id: str) -> Response:
        """Receive an async skill result from an external service.

        The local server performs UUID validation, checks completion status,
        marks the request completed, builds an SSE event, and delivers it
        via *callback_delivery*.  The AWS server delegates heavy delivery
        to a separate result-router Lambda, so the behaviour here is a
        superset that covers both.
        """
        request_id = request.path_params["request_id"]

        if self._pending_store is None:
            return JSONResponse({"error": "Async skills not enabled"}, status_code=501)

        # Validate UUID4 format
        try:
            uuid.UUID(request_id, version=4)
        except ValueError:
            return JSONResponse({"error": "Invalid request_id format"}, status_code=400)

        # Look up the pending request
        pending = self._pending_store.get(request_id)
        if asyncio.iscoroutine(pending):
            pending = await pending

        if not pending:
            return JSONResponse({"error": "Request not found or expired"}, status_code=404)

        # Check if already completed (dict-style for local, attr for AWS)
        status = (
            pending.get("status") if isinstance(pending, dict) else getattr(pending, "status", None)
        )
        if status == "completed":
            return JSONResponse({"error": "Already completed"}, status_code=409)

        try:
            body = await request.json()
        except Exception as e:
            logger.error(f"Callback failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        # Mark completed
        result = self._pending_store.mark_completed(request_id)
        if asyncio.iscoroutine(result):
            await result

        # Build SSE event payload
        skill_name = (
            pending.get("skill_name", "")
            if isinstance(pending, dict)
            else getattr(pending, "skill_name", "")
        )
        event_data: dict[str, Any] = {
            "request_id": request_id,
            "text": body.get("text", ""),
            "raw": False,
            "skill": skill_name,
            "route": "callback",
        }

        # Include audio payload if present
        if body.get("audio_b64"):
            event_data["audio"] = {
                "audio_b64": body["audio_b64"],
                "format": body.get("format", "wav"),
            }

        # Deliver to user via environment-specific mechanism (SSE, etc.)
        if self._callback_delivery is not None:
            try:
                await self._callback_delivery(event_data, pending)
            except Exception as exc:
                logger.error(f"Callback delivery failed: {exc}")

        logger.info(f"Callback delivered for request {request_id[:8]}")
        return JSONResponse({"ok": True})
