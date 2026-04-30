"""Tests for SOVA configuration loading."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from sova.config.loader import load_config
from sova.config.models import (
    AgentConfig,
    CIConfig,
    ProjectConfig,
    ReviewConfig,
    TaskSourceConfig,
    TriageConfig,
    WatchConfig,
    WorktreeConfig,
)


def test_default_config() -> None:
    """Default config has sensible values."""
    cfg = ProjectConfig()
    assert cfg.base_branch == "main"
    assert cfg.task_source.type == "github"
    assert cfg.agent.model == "opus"
    assert cfg.agent.max_budget == Decimal("10.00")
    assert cfg.review.enabled is True
    assert cfg.review.max_rounds == 2
    assert cfg.commit.format == "conventional"
    assert cfg.roles.default == "developer"
    assert cfg.triage.auto_label is True
    assert cfg.triage.min_confidence == 0.7


def test_load_from_toml(tmp_path: Path) -> None:
    """Load config from a TOML file."""
    toml_content = """
[project]
github_repo = "user/repo"
github_user = "testuser"
base_branch = "develop"
test_cmd = "pytest"

[agent]
model = "sonnet"
max_budget = 5.00

[task_source]
type = "github"

[review]
enabled = false
max_rounds = 3

[triage]
min_confidence = 0.8

[roles]
default = "researcher"

[roles.nicknames]
reviewer = "Koda"
"""
    toml_file = tmp_path / "sova.toml"
    toml_file.write_text(toml_content)

    cfg = load_config(tmp_path)
    assert cfg.github_repo == "user/repo"
    assert cfg.github_user == "testuser"
    assert cfg.base_branch == "develop"
    assert cfg.test_cmd == "pytest"
    assert cfg.agent.model == "sonnet"
    assert cfg.agent.max_budget == Decimal("5")
    assert cfg.task_source.type == "github"
    assert cfg.review.enabled is False
    assert cfg.review.max_rounds == 3
    assert cfg.triage.min_confidence == 0.8
    assert cfg.roles.default == "researcher"
    assert cfg.roles.nicknames == {"reviewer": "Koda"}


def test_notification_section_loaded_from_toml(tmp_path: Path) -> None:
    """Notification config is loaded from [notification] section in sova.toml."""
    toml_content = """
[project]
github_repo = "user/repo"

[notification]
desktop = true
slack_webhook_url = "https://hooks.slack.com/services/xxx"
"""
    toml_file = tmp_path / "sova.toml"
    toml_file.write_text(toml_content)

    cfg = load_config(tmp_path)
    assert cfg.notification.desktop is True
    assert cfg.notification.slack_webhook_url == "https://hooks.slack.com/services/xxx"


def test_notification_defaults_when_missing(tmp_path: Path) -> None:
    """Without [notification] section, defaults are used."""
    toml_content = """
[project]
github_repo = "user/repo"
"""
    toml_file = tmp_path / "sova.toml"
    toml_file.write_text(toml_content)

    cfg = load_config(tmp_path)
    assert cfg.notification.desktop is False
    assert cfg.notification.slack_webhook_url == ""


def test_legacy_conf_ignored_without_toml(tmp_path: Path) -> None:
    """Legacy .conf files are no longer loaded; defaults are returned instead.

    Users should migrate via `sova migrate config`.
    """
    conf_dir = tmp_path / ".claude" / "scripts"
    conf_dir.mkdir(parents=True)
    conf_file = conf_dir / "pak-agent.conf"
    conf_file.write_text('GITHUB_REPO="owner/myproject"\n')

    cfg = load_config(tmp_path)
    # Should NOT load legacy config -- returns defaults
    assert cfg.github_repo == ""
    assert cfg.base_branch == "main"


def test_load_nonexistent_dir_returns_defaults(tmp_path: Path) -> None:
    """Loading from a directory without config files returns defaults."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    cfg = load_config(empty_dir)
    assert cfg.base_branch == "main"
    assert cfg.task_source.type == "github"


def test_shared_knowledge_path_expansion() -> None:
    """Shared knowledge dir expands ~ to home."""
    cfg = ProjectConfig()
    path = cfg.shared_knowledge_path
    assert "~" not in str(path)
    assert path.is_absolute()


# -- Project Registry Tests --------------------------------------------------


