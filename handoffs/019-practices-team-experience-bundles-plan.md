# Handoff: 019 — Practices: Team Experience Bundles (Plan Only)

**Date:** 2026-03-03
**Status:** Plan complete, not yet implemented

---

## What Was Done

Designed an expanded Practices architecture to replace the simple "skill bundles" concept in Phase 8 of the roadmap. A Practice is now a **complete team experience** that bundles skills, custom console pages, and functionality into an installable, uploadable package.

**Key deliverables:**
- Full architecture plan: `docs/plan-practices-team-experience-bundles.md`
- Roadmap updated: Phase 8 expanded from simple skill bundles to full team experience bundles (8a/8b/8c)

---

## The Problem

The original Phase 8 planned Practices as simple skill bundles — a list of skill names assigned to a tenant. The architect's master plan is richer: teams need not just skills but also custom dashboard pages and tools bundled together as a cohesive workspace experience.

## The Solution

A Practice is a ZIP-distributable package containing:
- **Skills**: skill.yaml + worker.py (same as today)
- **Pages**: Self-contained HTML dashboards and interactive tools
- **Manifest**: `practice.yaml` defining the bundle, required integrations, nav structure

**Examples:**
- Engineering Practice: skills (sprint_status, release_notes), pages (sprint board, release dashboard)
- Sales Practice: skills (deal_status, crm_lookup), pages (pipeline dashboard)

### Key Design Decisions

| Decision | Choice |
|----------|--------|
| Activation model | One primary practice + add-on skills/pages from other practices |
| Page types | Both read-only dashboards and interactive tools, per page |
| Data source | Shared integrations only (same credentials as skills) |
| Page backend | Through skills — pages call `/api/skill/{name}` asynchronously (same EventBridge → Lambda → WebSocket/SSE flow as chat) |
| URL structure | Namespaced: `/p/{practice}/{page}` |
| Distribution | Uploadable ZIP from day one; first practice is built-in |

### Architecture Highlights

- Pages interact with data **through skills** — keeping skills as the single abstraction for data access
- Browser loads HTML from CDN (S3 + CloudFront), JS makes async API calls for data
- `POST /api/skill/{name}` returns 202 + request_id, result delivered via WebSocket/SSE
- `enabled_skills` remains the source of truth for the router — no router changes needed
- Dynamic nav injection: `/api/practices/pages` returns pages for tenant, injected into all page navbars

---

## Files Changed (This Handoff)

| File | Change |
|------|--------|
| `docs/ROADMAP.md` | Phase 8 expanded from simple skill bundles to team experience bundles (8a/8b/8c) |
| `docs/plan-practices-team-experience-bundles.md` | Full architecture plan (new file) |

---

## Files to Create/Modify (Implementation)

| File | Action | Purpose |
|------|--------|---------|
| `agent/models/practice.py` | Create | PracticePage, PracticeDefinition dataclasses |
| `agent/practices/registry.py` | Create | PracticeRegistry — load, install, query practices |
| `agent/practices/engineering/practice.yaml` | Create | Built-in engineering practice manifest |
| `agent/practices/engineering/skills/` | Move | Move existing skills from `agent/skills/` |
| `agent/practices/engineering/pages/sprint.html` | Create | Sprint board page |
| `agent/practices/engineering/pages/releases.html` | Create | Release dashboard page |
| `agent/models/tenant.py` | Modify | Add primary_practice, addon_skills, addon_pages |
| `adapters/local/dev_server.py` | Modify | Add practice routes, `/api/skill/{name}`, startup changes |
| `adapters/aws/server.py` | Modify | Mirror practice routes, S3 loading |
| `adapters/local/settings.html` | Modify | Add Practices tab |
| All HTML files | Modify | Add practice nav injection |
| `agent/skills/registry.py` | Modify | Support loading from practice subdirectories |
| `scripts/deploy.sh` | Modify | Sync practice pages to S3 |
| `infra/aws/modules/cdn/main.tf` | Modify | Add `/p/*` cache behavior |

---

## Next Steps

1. **Implement Phase 8a** — Core Practice Framework (models, registry, skill migration, API endpoints, page serving, nav injection)
2. **Implement Phase 8b** — Practice Upload & Management (ZIP upload/validation, settings UI, persistence)
3. **Implement Phase 8c** — AWS Deployment (S3 sync, CloudFront behaviors, uploaded practice storage)

See `docs/plan-practices-team-experience-bundles.md` for full implementation details.
