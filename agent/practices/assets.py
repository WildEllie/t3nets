"""
Practice asset/page resolution.

Helpers that read tenant settings to determine which practice pages are
visible in the dashboard nav and resolve a (practice, slug) pair to a
filesystem path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.models.practice import PracticeDefinition


def get_pages_for_tenant(
    practices: dict[str, PracticeDefinition],
    tenant: Any,
) -> list[dict[str, Any]]:
    """
    Get pages available to a tenant based on their practice configuration.
    Returns list of dicts with {slug, title, nav_label, url, practice}.
    """
    pages: list[dict[str, Any]] = []
    settings = tenant.settings

    if settings.primary_practice:
        practice = practices.get(settings.primary_practice)
        if practice:
            for page in practice.pages:
                if page.nav_order > 0 and page.nav_label:
                    pages.append(
                        {
                            "slug": page.slug,
                            "title": page.title,
                            "nav_label": page.nav_label,
                            "nav_order": page.nav_order,
                            "url": f"/p/{practice.name}/{page.slug}",
                            "practice": practice.name,
                        }
                    )

    for addon in getattr(settings, "addon_pages", []):
        parts = addon.split("/", 1)
        if len(parts) != 2:
            continue
        practice_name, page_slug = parts
        practice = practices.get(practice_name)
        if not practice:
            continue
        for page in practice.pages:
            if page.slug == page_slug and page.nav_label:
                pages.append(
                    {
                        "slug": page.slug,
                        "title": page.title,
                        "nav_label": page.nav_label,
                        "nav_order": page.nav_order,
                        "url": f"/p/{practice_name}/{page.slug}",
                        "practice": practice_name,
                    }
                )

    pages.sort(key=lambda p: p["nav_order"])
    return pages


def get_page_path(
    practices: dict[str, PracticeDefinition],
    practice_name: str,
    page_slug: str,
) -> Path | None:
    """
    Resolve a practice page to its filesystem path.
    Returns None if not found.
    """
    practice = practices.get(practice_name)
    if not practice:
        return None

    for page in practice.pages:
        if page.slug == page_slug:
            return Path(practice.base_path) / page.file

    return None
