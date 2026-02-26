"""
Release Notes skill tests.

Tests cover:
- Worker: execute() dispatch, credential validation, issue parsing, error handling
- Worker: future/unstarted release handling
- Routing: rule_router correctly routes release-related messages
- Routing: release_name extraction from user messages
- Registration: skill.yaml loads into the registry
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.skills.release_notes.worker import (
    execute,
    _extract_issue,
    _list_releases,
    _summarize_release,
)
from agent.skills.registry import SkillRegistry
from agent.router.rule_router import RuleBasedRouter, strip_raw_flag


# --- Fixtures ---


FAKE_SECRETS = {
    "url": "https://mycompany.atlassian.net",
    "email": "bot@company.com",
    "api_token": "tok-123",
    "board_id": "42",
    "project_key": "PROJ",
}


def _fake_jira_response(data: dict) -> MagicMock:
    """Create a mock urllib response."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# --- Worker: credential validation ---


def test_execute_missing_credentials():
    """Should return error when Jira credentials are missing."""
    result = execute({"action": "list_releases"}, {})
    assert "error" in result
    assert "Missing Jira credentials" in result["error"]


def test_execute_partial_credentials():
    """Should list specific missing keys."""
    result = execute({"action": "list_releases"}, {"url": "https://x.atlassian.net"})
    assert "email" in result["error"]
    assert "api_token" in result["error"]


def test_execute_unknown_action():
    """Should return error for unknown action."""
    result = execute({"action": "bogus"}, FAKE_SECRETS)
    assert "error" in result
    assert "Unknown action" in result["error"]


# --- Worker: list_releases ---


@patch("agent.skills.release_notes.worker._jira_rest_request")
def test_list_releases_success(mock_req):
    """Should group released and unreleased versions."""
    mock_req.return_value = [
        {
            "name": "v1.0.0",
            "id": "100",
            "released": True,
            "releaseDate": "2025-06-01",
            "startDate": "2025-05-01",
            "archived": False,
            "description": "Initial release",
        },
        {
            "name": "v2.0.0",
            "id": "200",
            "released": False,
            "archived": False,
            "description": "Next major",
        },
    ]

    result = execute({"action": "list_releases"}, FAKE_SECRETS)

    assert result["project"] == "PROJ"
    assert result["total_versions"] == 2
    assert len(result["released"]) == 1
    assert len(result["unreleased"]) == 1
    assert result["released"][0]["name"] == "v1.0.0"
    assert result["unreleased"][0]["name"] == "v2.0.0"


@patch("agent.skills.release_notes.worker._get_project_key_from_board")
def test_list_releases_no_project_key(mock_board):
    """Should error if project key cannot be determined."""
    mock_board.return_value = ""
    secrets = {k: v for k, v in FAKE_SECRETS.items() if k != "project_key"}

    result = execute({"action": "list_releases"}, secrets)

    assert "error" in result
    assert "project key" in result["error"].lower()


# --- Worker: summarize ---


def test_summarize_missing_release_name():
    """Should error when release_name is missing for summarize."""
    result = execute({"action": "summarize"}, FAKE_SECRETS)
    assert "error" in result
    assert "release_name is required" in result["error"]


