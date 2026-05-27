"""Tests for sova.dashboard.settings_meta -- settings metadata registry."""

from __future__ import annotations

import pytest

from sova.dashboard.settings_meta import (
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

    def test_all_groups_in_order(self) -> None:
        for gid in GROUP_ORDER:
            assert gid in GROUPS, f"GROUP_ORDER contains '{gid}' not in GROUPS"

    def test_all_groups_have_order(self) -> None:
        for gid in GROUPS:
            assert gid in GROUP_ORDER, f"GROUPS contains '{gid}' not in GROUP_ORDER"


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
