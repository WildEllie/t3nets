"""Sprint Status worker — new SDK contract.

Returns structured data and a `render_prompt` that steers the router's
AI formatter to produce a rich status report with tables / section
headers / blockers callouts. In `--raw` mode the router bypasses the
formatter and the user sees the JSON payload directly.
"""

from __future__ import annotations

import base64
import json
import urllib.request
from datetime import datetime
from typing import Any, cast

from t3nets_sdk.contracts import SkillContext, SkillResult

_RENDER_PROMPTS: dict[str, str] = {
    "status": (
        "Format this sprint report for a standup audience. Lead with the sprint "
        "name, end date (with days remaining), and progress line (tickets done / "
        "story points done). Then render, in this order and only if non-empty: "
        "(1) a Blockers section listing each blocked ticket as `KEY — summary "
        "(assignee)`; (2) a Large unstarted section (≥5 pts); (3) an In progress "
        "section. Use markdown headings (###), bold labels, and bullet lists. "
        "Call out risks — flag an overdue sprint, stalled tickets, or unassigned "
        "story points. Keep it skimmable."
    ),
    "blockers": (
        "List the blockers in the active sprint as a markdown section. If there "
        "are none, say so cheerfully in one line. Otherwise use bullets "
        "`KEY — summary · status · (assignee)`."
    ),
    "mine": (
        "Format the user's assigned sprint tickets. Lead with a short summary "
        "line (count, sprint name), then bullet each ticket "
        "`KEY — summary · status · points`. Put flagged/blocked tickets first."
    ),
}


