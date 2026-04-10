"""
Tests for `t3nets_sdk.manifest` — pydantic validators for `practice.yaml`
and `skill.yaml`.

These verify the schemas accept all real built-in manifests, reject the
common authoring mistakes, and produce errors clear enough that the install
path can show them verbatim.
"""

from pathlib import Path

import pytest
from t3nets_sdk.manifest import (
    ManifestError,
    PracticeManifest,
    SkillManifest,
    parse_practice_yaml,
    parse_skill_yaml,
)

REPO_ROOT = Path(__file__).parent.parent


# ---------- parse_practice_yaml ----------


class TestParsePracticeYaml:
    def test_minimal_manifest_only_requires_name(self) -> None:
        m = parse_practice_yaml("name: minimal")
        assert isinstance(m, PracticeManifest)
        assert m.name == "minimal"
        # Defaults match the historical _load_manifest defaults
        assert m.version == "1.0.0"
        assert m.description == ""
        assert m.skills == []
        assert m.pages == []
        assert m.hooks == {}

    def test_full_manifest_round_trip(self) -> None:
        text = """
name: dev-jira
display_name: "Dev — Jira"
description: "Sprint stuff"
version: "2.1.0"
icon: gear
integrations: [jira]
skills: [sprint_status, release_notes]
pages:
  - slug: dash
    title: Dashboard
    file: pages/dash.html
    nav_label: Dash
    nav_order: 10
hooks:
  on_install: hooks/install.py
"""
        m = parse_practice_yaml(text)
        assert m.name == "dev-jira"
        assert m.display_name == "Dev — Jira"
        assert m.version == "2.1.0"
        assert m.integrations == ["jira"]
        assert m.skills == ["sprint_status", "release_notes"]
        assert len(m.pages) == 1
        assert m.pages[0].slug == "dash"
        assert m.pages[0].nav_order == 10
        assert m.hooks == {"on_install": "hooks/install.py"}

    def test_name_with_dashes_and_underscores_is_allowed(self) -> None:
        assert parse_practice_yaml("name: dev-jira").name == "dev-jira"
        assert parse_practice_yaml("name: dev_jira").name == "dev_jira"
        assert parse_practice_yaml("name: dev-jira_v2").name == "dev-jira_v2"

    def test_name_with_spaces_is_rejected(self) -> None:
        with pytest.raises(ManifestError, match="name"):
            parse_practice_yaml("name: bad name")

    def test_name_with_slash_is_rejected(self) -> None:
        with pytest.raises(ManifestError, match="name"):
            parse_practice_yaml("name: foo/bar")

    def test_missing_name_is_rejected(self) -> None:
        with pytest.raises(ManifestError, match="name"):
            parse_practice_yaml("description: no name here")

    def test_unknown_top_level_key_is_rejected(self) -> None:
        # extra="forbid" makes typos fail loudly instead of being silently ignored.
        with pytest.raises(ManifestError, match="skillz"):
            parse_practice_yaml("name: typo\nskillz: [oops]")

    def test_top_level_must_be_a_mapping(self) -> None:
        with pytest.raises(ManifestError, match="mapping"):
            parse_practice_yaml("- just\n- a\n- list")

    def test_malformed_yaml_raises_manifest_error(self) -> None:
        with pytest.raises(ManifestError):
            parse_practice_yaml("name: foo\n  bad: [unbalanced")

    def test_manifest_error_is_a_value_error(self) -> None:
        # Existing callers do `except ValueError` — keep that contract.
        with pytest.raises(ValueError):
            parse_practice_yaml("name: bad name")

    def test_page_requires_slug_title_and_file(self) -> None:
        text = """
name: pageless
pages:
  - slug: dash
    title: Dashboard
"""
        with pytest.raises(ManifestError, match="file"):
            parse_practice_yaml(text)

    def test_page_nav_order_defaults_to_zero(self) -> None:
        text = """
name: paged
pages:
  - slug: dash
    title: Dashboard
    file: pages/dash.html
"""
        m = parse_practice_yaml(text)
        assert m.pages[0].nav_order == 0
        assert m.pages[0].type == "dashboard"


# ---------- parse_skill_yaml ----------


class TestParseSkillYaml:
    def test_minimal_skill_requires_name_and_description(self) -> None:
        s = parse_skill_yaml("name: ping\ndescription: pong")
        assert isinstance(s, SkillManifest)
        assert s.name == "ping"
        assert s.description == "pong"
        assert s.triggers == []
        assert s.parameters == {}
        assert s.supports_raw is False

    def test_skill_with_triggers_and_parameters(self) -> None:
        text = """
name: release_notes
description: |
  Generate release notes from recent commits.
triggers:
  - "release notes"
  - "what shipped"
requires_integration: jira
supports_raw: true
parameters:
  type: object
  properties:
    days:
      type: integer
"""
        s = parse_skill_yaml(text)
        assert s.name == "release_notes"
        assert "Generate release notes" in s.description
        assert s.triggers == ["release notes", "what shipped"]
        assert s.requires_integration == "jira"
        assert s.supports_raw is True
        assert s.parameters["properties"]["days"]["type"] == "integer"

    def test_missing_description_is_rejected(self) -> None:
        with pytest.raises(ManifestError, match="description"):
            parse_skill_yaml("name: nameonly")

    def test_invalid_skill_name_is_rejected(self) -> None:
        with pytest.raises(ManifestError, match="name"):
            parse_skill_yaml("name: bad name\ndescription: x")


# ---------- Built-in manifests must validate ----------


class TestBuiltinManifestsValidate:
    """Real practice/skill manifests in the repo must parse cleanly.
    If a maintainer adds a new built-in that breaks the schema, this fails."""

    def test_all_builtin_practice_manifests_parse(self) -> None:
        practices = list((REPO_ROOT / "agent" / "practices").rglob("practice.yaml"))
        assert practices, "expected at least one built-in practice.yaml"
        for p in practices:
            parse_practice_yaml(p.read_text())

    def test_all_builtin_skill_manifests_parse(self) -> None:
        skill_yamls = list((REPO_ROOT / "agent").rglob("skill.yaml"))
        assert skill_yamls, "expected at least one built-in skill.yaml"
        for p in skill_yamls:
            parse_skill_yaml(p.read_text())
