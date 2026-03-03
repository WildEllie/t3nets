# Practices: Team Experience Bundles (Skills + Pages + Functionality)

## Context

The current roadmap (Phase 8, formerly Phase 7) planned Practices as simple skill bundles — a list of skill names assigned to a tenant. The architect's master plan is much richer: a Practice is a **complete team experience** that bundles skills, custom console pages, and functionality into an installable, uploadable package. Think of it like a "theme" for a team's workspace:

- **Engineering Practice**: skills (sprint_status, release_notes), pages (sprint board, release dashboard)
- **Sales Practice**: skills (deal_status, crm_lookup), pages (pipeline dashboard)
- **HR Practice**: skills (leave_tracker), pages (leave calendar, team roster)

Pages interact with data **through skills** — the same execution path as chat. This keeps skills as the single abstraction for data access, whether triggered from a conversation or from a visual dashboard.

## Key Design Decisions

| Decision | Choice |
|----------|--------|
| Activation model | One primary practice + add-on skills/pages from other practices |
| Page types | Both read-only dashboards and interactive tools, per page |
| Data source | Shared integrations only (same credentials as skills) |
| Page backend | Through skills — pages call `/api/skill/{name}` asynchronously (same EventBridge → Lambda → WebSocket/SSE flow as chat) |
| URL structure | Namespaced: `/p/{practice}/{page}` |
| Distribution | Uploadable ZIP from day one; first practice is built-in |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                   PRACTICE ZIP                          │
│                                                         │
│  practice.yaml          ← manifest                      │
│  skills/                                                │
│    sprint_status/                                       │
│      skill.yaml         ← skill metadata + triggers     │
│      worker.py          ← skill execution logic         │
│    release_notes/                                       │
│      skill.yaml                                         │
│      worker.py                                          │
│  pages/                                                 │
│    sprint.html          ← self-contained dashboard page │
│    releases.html        ← self-contained dashboard page │
└─────────────────────────────────────────────────────────┘

Install flow:
  Upload ZIP → Validate → Extract skills + pages → Register → Available to tenants

Tenant activation:
  Admin selects primary practice → All its skills + pages become active
  Admin adds individual add-on skills/pages from other installed practices

Request flow (pages):
  Browser loads /p/engineering/sprint from CDN (S3 + CloudFront)
    → Static HTML + JS loads in browser
    → Page JS opens WebSocket/SSE connection (same as chat page)
    → Page JS calls POST /api/skill/sprint_status {action: "status"}
    → Server publishes skill invocation via EventBus (async)
    → Skill executes (DirectBus locally, Lambda on AWS)
    → Result delivered via WebSocket/SSE push
    → Page JS receives result and renders visual dashboard
```

---

## 1. Practice Manifest (`practice.yaml`)

```yaml
name: engineering
display_name: "Engineering"
description: "Sprint management, release tracking, and code review tools for engineering teams"
version: "1.0.0"
icon: "⚙️"

# Required integrations — tenant must have these connected
integrations:
  - jira

# Skills bundled in this practice
skills:
  - sprint_status
  - release_notes

# Console pages bundled in this practice
pages:
  - slug: sprint
    title: "Sprint Board"
    nav_label: "Sprint"
    nav_order: 1
    file: pages/sprint.html
    type: interactive          # "dashboard" (read-only) | "interactive"
    description: "Visual sprint progress with ticket board"
    requires_skills:           # Skills this page calls
      - sprint_status

  - slug: releases
    title: "Release Dashboard"
    nav_label: "Releases"
    nav_order: 2
    file: pages/releases.html
    type: dashboard
    description: "Release history and changelog viewer"
    requires_skills:
      - release_notes

# Optional system prompt addition when this practice is active
system_prompt_addon: |
  The user's team uses Jira for sprint management.
  When discussing work items, use Jira terminology (stories, epics, sprints).
```

---

## 2. Directory Structure

### Built-in practice (in the codebase)

```
agent/
  practices/
    engineering/
      practice.yaml
      skills/
        ping/
          skill.yaml
          worker.py
        sprint_status/
          skill.yaml
          worker.py
        release_notes/
          skill.yaml
          worker.py
      pages/
        sprint.html
        releases.html