async def execute(ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
    """Dispatch on `action` and return a SkillResult with a render_prompt."""
    required = ["url", "email", "api_token", "board_id"]
    missing = [k for k in required if not ctx.secrets.get(k)]
    if missing:
        return SkillResult.fail(f"Missing Jira credentials: {', '.join(missing)}")

    action = params.get("action", "status")

    try:
        if action == "status":
            data = _get_status(ctx.secrets)
        elif action == "blockers":
            data = _get_blockers(ctx.secrets)
        elif action == "mine":
            email = params.get("assignee_email", "")
            if not email:
                return SkillResult.fail("assignee_email required for 'mine' action")
            data = _get_mine(ctx.secrets, email)
        else:
            return SkillResult.fail(f"Unknown action: {action}")
    except Exception as e:
        ctx.logger.exception("sprint_status failed")
        return SkillResult.fail(f"Jira API error: {e}")

    if "error" in data:
        return SkillResult.fail(str(data["error"]))

    render_prompt = None if ctx.raw else _RENDER_PROMPTS.get(action)
    return SkillResult.ok(data, render_prompt=render_prompt)


# --- Jira HTTP ---------------------------------------------------------------


def _jira_request(secrets: dict[str, Any], endpoint: str) -> dict[str, Any]:
    url = f"{secrets['url'].rstrip('/')}/rest/agile/1.0/{endpoint}"
    credentials = base64.b64encode(f"{secrets['email']}:{secrets['api_token']}".encode()).decode()

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(req) as response:
        return cast(dict[str, Any], json.loads(response.read().decode()))


def _get_active_sprint(secrets: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    data = _jira_request(secrets, f"board/{secrets['board_id']}/sprint?state=active")
    sprints = data.get("values", [])
    if not sprints:
        return None, "No active sprint found"
    return sprints[0], None


def _get_sprint_issues(secrets: dict[str, Any], sprint_id: int) -> list[dict[str, Any]]:
    issues = []
    start_at = 0
    max_results = 50

    while True:
        data = _jira_request(
            secrets,
            f"sprint/{sprint_id}/issue?startAt={start_at}&maxResults={max_results}"
            f"&fields=summary,status,assignee,priority,customfield_10016,labels",
        )
        issues.extend(data.get("issues", []))
        if start_at + max_results >= data.get("total", 0):
            break
        start_at += max_results

    return issues


def _parse_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parsed = []
    for issue in issues:
        fields = issue.get("fields", {})
        assignee = fields.get("assignee")
        story_points = fields.get("customfield_10016") or 0

        parsed.append(
            {
                "key": issue.get("key", ""),
                "summary": fields.get("summary", ""),
                "status": fields.get("status", {}).get("name", "Unknown"),
                "status_category": fields.get("status", {})
                .get("statusCategory", {})
                .get("name", "Unknown"),
                "assignee": assignee.get("displayName", "Unassigned") if assignee else "Unassigned",
                "assignee_email": assignee.get("emailAddress", "") if assignee else "",
                "priority": fields.get("priority", {}).get("name", "Medium"),
                "story_points": story_points,
                "flagged": "impediment" in [lbl.lower() for lbl in fields.get("labels", [])],
            }
        )
    return parsed


def _build_report(sprint: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    today = datetime.now()
    end_date = sprint.get("endDate", "")[:10]

    days_remaining = None
    if end_date:
        try:
            end = datetime.strptime(end_date, "%Y-%m-%d")
            days_remaining = max(0, (end - today).days)
        except ValueError:
            pass

    categories: dict[str, list[dict[str, Any]]] = {"To Do": [], "In Progress": [], "Done": []}
    for issue in issues:
        cat = issue["status_category"]
        categories.setdefault(cat, []).append(issue)

    total = len(issues)
    done_count = len(categories.get("Done", []))
    total_points = sum(i["story_points"] or 0 for i in issues)
    done_points = sum(i["story_points"] or 0 for i in categories.get("Done", []))

    return {
        "sprint": {
            "name": sprint.get("name", "Unknown"),
            "goal": sprint.get("goal", ""),
            "start_date": sprint.get("startDate", "")[:10],
            "end_date": end_date,
            "days_remaining": days_remaining,
            "state": sprint.get("state", "active"),
        },
        "progress": {
            "total_tickets": total,
            "done": done_count,
            "in_progress": len(categories.get("In Progress", [])),
            "todo": len(categories.get("To Do", [])),
            "percent_done_tickets": round(done_count / total * 100) if total else 0,
            "total_story_points": total_points,
            "done_story_points": done_points,
            "percent_done_points": round(done_points / total_points * 100) if total_points else 0,
        },
        "blockers": [i for i in issues if i["flagged"]],
        "large_unstarted": [
            i for i in categories.get("To Do", []) if (i["story_points"] or 0) >= 5
        ],
        "tickets": {
            "done": categories.get("Done", []),
            "in_progress": categories.get("In Progress", []),
            "todo": categories.get("To Do", []),
        },
    }


# --- Actions -----------------------------------------------------------------


def _get_status(secrets: dict[str, Any]) -> dict[str, Any]:
    sprint, error = _get_active_sprint(secrets)
    if error or sprint is None:
        return {"error": error or "No active sprint found"}
    issues = _get_sprint_issues(secrets, sprint["id"])
    parsed = _parse_issues(issues)
    return _build_report(sprint, parsed)


def _get_blockers(secrets: dict[str, Any]) -> dict[str, Any]:
    sprint, error = _get_active_sprint(secrets)
    if error or sprint is None:
        return {"error": error or "No active sprint found"}
    issues = _get_sprint_issues(secrets, sprint["id"])
    parsed = _parse_issues(issues)
    return {
        "sprint": sprint.get("name"),
        "blockers": [i for i in parsed if i["flagged"]],
    }


def _get_mine(secrets: dict[str, Any], assignee_email: str) -> dict[str, Any]:
    sprint, error = _get_active_sprint(secrets)
    if error or sprint is None:
        return {"error": error or "No active sprint found"}
    issues = _get_sprint_issues(secrets, sprint["id"])
    parsed = _parse_issues(issues)
    return {
        "sprint": sprint.get("name"),
        "assignee": assignee_email,
        "tickets": [i for i in parsed if assignee_email.lower() in i["assignee_email"].lower()],
    }