@patch("agent.skills.release_notes.worker._get_version_info")
@patch("agent.skills.release_notes.worker._jira_search")
def test_summarize_success(mock_search, mock_version_info):
    """Should return structured release summary."""
    mock_version_info.return_value = {
        "name": "v1.0.0",
        "released": True,
        "release_date": "2025-06-20",
    }
    mock_search.return_value = [
        {
            "key": "PROJ-1",
            "fields": {
                "summary": "Add login page",
                "status": {"name": "Done"},
                "issuetype": {"name": "Story"},
                "priority": {"name": "High"},
                "assignee": {"displayName": "Alice"},
                "reporter": {"displayName": "Bob"},
                "resolution": {"name": "Done"},
                "resolutiondate": "2025-06-15",
                "labels": ["frontend"],
                "components": [{"name": "auth"}],
                "customfield_10016": 5,
            },
        },
        {
            "key": "PROJ-2",
            "fields": {
                "summary": "Fix crash on logout",
                "status": {"name": "Done"},
                "issuetype": {"name": "Bug"},
                "priority": {"name": "Critical"},
                "assignee": {"displayName": "Alice"},
                "reporter": {"displayName": "Charlie"},
                "resolution": {"name": "Done"},
                "resolutiondate": "2025-06-16",
                "labels": [],
                "components": [],
                "customfield_10016": 3,
            },
        },
    ]

    result = execute(
        {"action": "summarize", "release_name": "v1.0.0"},
        FAKE_SECRETS,
    )

    assert result["release"] == "v1.0.0"
    assert result["total_issues"] == 2
    assert result["total_story_points"] == 8
    assert "Story" in result["issues_by_type"]
    assert "Bug" in result["issues_by_type"]
    assert result["summary"]["by_type"]["Story"] == 1
    assert result["summary"]["by_type"]["Bug"] == 1
    assert result["summary"]["contributors"]["Alice"] == 2


@patch("agent.skills.release_notes.worker._get_version_info")
@patch("agent.skills.release_notes.worker._jira_search")
def test_summarize_no_issues(mock_search, mock_version_info):
    """Should return not_started when unreleased version has no issues."""
    mock_search.return_value = []
    mock_version_info.return_value = {
        "name": "v99.0.0",
        "released": False,
        "release_date": "",
    }

    result = execute(
        {"action": "summarize", "release_name": "v99.0.0"},
        FAKE_SECRETS,
    )

    assert result["total_issues"] == 0
    assert result.get("not_started") is True
    assert "not started" in result["message"].lower()


@patch("agent.skills.release_notes.worker._get_version_info")
def test_summarize_release_not_found(mock_version_info):
    """Should return error when release doesn't exist in Jira."""
    mock_version_info.return_value = {}

    result = execute(
        {"action": "summarize", "release_name": "v999.0.0"},
        FAKE_SECRETS,
    )

    assert "error" in result
    assert "not found" in result["error"].lower()


@patch("agent.skills.release_notes.worker._get_version_info")
@patch("agent.skills.release_notes.worker._jira_search")
def test_summarize_future_release_no_work_started(mock_search, mock_version_info):
    """Should return not_started when issues exist but none have started."""
    mock_version_info.return_value = {
        "name": "v3.0.0",
        "released": False,
        "release_date": "",
    }
    mock_search.return_value = [
        {
            "key": "PROJ-10",
            "fields": {
                "summary": "New feature planned",
                "status": {"name": "To Do"},
                "issuetype": {"name": "Story"},
                "priority": {"name": "Medium"},
                "assignee": None,
                "reporter": {"displayName": "PM"},
                "resolution": None,
                "labels": [],
                "components": [],
                "customfield_10016": 5,
            },
        },
        {
            "key": "PROJ-11",
            "fields": {
                "summary": "Another planned item",
                "status": {"name": "Backlog"},
                "issuetype": {"name": "Task"},
                "priority": {"name": "Low"},
                "assignee": None,
                "reporter": {"displayName": "PM"},
                "resolution": None,
                "labels": [],
                "components": [],
                "customfield_10016": 3,
            },
        },
    ]

    result = execute(
        {"action": "summarize", "release_name": "v3.0.0"},
        FAKE_SECRETS,
    )

    assert result.get("not_started") is True
    assert result["total_issues"] == 2
    assert "no work to summarize" in result["message"].lower()


@patch("agent.skills.release_notes.worker._get_version_info")
@patch("agent.skills.release_notes.worker._jira_search")
def test_summarize_future_release_with_work(mock_search, mock_version_info):
    """Should summarize normally if unreleased version has work in progress."""
    mock_version_info.return_value = {
        "name": "v2.0.0",
        "released": False,
        "release_date": "",
    }
    mock_search.return_value = [
        {
            "key": "PROJ-20",
            "fields": {
                "summary": "Feature in progress",
                "status": {"name": "In Progress"},
                "issuetype": {"name": "Story"},
                "priority": {"name": "High"},
                "assignee": {"displayName": "Alice"},
                "reporter": {"displayName": "Bob"},
                "resolution": None,
                "resolutiondate": None,
                "labels": [],
                "components": [],
                "customfield_10016": 8,
            },
        },
    ]

    result = execute(
        {"action": "summarize", "release_name": "v2.0.0"},
        FAKE_SECRETS,
    )

    assert result.get("not_started") is not True
    assert result["total_issues"] == 1
    assert "issues_by_type" in result


