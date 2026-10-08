"""Tests for sova.dashboard.settings_meta -- settings metadata registry."""

from __future__ import annotations

import pytest

from sova.config.models import AtlassianMCPConfig
from sova.dashboard.settings_meta import (
    _REGISTRY,
    GROUP_ORDER,
    GROUPS,
    _humanize_key,
    _infer_group,
    _infer_type,
    get_grouped_config,
    get_meta,
)


class TestSettingMeta:
    def test_get_meta_known_key(self) -> None:
        meta = get_meta("agent.model")
        assert meta is not None
        assert meta.label == "Model"
        assert meta.group == "agent"
        assert "Claude model" in meta.description

    def test_get_meta_unknown_key(self) -> None:
        assert get_meta("nonexistent.key.here") is None

    def test_review_repo_context_agent_meta_registered(self) -> None:
        meta = get_meta("review.repo_context_agent")
        assert meta is not None
        assert meta.group == "review"
        assert meta.value_type == "boolean"

    def test_review_repo_context_timeout_meta_registered(self) -> None:
        meta = get_meta("review.repo_context_timeout")
        assert meta is not None
        assert meta.group == "review"
        assert meta.value_type == "number"

    def test_review_repo_context_max_chars_meta_registered(self) -> None:
        meta = get_meta("review.repo_context_max_chars")
        assert meta is not None
        assert meta.group == "review"
        assert meta.value_type == "number"

    @pytest.mark.parametrize(
        ("key", "value_type"),
        [
            ("mcp.atlassian.enabled", "boolean"),
            ("mcp.atlassian.jira_url", "string"),
            ("mcp.atlassian.confluence_url", "string"),
            ("mcp.atlassian.auth_type", "select"),
            ("mcp.atlassian.email", "string"),
            ("mcp.atlassian.token", "secret"),
            ("mcp.atlassian.read_only", "boolean"),
            ("mcp.atlassian.toolsets", "list"),
        ],
    )
    def test_atlassian_mcp_meta_registered(self, key: str, value_type: str) -> None:
        meta = get_meta(key)
        assert meta is not None
        assert meta.group == "mcp"
        assert meta.value_type == value_type

    def test_atlassian_mcp_keys_match_flattened_config(self) -> None:
        """The registry keys must match what get_config() actually flattens out.

        Registering the container (``mcp.atlassian``) instead of its leaves would
        stop the flattener from recursing, and every sidecar setting would vanish
        from the settings page.
        """
        from sova.dashboard.services.settings_service import _flatten_dict

        flat: dict = {}
        _flatten_dict("", {"mcp": {"atlassian": AtlassianMCPConfig().model_dump()}}, flat)
        registered = {m.key for m in _REGISTRY if m.key.startswith("mcp.atlassian.")}
        assert registered == set(flat)

    def test_commit_pr_auto_link_issues_meta_registered(self) -> None:
        meta = get_meta("commit.pr_auto_link_issues")
        assert meta is not None
        assert meta.group == "commit"
        assert meta.value_type == "boolean"

    def test_all_groups_in_order(self) -> None:
        for gid in GROUP_ORDER:
            assert gid in GROUPS, f"GROUP_ORDER contains '{gid}' not in GROUPS"

    def test_all_groups_have_order(self) -> None:
        for gid in GROUPS:
            assert gid in GROUP_ORDER, f"GROUPS contains '{gid}' not in GROUP_ORDER"


class TestGetGroupedConfigSecrets:
    def test_secret_location_defaults_to_unset(self) -> None:
        groups = get_grouped_config({"llm.api_key": "••••••••"})
        setting = next(s for g in groups for s in g["settings"] if s["key"] == "llm.api_key")
        assert setting["secret_location"] == "unset"

    def test_secret_location_passed_through(self) -> None:
        groups = get_grouped_config({"llm.api_key": "••••••••"}, secret_locations={"llm.api_key": "keyring"})
        setting = next(s for g in groups for s in g["settings"] if s["key"] == "llm.api_key")
        assert setting["secret_location"] == "keyring"

    def test_keyring_capable_true_for_resolved_key(self) -> None:
        groups = get_grouped_config({"llm.api_key": "••••••••"})
        setting = next(s for g in groups for s in g["settings"] if s["key"] == "llm.api_key")
        assert setting["keyring_capable"] is True

    def test_keyring_capable_false_for_unresolved_secret(self) -> None:
        groups = get_grouped_config({"mcp.token_secret": "••••••••"})
        setting = next(s for g in groups for s in g["settings"] if s["key"] == "mcp.token_secret")
        assert setting["keyring_capable"] is False

    def test_non_secret_setting_has_no_secret_fields(self) -> None:
        groups = get_grouped_config({"agent.model": "opus"})
        setting = next(s for g in groups for s in g["settings"] if s["key"] == "agent.model")
        assert "secret_location" not in setting
        assert "keyring_capable" not in setting


