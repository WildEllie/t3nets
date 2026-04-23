"""Release Notes worker — new SDK contract.

Returns structured data plus a `render_prompt` so the router's AI
formatter produces rich markdown (headings, tables, contributor call-outs).
In `--raw` mode the router skips the formatter entirely.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from typing import Any

from t3nets_sdk.contracts import SkillContext, SkillResult

_RENDER_PROMPTS: dict[str, str] = {
    "list_releases": (
        "Format this list of project releases for the user. Lead with the project "
        "key and a one-line summary (total versions, released vs unreleased). "
        "Then render two markdown sections — **Unreleased** first (sorted by name, "
        "include planned release date if present), **Released** second (sorted by "
        "release date descending). Use bullets `name · date`. Cap each section at "
        "20 entries and note the remainder."
    ),
    "summarize": (
        "Produce a release-notes-style summary for this release. Lead with a "
        "heading `### <release name>` plus release/unreleased status, date, and "
        "total ticket/point counts. Then render: a progress line by status; a "
        "breakdown by issue type; top contributors (up to 5); and a per-type "
        "section listing tickets as `KEY — summary · status · (assignee)`. If the "
        "`not_started` flag is set, skip the breakdown and just show the message. "
        "Use markdown headings (### / ####), bold labels, and bullet lists."
    ),
}


async def execute(ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
    """Dispatch on `action` and return a SkillResult with a render_prompt."""
    required = ["url", "email", "api_token"]
    missing = [k for k in required if not ctx.secrets.get(k)]
    if missing:
        return SkillResult.fail(f"Missing Jira credentials: {', '.join(missing)}")

    action = params.get("action", "list_releases")

    try:
        if action == "list_releases":
            data = _list_releases(ctx.secrets)
        elif action == "summarize":
            release_name = params.get("release_name", "")
            if not release_name:
                return SkillResult.fail("release_name is required for 'summarize' action")
            data = _summarize_release(ctx.secrets, release_name)
        else:
            return SkillResult.fail(f"Unknown action: {action}")
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        ctx.logger.exception("release_notes HTTP error")
        return SkillResult.fail(f"Jira API error ({e.code}): {body[:500]}")
    except Exception as e:
        ctx.logger.exception("release_notes failed")
        return SkillResult.fail(f"Jira API error: {e}")

    if "error" in data:
        return SkillResult.fail(
            str(data["error"]), **{k: v for k, v in data.items() if k != "error"}
        )

    render_prompt = None if ctx.raw else _RENDER_PROMPTS.get(action)
    return SkillResult.ok(data, render_prompt=render_prompt)


# --- Jira API helpers --------------------------------------------------------


def _make_headers(secrets: dict[str, Any]) -> dict[str, str]:
    creds = base64.b64encode(f"{secrets['email']}:{secrets['api_token']}".encode()).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _jira_rest_request(secrets: dict[str, Any], endpoint: str) -> Any:
    url = f"{secrets['url'].rstrip('/')}/rest/api/3/{endpoint}"
    req = urllib.request.Request(url, headers=_make_headers(secrets))
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode())


def _jira_search(
    secrets: dict[str, Any], jql: str, fields: list[str], max_per_page: int = 100
) -> list[dict[str, Any]]:
    all_issues: list[dict[str, Any]] = []
    next_page_token: str | None = None

    while True:
        query: dict[str, str | int] = {
            "jql": jql,
            "maxResults": max_per_page,
            "fields": ",".join(fields),
        }
        if next_page_token:
            query["nextPageToken"] = next_page_token

        params = urllib.parse.urlencode(query)
        data = _jira_rest_request(secrets, f"search/jql?{params}")
        issues = data.get("issues", [])
        all_issues.extend(issues)

        next_page_token = data.get("nextPageToken")
        if not next_page_token or data.get("isLast", False) or not issues:
            break

    return all_issues


# --- Actions -----------------------------------------------------------------


def _list_releases(secrets: dict[str, Any]) -> dict[str, Any]:
    project_key = secrets.get("project_key", "") or _get_project_key_from_board(secrets)

    if not project_key:
        return {
            "error": (
                "Could not determine project key. "
                "Add 'project_key' to your Jira integration settings."
            )
        }

    data = _jira_rest_request(secrets, f"project/{project_key}/versions")

    released = []
    unreleased = []

    for version in data:
        entry = {
            "name": version.get("name", ""),
            "id": version.get("id", ""),
            "description": version.get("description", ""),
            "released": version.get("released", False),
            "release_date": version.get("releaseDate", ""),
            "start_date": version.get("startDate", ""),
            "archived": version.get("archived", False),
        }
        if version.get("released"):
            released.append(entry)
        else:
            unreleased.append(entry)

    released.sort(key=lambda v: v.get("release_date", ""), reverse=True)
    unreleased.sort(key=lambda v: v.get("name", ""))

    return {
        "project": project_key,
        "total_versions": len(data),
        "released": released,
        "unreleased": unreleased,
    }


def _summarize_release(secrets: dict[str, Any], release_name: str) -> dict[str, Any]:
    project_key = secrets.get("project_key", "") or _get_project_key_from_board(secrets)

    version_info = _get_version_info(secrets, project_key, release_name)
    if not version_info:
        return {
            "release": release_name,
            "total_issues": 0,
            "error": f"Release '{release_name}' was not found in project '{project_key}'.",
        }

    jql = f'fixVersion = "{release_name}"'
    if project_key:
        jql = f'project = "{project_key}" AND {jql}'
    jql += " ORDER BY issuetype ASC, priority ASC, key ASC"

    fields = [
        "summary",
        "status",
        "issuetype",
        "priority",
        "assignee",
        "reporter",
        "created",
        "updated",
        "resolutiondate",
        "resolution",
        "labels",
        "components",
        "fixVersions",
        "customfield_10016",
    ]

    raw_issues = _jira_search(secrets, jql, fields)

    if not raw_issues:
        if not version_info.get("released", False):
            return {
                "release": release_name,
                "version_info": version_info,
                "total_issues": 0,
                "not_started": True,
                "message": (
                    f"Release '{release_name}' exists but has no issues assigned to it. "
                    f"Work has not started on this release yet — there is nothing to summarize."
                ),
            }
        return {
            "release": release_name,
            "version_info": version_info,
            "total_issues": 0,
            "error": f"No issues found for release '{release_name}'.",
        }

    if not version_info.get("released", False):
        status_names = [
            (issue.get("fields", {}).get("status") or {}).get("name", "") for issue in raw_issues
        ]
        work_statuses = {"In Progress", "In Review", "Done", "Closed", "Resolved"}
        has_work = any(s in work_statuses for s in status_names)
        if not has_work:
            return {
                "release": release_name,
                "version_info": version_info,
                "total_issues": len(raw_issues),
                "not_started": True,
                "message": (
                    f"Release '{release_name}' has {len(raw_issues)} issue(s) assigned "
                    f"but none have started yet. There is no work to summarize."
                ),
            }

    issues = [_extract_issue(secrets, issue) for issue in raw_issues]

    by_type: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        issue_type = issue["issue_type"]
        by_type.setdefault(issue_type, []).append(issue)

    statuses: dict[str, int] = {}
    priorities: dict[str, int] = {}
    assignees: dict[str, int] = {}
    total_points = 0

    for issue in issues:
        statuses[issue["status"]] = statuses.get(issue["status"], 0) + 1
        priorities[issue["priority"]] = priorities.get(issue["priority"], 0) + 1
        assignees[issue["assignee"]] = assignees.get(issue["assignee"], 0) + 1
        total_points += issue.get("story_points") or 0

    return {
        "release": release_name,
        "version_info": version_info,
        "total_issues": len(issues),
        "total_story_points": total_points,
        "summary": {
            "by_status": statuses,
            "by_priority": priorities,
            "by_type": {t: len(items) for t, items in by_type.items()},
            "contributors": assignees,
        },
        "issues_by_type": by_type,
    }


# --- Helpers -----------------------------------------------------------------


def _extract_issue(secrets: dict[str, Any], issue: dict[str, Any]) -> dict[str, Any]:
    f = issue.get("fields", {})
    base_url = secrets["url"].rstrip("/")
    key = issue.get("key", "")

    return {
        "key": key,
        "summary": f.get("summary", ""),
        "status": (f.get("status") or {}).get("name", ""),
        "issue_type": (f.get("issuetype") or {}).get("name", ""),
        "priority": (f.get("priority") or {}).get("name", ""),
        "assignee": (f.get("assignee") or {}).get("displayName", "Unassigned"),
        "reporter": (f.get("reporter") or {}).get("displayName", ""),
        "resolution": (f.get("resolution") or {}).get("name", "Unresolved"),
        "resolved_date": f.get("resolutiondate", ""),
        "labels": f.get("labels", []),
        "components": [c.get("name", "") for c in (f.get("components") or [])],
        "story_points": f.get("customfield_10016"),
        "url": f"{base_url}/browse/{key}",
    }


def _get_version_info(
    secrets: dict[str, Any], project_key: str, release_name: str
) -> dict[str, Any]:
    if not project_key:
        return {}

    try:
        versions = _jira_rest_request(secrets, f"project/{project_key}/versions")
        for v in versions:
            if v.get("name") == release_name:
                return {
                    "name": v.get("name", ""),
                    "description": v.get("description", ""),
                    "released": v.get("released", False),
                    "release_date": v.get("releaseDate", ""),
                    "start_date": v.get("startDate", ""),
                }
    except Exception:
        pass

    return {}


def _get_project_key_from_board(secrets: dict[str, Any]) -> str:
    board_id = secrets.get("board_id", "")
    if not board_id:
        return ""

    try:
        url = f"{secrets['url'].rstrip('/')}/rest/agile/1.0/board/{board_id}/configuration"
        req = urllib.request.Request(url, headers=_make_headers(secrets))
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())

        filter_id = data.get("filter", {}).get("id", "")
        if filter_id:
            filter_data = _jira_rest_request(secrets, f"filter/{filter_id}")
            jql = str(filter_data.get("jql", ""))
            if "project" in jql.lower():
                parts = jql.split()
                for i, part in enumerate(parts):
                    if part.lower() == "project" and i + 2 < len(parts):
                        key = parts[i + 2].strip('"').strip("'")
                        if key and key.isalpha():
                            return key

        location = data.get("location", {})
        project_key = str(location.get("projectKey", ""))
        if project_key:
            return project_key

    except Exception:
        pass

    return ""