class TestProjectRegistry:
    """Tests for sova.config.registry."""

    def test_register_and_list(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        project = tmp_path / "my-project"
        project.mkdir()

        slug = registry.register_project(project)
        assert slug == "my-project"

        projects = registry.list_projects()
        assert "my-project" in projects
        assert projects["my-project"] == str(project)

    def test_get_project_path(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        project = tmp_path / "proj"
        project.mkdir()
        registry.register_project(project, slug="test")

        result = registry.get_project_path("test")
        assert result == project

        assert registry.get_project_path("nonexistent") is None

    def test_unregister(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        project = tmp_path / "proj"
        project.mkdir()
        registry.register_project(project, slug="test")

        assert registry.unregister_project("test") is True
        assert registry.unregister_project("test") is False
        assert registry.list_projects() == {}

    def test_slug_deduplication(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        p1 = tmp_path / "proj"
        p1.mkdir()
        p2 = tmp_path / "other" / "proj"
        p2.mkdir(parents=True)

        slug1 = registry.register_project(p1)
        slug2 = registry.register_project(p2)

        assert slug1 == "proj"
        assert slug2 == "proj-2"

    def test_custom_slug(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        project = tmp_path / "my-project"
        project.mkdir()

        slug = registry.register_project(project, slug="custom")
        assert slug == "custom"

    def test_has_projects(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        assert registry.has_projects() is False

        project = tmp_path / "proj"
        project.mkdir()
        registry.register_project(project)
        assert registry.has_projects() is True

    def test_slugify(self) -> None:
        from sova.config.registry import _slugify

        assert _slugify("My Project") == "my-project"
        assert _slugify("hello_world") == "hello-world"
        assert _slugify("--test--") == "test"
        assert _slugify("") == "project"

    def test_register_nonexistent_raises(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        import pytest

        with pytest.raises(ValueError, match="Not a directory"):
            registry.register_project(tmp_path / "nonexistent")

    def test_re_register_same_path(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        project = tmp_path / "proj"
        project.mkdir()

        slug1 = registry.register_project(project)
        slug2 = registry.register_project(project)
        assert slug1 == slug2  # Same path re-registers under same slug


class TestFieldConstraints:
    """Pydantic Field constraints reject invalid numeric values."""

    @pytest.mark.parametrize(
        "model_cls, field, invalid_value",
        [
            (AgentConfig, "max_budget", Decimal("-1")),
            (AgentConfig, "max_budget", Decimal("0")),
            (ReviewConfig, "max_rounds", 0),
            (ReviewConfig, "max_rounds", -1),
            (CIConfig, "poll_interval", 0),
            (CIConfig, "poll_interval", -60),
            (CIConfig, "max_wait", 0),
            (CIConfig, "max_wait", -1),
            (WatchConfig, "interval_active", 0),
            (WatchConfig, "interval_active", -1),
            (WatchConfig, "interval_idle", 0),
            (WatchConfig, "interval_idle", -300),
            (WatchConfig, "veto_seconds", 0),
            (WatchConfig, "veto_seconds", -5),
            (WorktreeConfig, "ttl_done_days", 0),
            (WorktreeConfig, "ttl_done_days", -1),
            (WorktreeConfig, "ttl_paused_days", 0),
            (WorktreeConfig, "ttl_paused_days", -1),
            (TriageConfig, "min_confidence", -0.1),
            (TriageConfig, "min_confidence", 1.5),
        ],
        ids=[
            "agent-max_budget-negative",
            "agent-max_budget-zero",
            "review-max_rounds-zero",
            "review-max_rounds-negative",
            "ci-poll_interval-zero",
            "ci-poll_interval-negative",
            "ci-max_wait-zero",
            "ci-max_wait-negative",
            "watch-interval_active-zero",
            "watch-interval_active-negative",
            "watch-interval_idle-zero",
            "watch-interval_idle-negative",
            "watch-veto_seconds-zero",
            "watch-veto_seconds-negative",
            "worktree-ttl_done_days-zero",
            "worktree-ttl_done_days-negative",
            "worktree-ttl_paused_days-zero",
            "worktree-ttl_paused_days-negative",
            "triage-min_confidence-negative",
            "triage-min_confidence-above-one",
        ],
    )
    def test_rejects_invalid(self, model_cls: type, field: str, invalid_value: object) -> None:
        with pytest.raises(ValidationError):
            model_cls(**{field: invalid_value})

    def test_triage_min_confidence_boundary_zero(self) -> None:
        cfg = TriageConfig(min_confidence=0.0)
        assert cfg.min_confidence == 0.0

    def test_triage_min_confidence_boundary_one(self) -> None:
        cfg = TriageConfig(min_confidence=1.0)
        assert cfg.min_confidence == 1.0

    @pytest.mark.parametrize("bad_type", ["jira", "linear", "manual", "unknown"])
    def test_task_source_rejects_non_github_type(self, bad_type: str) -> None:
        with pytest.raises(ValidationError):
            TaskSourceConfig(type=bad_type)

    def test_defaults_still_accepted(self) -> None:
        """All constrained models accept their default values."""
        AgentConfig()
        ReviewConfig()
        CIConfig()
        WatchConfig()
        WorktreeConfig()
        TriageConfig()