```

**Migration**: Move existing skills from `agent/skills/` into `agent/practices/engineering/skills/`. The `agent/skills/` directory becomes empty (or removed). `SkillRegistry.load_from_directory()` is called on each practice's `skills/` subdirectory instead.

### Uploaded practices (runtime data)

```
data/practices/                          # Local dev
  sales/
    practice.yaml
    skills/
      deal_status/
        skill.yaml
        worker.py
    pages/
      pipeline.html

# AWS: stored in S3 under practices/ prefix
s3://t3nets-{stage}-static/practices/sales/practice.yaml
s3://t3nets-{stage}-static/practices/sales/skills/deal_status/...
s3://t3nets-{stage}-static/practices/sales/pages/pipeline.html
```

---

## 3. Data Models

### `agent/models/practice.py` (new)

```python
@dataclass
class PracticePage:
    slug: str                          # "sprint"
    title: str                         # "Sprint Board"
    nav_label: str                     # "Sprint"
    nav_order: int                     # 1
    file: str                          # "pages/sprint.html"
    page_type: str                     # "dashboard" | "interactive"
    description: str
    requires_skills: list[str] = field(default_factory=list)

@dataclass
class PracticeDefinition:
    name: str                          # "engineering"
    display_name: str                  # "Engineering"
    description: str
    version: str                       # "1.0.0"
    icon: str                          # "⚙️"
    integrations: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    pages: list[PracticePage] = field(default_factory=list)
    system_prompt_addon: str = ""
    built_in: bool = False             # True for codebase practices
    base_path: str = ""                # Filesystem path for serving pages
```

### TenantSettings changes (`agent/models/tenant.py`)

```python
@dataclass
class TenantSettings:
    # Existing
    enabled_skills: list[str] = field(default_factory=list)
    ai_model: str = ""
    # ...

    # New — practice configuration
    primary_practice: str = ""                    # "engineering"
    addon_skills: list[str] = field(default_factory=list)   # Individual skills from other practices
    addon_pages: list[str] = field(default_factory=list)    # "sales/pipeline" format
```

**How `enabled_skills` is computed**: When a tenant sets `primary_practice = "engineering"`, the system auto-populates `enabled_skills` with the practice's skill list (`["ping", "sprint_status", "release_notes"]`) plus any `addon_skills`. The admin can still toggle individual skills on/off from settings. The `enabled_skills` field remains the source of truth for the router — no router changes needed.

---

## 4. Practice Registry (`agent/practices/registry.py`)

See full plan file for PracticeRegistry class with `load_builtin()`, `load_uploaded()`, `install()`, `get_pages_for_tenant()`, and `get_page_path()` methods.

---

## 5. New API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/skill/{name}` | POST | Async skill invocation for pages (returns 202 + request_id, result via WebSocket/SSE) |
| `/api/practices/pages` | GET | Pages available to current tenant (for nav injection) |
| `/api/practices` | GET | List all installed practices |
| `/api/practices/upload` | POST | Upload a practice ZIP (admin only) |
| `/p/{practice}/{page}` | GET | Serve practice page (local: FileResponse; AWS: CDN) |

---

## 6. Dynamic Navigation

Pages inject nav links dynamically via `/api/practices/pages` — same pattern as the existing platform link injection in `checkAuth()`.

---

## 7. Upload Validation

ZIP validation: structure, manifest, name uniqueness, skill validity (skill.yaml + worker.py), worker safety (AST check), page validity, no path traversal, size limit, skill name uniqueness.

---

## 8. Implementation Phases

### Phase 8a — Core Practice Framework
- Practice data models, PracticeRegistry, built-in engineering practice
- Move existing skills into practice directory
- `/api/skill/{name}` async endpoint, `/api/practices/pages`, page serving
- Dynamic nav injection in all HTML pages
- **Milestone:** Built-in engineering practice works with pages at `/p/engineering/sprint`

### Phase 8b — Practice Upload & Management
- ZIP upload, validation, extraction
- Settings UI: Practices tab
- Practice persistence (DynamoDB/SQLite)
- **Milestone:** Admin can upload and activate practice ZIPs

### Phase 8c — AWS Deployment
- S3 sync for practice pages, CloudFront `/p/*` cache behavior
- Uploaded practices stored in S3
- **Milestone:** Practices work end-to-end on AWS