# --- Worker: search endpoint and pagination ---


@patch("agent.skills.release_notes.worker._jira_rest_request")
def test_jira_search_uses_new_endpoint(mock_req):
    """Should use /rest/api/3/search/jql (not deprecated /search)."""
    from agent.skills.release_notes.worker import _jira_search

    mock_req.return_value = {"issues": [], "isLast": True}
    _jira_search(FAKE_SECRETS, 'project = "NV"', ["summary"])

    call_args = mock_req.call_args[0]
    endpoint = call_args[1]
    assert endpoint.startswith("search/jql?"), f"Expected search/jql endpoint, got: {endpoint}"
    assert "startAt" not in endpoint, "Should not use deprecated startAt parameter"


@patch("agent.skills.release_notes.worker._jira_rest_request")
def test_jira_search_pagination_with_token(mock_req):
    """Should paginate using nextPageToken until isLast or no token."""
    from agent.skills.release_notes.worker import _jira_search

    mock_req.side_effect = [
        {
            "issues": [{"key": "NV-1", "fields": {}}],
            "nextPageToken": "token-page-2",
            "isLast": False,
        },
        {
            "issues": [{"key": "NV-2", "fields": {}}],
            "isLast": True,
        },
    ]

    results = _jira_search(FAKE_SECRETS, 'project = "NV"', ["summary"])

    assert len(results) == 2
    assert results[0]["key"] == "NV-1"
    assert results[1]["key"] == "NV-2"
    assert mock_req.call_count == 2

    # Second call should include nextPageToken
    second_call_endpoint = mock_req.call_args_list[1][0][1]
    assert "nextPageToken=token-page-2" in second_call_endpoint


# --- Worker: _extract_issue ---


def test_extract_issue_full():
    """Should flatten Jira issue into clean dict."""
    issue = {
        "key": "PROJ-42",
        "fields": {
            "summary": "Implement SSO",
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Story"},
            "priority": {"name": "Medium"},
            "assignee": {"displayName": "Dave"},
            "reporter": {"displayName": "Eve"},
            "resolution": None,
            "resolutiondate": None,
            "labels": ["security"],
            "components": [{"name": "auth"}, {"name": "backend"}],
            "customfield_10016": 8,
        },
    }

    result = _extract_issue(FAKE_SECRETS, issue)

    assert result["key"] == "PROJ-42"
    assert result["summary"] == "Implement SSO"
    assert result["status"] == "In Progress"
    assert result["issue_type"] == "Story"
    assert result["assignee"] == "Dave"
    assert result["resolution"] == "Unresolved"
    assert result["story_points"] == 8
    assert result["components"] == ["auth", "backend"]
    assert result["url"] == "https://mycompany.atlassian.net/browse/PROJ-42"


def test_extract_issue_nulls():
    """Should handle null/missing fields gracefully."""
    issue = {"key": "X-1", "fields": {}}

    result = _extract_issue(FAKE_SECRETS, issue)

    assert result["assignee"] == "Unassigned"
    assert result["resolution"] == "Unresolved"
    assert result["components"] == []
    assert result["story_points"] is None


# --- Worker: error handling ---


@patch("agent.skills.release_notes.worker._jira_rest_request")
def test_http_error_handling(mock_req):
    """Should catch HTTP errors and return friendly message."""
    import urllib.error

    mock_req.side_effect = urllib.error.HTTPError(
        url="https://x.atlassian.net/rest/api/3/project/PROJ/versions",
        code=403,
        msg="Forbidden",
        hdrs={},
        fp=MagicMock(read=lambda: b"Access denied"),
    )

    result = execute({"action": "list_releases"}, FAKE_SECRETS)

    assert "error" in result
    assert "403" in result["error"]


