"""
Sprint Status Worker

Follows the T3nets skill contract:
    def execute(params: dict, secrets: dict) -> dict

No cloud imports. No Lambda knowledge. Pure business logic.
Secrets are injected by the infrastructure layer.
"""

import json
import urllib.request
import base64
from datetime import datetime


def execute(params: dict, secrets: dict) -> dict:
    """
    Skill contract entry point.

    Args:
        params: {"action": "status|blockers|mine", "assignee_email": "..."}
        secrets: {"url": "...", "email": "...", "api_token": "...", "board_id": "..."}

    Returns:
        dict with sprint data or {"error": "..."}
    """
    # Validate secrets
    required = ["url", "email", "api_token", "board_id"]
    missing = [k for k in required if not secrets.get(k)]
    if missing:
        return {"error": f"Missing Jira credentials: {', '.join(missing)}"}

    action = params.get("action", "status")

    try:
        if action == "status":
            return _get_status(secrets)
        elif action == "blockers":
            return _get_blockers(secrets)
        elif action == "mine":
            email = params.get("assignee_email", "")
            if not email:
                return {"error": "assignee_email required for 'mine' action"}
            return _get_mine(secrets, email)
        else:
            return {"error": f"Unknown action: {action}"}
    except Exception as e:
        return {"error": f"Jira API error: {str(e)}"}


# --- Internal functions (same logic as your working local script) ---


def _jira_request(secrets: dict, endpoint: str) -> dict:
    """Make authenticated request to Jira Cloud REST API."""
    url = f"{secrets['url'].rstrip('/')}/rest/agile/1.0/{endpoint}"
    credentials = base64.b64encode(
        f"{secrets['email']}:{secrets['api_token']}".encode()
    ).decode()

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode())


def _get_active_sprint(secrets: dict):
    """Get currently active sprint for the board."""
    data = _jira_request(secrets, f"board/{secrets['board_id']}/sprint?state=active")
    sprints = data.get("values", [])
    if not sprints:
        return None, "No active sprint found"
    return sprints[0], None


def _get_sprint_issues(secrets: dict, sprint_id: int) -> list[dict]:
    """Get all issues in the sprint."""
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


def _parse_issues(issues: list[dict]) -> list[dict]:
    """Parse Jira issues into clean structure."""
    parsed = []
    for issue in issues:
        fields = issue.get("fields", {})
        assignee = fields.get("assignee")
        story_points = fields.get("customfield_10016") or 0

        parsed.append({
            "key": issue.get("key", ""),
            "summary": fields.get("summary", ""),
            "status": fields.get("status", {}).get("name", "Unknown"),
            "status_category": fields.get("status", {}).get("statusCategory", {}).get("name", "Unknown"),
            "assignee": assignee.get("displayName", "Unassigned") if assignee else "Unassigned",
            "assignee_email": assignee.get("emailAddress", "") if assignee else "",
            "priority": fields.get("priority", {}).get("name", "Medium"),
            "story_points": story_points,
            "flagged": "impediment" in [l.lower() for l in fields.get("labels", [])],
        })
    return parsed


def _build_report(sprint: dict, issues: list[dict]) -> dict:
    """Build status report for Claude to interpret."""
    today = datetime.now()
    end_date = sprint.get("endDate", "")[:10]

    days_remaining = None
    if end_date:
        try:
            end = datetime.strptime(end_date, "%Y-%m-%d")
            days_remaining = max(0, (end - today).days)
        except ValueError:
            pass

    categories = {"To Do": [], "In Progress": [], "Done": []}
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
            i for i in categories.get("To Do", [])
            if (i["story_points"] or 0) >= 5
        ],
        "tickets": {
            "done": categories.get("Done", []),
            "in_progress": categories.get("In Progress", []),
            "todo": categories.get("To Do", []),
        },
    }


def _get_status(secrets: dict) -> dict:
    sprint, error = _get_active_sprint(secrets)
    if error:
        return {"error": error}
    issues = _get_sprint_issues(secrets, sprint["id"])
    parsed = _parse_issues(issues)
    return _build_report(sprint, parsed)


def _get_blockers(secrets: dict) -> dict:
    sprint, error = _get_active_sprint(secrets)
    if error:
        return {"error": error}
    issues = _get_sprint_issues(secrets, sprint["id"])
    parsed = _parse_issues(issues)
    return {
        "sprint": sprint.get("name"),
        "blockers": [i for i in parsed if i["flagged"]],
    }


def _get_mine(secrets: dict, assignee_email: str) -> dict:
    sprint, error = _get_active_sprint(secrets)
    if error:
        return {"error": error}
    issues = _get_sprint_issues(secrets, sprint["id"])
    parsed = _parse_issues(issues)
    return {
        "sprint": sprint.get("name"),
        "assignee": assignee_email,
        "tickets": [
            i for i in parsed
            if assignee_email.lower() in i["assignee_email"].lower()
        ],
    }