class TestGetGroupedConfig:
    def test_empty_config(self) -> None:
        result = get_grouped_config({})
        assert result == []

    def test_error_key_filtered(self) -> None:
        result = get_grouped_config({"_error": "No config"})
        assert result == []

    def test_known_keys_grouped(self) -> None:
        flat = {
            "agent.model": "opus",
            "agent.max_budget": 10,
            "ci.poll_interval": 60,
        }
        groups = get_grouped_config(flat)
        group_ids = [g["id"] for g in groups]
        assert "agent" in group_ids
        assert "ci" in group_ids

        agent_group = next(g for g in groups if g["id"] == "agent")
        assert agent_group["label"] == "Agent"
        assert len(agent_group["settings"]) == 2

        model_setting = next(s for s in agent_group["settings"] if s["key"] == "agent.model")
        assert model_setting["label"] == "Model"
        assert model_setting["value"] == "opus"
        assert model_setting["description"] != ""

    def test_unknown_keys_infer_group(self) -> None:
        flat = {"custom.foo": "bar"}
        groups = get_grouped_config(flat)
        assert len(groups) == 1
        assert groups[0]["id"] == "custom"
        assert groups[0]["settings"][0]["label"] == "Foo"

    def test_top_level_keys_go_to_project(self) -> None:
        flat = {"github_repo": "user/repo"}
        groups = get_grouped_config(flat)
        assert groups[0]["id"] == "project"
        setting = groups[0]["settings"][0]
        assert setting["key"] == "github_repo"
        assert setting["label"] == "GitHub repository"

    def test_group_order_respected(self) -> None:
        flat = {
            "server.port": 8111,
            "agent.model": "opus",
            "ci.poll_interval": 60,
        }
        groups = get_grouped_config(flat)
        ids = [g["id"] for g in groups]
        assert ids.index("agent") < ids.index("ci")
        assert ids.index("ci") < ids.index("server")

    def test_boolean_type_detected(self) -> None:
        flat = {"review.enabled": True}
        groups = get_grouped_config(flat)
        setting = groups[0]["settings"][0]
        assert setting["value_type"] == "boolean"

    def test_list_value(self) -> None:
        flat = {"ci.flaky_checks": ["check-a", "check-b"]}
        groups = get_grouped_config(flat)
        setting = groups[0]["settings"][0]
        assert setting["value_type"] == "list"
        assert setting["value"] == ["check-a", "check-b"]

    def test_object_value(self) -> None:
        flat = {"roles.nicknames": {"dev": "developer"}}
        groups = get_grouped_config(flat)
        setting = next(s for g in groups for s in g["settings"] if s["key"] == "roles.nicknames")
        assert setting["value_type"] == "object"


class TestHelpers:
    @pytest.mark.parametrize(
        "key,expected",
        [
            ("agent.model", "agent"),
            ("ci.poll_interval", "ci"),
            ("github_repo", "project"),
            ("some.nested.key", "some"),
        ],
    )
    def test_infer_group(self, key: str, expected: str) -> None:
        assert _infer_group(key) == expected

    @pytest.mark.parametrize(
        "key,expected",
        [
            ("agent.model", "Model"),
            ("ci.poll_interval", "Poll interval"),
            ("github_repo", "Github repo"),
            ("some.multi_word_key", "Multi word key"),
        ],
    )
    def test_humanize_key(self, key: str, expected: str) -> None:
        assert _humanize_key(key) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [
            (True, "boolean"),
            (False, "boolean"),
            (42, "number"),
            (3.14, "number"),
            ("hello", "string"),
            ([], "list"),
            ({}, "object"),
            (None, "string"),
        ],
    )
    def test_infer_type(self, value: object, expected: str) -> None:
        assert _infer_type(value) == expected


class TestLLMProviderMeta:
    def test_provider_is_select_with_options(self) -> None:
        meta = get_meta("llm.provider")
        assert meta is not None
        assert meta.value_type == "select"
        assert meta.requires_restart is False
        assert meta.options == ("claude-code", "litellm", "hybrid", "anthropic", "openai", "ollama", "vertex")

    def test_api_key_is_secret(self) -> None:
        meta = get_meta("llm.api_key")
        assert meta is not None
        assert meta.value_type == "secret"
        assert meta.group == "llm"
        assert meta.requires_restart is False

    def test_grouped_config_exposes_options(self) -> None:
        groups = get_grouped_config({"llm.provider": "anthropic"})
        llm_group = next(g for g in groups if g["id"] == "llm")
        setting = next(s for s in llm_group["settings"] if s["key"] == "llm.provider")
        assert setting["value_type"] == "select"
        assert setting["options"] == ["claude-code", "litellm", "hybrid", "anthropic", "openai", "ollama", "vertex"]

    def test_grouped_config_options_default_empty(self) -> None:
        groups = get_grouped_config({"agent.model": "opus"})
        setting = groups[0]["settings"][0]
        assert setting["options"] == []