# --- Routing: rule_router integration ---


@pytest.fixture
def router():
    """Build a RuleBasedRouter with release_notes loaded."""
    skills_dir = Path(__file__).parent.parent / "agent" / "skills"
    registry = SkillRegistry()
    registry.load_from_directory(skills_dir)
    return RuleBasedRouter(registry)


ENABLED_SKILLS = ["release_notes", "sprint_status", "ping"]


class TestReleaseNotesRouting:
    """Verify rule_router correctly routes release-related messages."""

    def test_release_notes_trigger(self, router):
        match = router.match("show me the release notes", ENABLED_SKILLS)
        assert match is not None
        assert match.skill_name == "release_notes"

    def test_changelog_trigger(self, router):
        match = router.match("generate a changelog", ENABLED_SKILLS)
        assert match is not None
        assert match.skill_name == "release_notes"

    def test_what_shipped_trigger(self, router):
        match = router.match("what shipped last week", ENABLED_SKILLS)
        assert match is not None
        assert match.skill_name == "release_notes"

    def test_list_releases_action(self, router):
        match = router.match("list all releases", ENABLED_SKILLS)
        assert match is not None
        assert match.skill_name == "release_notes"
        assert match.action == "list_releases"

    def test_what_versions_routes_to_list(self, router):
        match = router.match("what versions do we have", ENABLED_SKILLS)
        assert match is not None
        assert match.skill_name == "release_notes"
        assert match.action == "list_releases"

    def test_summarize_action(self, router):
        match = router.match("summarize the release v2.0", ENABLED_SKILLS)
        assert match is not None
        assert match.skill_name == "release_notes"
        assert match.action == "summarize"

    def test_summarize_extracts_version(self, router):
        """Should extract version number from message."""
        match = router.match("release notes for v2.5.0", ENABLED_SKILLS)
        assert match is not None
        assert match.skill_name == "release_notes"
        assert match.params.get("release_name") == "v2.5.0"

    def test_summarize_extracts_quoted_name(self, router):
        """Should extract quoted release name."""
        match = router.match('generate release notes for "Nova 3.0"', ENABLED_SKILLS)
        assert match is not None
        assert match.skill_name == "release_notes"
        assert match.params.get("release_name") == "Nova 3.0"

    def test_sprint_status_not_stolen(self, router):
        """'sprint status' should still route to sprint_status, not release_notes."""
        match = router.match("sprint status", ENABLED_SKILLS)
        assert match is not None
        assert match.skill_name == "sprint_status"

    def test_release_status_routes_to_sprint(self, router):
        """'release status' should route to sprint_status (delivery context)."""
        match = router.match("release status", ENABLED_SKILLS)
        assert match is not None
        assert match.skill_name == "sprint_status"

    def test_raw_mode_supported(self, router):
        assert router.supports_raw("release_notes") is True

    def test_raw_flag_stripped(self):
        """--raw flag should be stripped from message text."""
        clean, is_raw = strip_raw_flag("list releases --raw")
        assert is_raw is True
        assert "--raw" not in clean
        assert "list releases" in clean


# --- Skill registration ---


def test_skill_yaml_loads():
    """skill.yaml should load into the registry without errors."""
    skills_dir = Path(__file__).parent.parent / "agent" / "skills"
    registry = SkillRegistry()
    registry.load_from_directory(skills_dir)

    skill = registry.get_skill("release_notes")
    assert skill is not None
    assert skill.name == "release_notes"
    assert skill.requires_integration == "jira"
    assert skill.supports_raw is True
    assert len(skill.triggers) >= 5
    assert "list_releases" in str(skill.parameters)
    assert "summarize" in str(skill.parameters)


def test_worker_importable():
    """Registry should be able to import the worker module."""
    skills_dir = Path(__file__).parent.parent / "agent" / "skills"
    registry = SkillRegistry()
    registry.load_from_directory(skills_dir)

    worker_fn = registry.get_worker("release_notes")
    assert callable(worker_fn)
