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
    IntegrationGatesConfig,
    PipelineConfig,
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
    assert cfg.agent.fallback_models == []
    assert cfg.agent.max_budget == Decimal("10.00")
    assert cfg.agent.step_timeout == 1800
    assert cfg.review.enabled is True
    assert cfg.review.max_rounds == 2
    assert cfg.commit.format == "conventional"
    assert cfg.commit.pr_auto_link_issues is True
    assert cfg.roles.default == "developer"
    assert cfg.pipeline.auto_handoff is True
    assert cfg.pipeline.auto_address_review is True
    assert cfg.pipeline.auto_integrate is False
    assert cfg.spec.auto_approve_threshold == "simple"
    assert cfg.supervisor.auto_review is True
    assert cfg.triage.auto_label is True
    assert cfg.triage.min_confidence == 0.7
    assert cfg.monitoring.enabled is True


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


def test_notification_config_defaults() -> None:
    """NotificationConfig has correct defaults."""
    from sova.config.models import NotificationConfig

    cfg = NotificationConfig()
    assert cfg.desktop is False
    assert cfg.slack_webhook_url == ""
    assert cfg.email_enabled is False
    assert cfg.email_to == ""
    assert cfg.email_from == ""
    assert cfg.email_smtp_host == ""
    assert cfg.email_smtp_port == 587
    assert cfg.email_smtp_starttls is True
    assert cfg.email_smtp_user == ""
    assert cfg.email_smtp_password == ""
    assert cfg.webhook_url == ""
    assert cfg.webhook_headers == ""


def test_notification_config_from_toml(tmp_path: Path) -> None:
    """NotificationConfig loads email and webhook settings from TOML."""
    toml_content = """
[notification]
desktop = false
email_enabled = true
email_to = "dev@example.com"
email_from = "sova@example.com"
email_smtp_host = "smtp.example.com"
email_smtp_port = 465
email_smtp_starttls = false
webhook_url = "https://hooks.example.com/sova"
webhook_headers = '{"Authorization": "Bearer token123"}'
"""
    toml_file = tmp_path / "sova.toml"
    toml_file.write_text(toml_content)

    cfg = load_config(tmp_path)
    assert cfg.notification.desktop is False
    assert cfg.notification.email_enabled is True
    assert cfg.notification.email_to == "dev@example.com"
    assert cfg.notification.email_from == "sova@example.com"
    assert cfg.notification.email_smtp_host == "smtp.example.com"
    assert cfg.notification.email_smtp_port == 465
    assert cfg.notification.email_smtp_starttls is False
    assert cfg.notification.webhook_url == "https://hooks.example.com/sova"
    assert cfg.notification.webhook_headers == '{"Authorization": "Bearer token123"}'


def test_server_config_log_rotation_defaults() -> None:
    """ServerConfig has correct log rotation defaults."""
    from sova.config.models import ServerConfig

    cfg = ServerConfig()
    assert cfg.log_max_bytes == 10_485_760  # 10 MB
    assert cfg.log_backup_count == 5


def test_server_config_from_toml(tmp_path: Path) -> None:
    """ServerConfig loads log rotation settings from TOML."""
    toml_content = """
[server]
host = "0.0.0.0"
port = 9000
log_max_bytes = 5242880
log_backup_count = 3
"""
    toml_file = tmp_path / "sova.toml"
    toml_file.write_text(toml_content)

    cfg = load_config(tmp_path)
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 9000
    assert cfg.server.log_max_bytes == 5_242_880
    assert cfg.server.log_backup_count == 3


def test_fallback_models_loaded_from_toml(tmp_path: Path) -> None:
    """fallback_models list is loaded from [agent] section."""
    toml_content = """
[project]
github_repo = "user/repo"

[agent]
model = "opus"
fallback_models = ["sonnet", "haiku"]
"""
    (tmp_path / "sova.toml").write_text(toml_content)
    cfg = load_config(tmp_path)
    assert cfg.agent.fallback_models == ["sonnet", "haiku"]


def test_check_cmd_loaded_from_toml(tmp_path: Path) -> None:
    """check_cmd is loaded from root-level TOML key."""
    toml_content = """
github_repo = "user/repo"
test_cmd = "pytest"
lint_cmd = "ruff check ."
check_cmd = "make check"
"""
    (tmp_path / "sova.toml").write_text(toml_content)
    cfg = load_config(tmp_path)
    assert cfg.check_cmd == "make check"


def test_check_cmd_defaults_to_empty(tmp_path: Path) -> None:
    """check_cmd defaults to empty string when not specified."""
    toml_content = """
github_repo = "user/repo"
test_cmd = "pytest"
lint_cmd = "ruff check ."
"""
    (tmp_path / "sova.toml").write_text(toml_content)
    cfg = load_config(tmp_path)
    assert cfg.check_cmd == ""


def test_migrate_deprecated_no_ai_coauthor(tmp_path: Path) -> None:
    """Old no_ai_coauthor=true is migrated to ai_coauthor=false."""
    toml_content = """
[project]
github_repo = "user/repo"

[commit]
no_ai_coauthor = true
"""
    (tmp_path / "sova.toml").write_text(toml_content)
    cfg = load_config(tmp_path)
    assert cfg.commit.ai_coauthor is False


def test_migrate_deprecated_no_ai_coauthor_false(tmp_path: Path) -> None:
    """Old no_ai_coauthor=false is migrated to ai_coauthor=true."""
    toml_content = """
[project]
github_repo = "user/repo"

[commit]
no_ai_coauthor = false
"""
    (tmp_path / "sova.toml").write_text(toml_content)
    cfg = load_config(tmp_path)
    assert cfg.commit.ai_coauthor is True


def test_migrate_no_clobber_new_key(tmp_path: Path) -> None:
    """If both old and new keys exist, new key wins (no migration)."""
    toml_content = """
[project]
github_repo = "user/repo"

[commit]
no_ai_coauthor = true
ai_coauthor = true
"""
    (tmp_path / "sova.toml").write_text(toml_content)
    cfg = load_config(tmp_path)
    assert cfg.commit.ai_coauthor is True


def test_pipeline_section_loaded_from_toml(tmp_path: Path) -> None:
    """Pipeline config is loaded from [pipeline] section in sova.toml."""
    toml_content = """
[project]
github_repo = "user/repo"

[pipeline]
auto_handoff = false
auto_address_review = false
"""
    toml_file = tmp_path / "sova.toml"
    toml_file.write_text(toml_content)

    cfg = load_config(tmp_path)
    assert cfg.pipeline.auto_handoff is False
    assert cfg.pipeline.auto_address_review is False


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


def test_sensitive_fields_hidden_from_repr() -> None:
    """Sensitive config fields must not appear in repr() output."""
    from sova.config.models import NotificationConfig, TaskSourceConfig

    ts = TaskSourceConfig(jira_api_token="secret-jira-token")
    assert "secret-jira-token" not in repr(ts)

    nc = NotificationConfig(slack_webhook_url="https://hooks.slack.com/secret")
    assert "https://hooks.slack.com/secret" not in repr(nc)
    assert "desktop" in repr(nc)


def test_dashboard_section_loaded_from_toml(tmp_path: Path) -> None:
    """Dashboard config is loaded from [dashboard] section in sova.toml."""
    toml_content = """
[project]
github_repo = "user/repo"

[dashboard]
kanban_columns = "role_based"
"""
    toml_file = tmp_path / "sova.toml"
    toml_file.write_text(toml_content)

    cfg = load_config(tmp_path)
    assert cfg.dashboard.kanban_columns == "role_based"


def test_dashboard_defaults_when_missing(tmp_path: Path) -> None:
    """Without [dashboard] section, defaults are used."""
    toml_content = """
[project]
github_repo = "user/repo"
"""
    toml_file = tmp_path / "sova.toml"
    toml_file.write_text(toml_content)

    cfg = load_config(tmp_path)
    assert cfg.dashboard.kanban_columns == "step_based"


def test_dashboard_rejects_invalid_kanban_mode(tmp_path: Path) -> None:
    """Invalid kanban_columns value is rejected by Literal validation."""
    from sova.config.models import DashboardConfig

    with pytest.raises(ValidationError):
        DashboardConfig(kanban_columns="invalid_mode")


def test_dashboard_confirm_model_default() -> None:
    """Default confirm_model is 'complex_only'."""
    from sova.config.models import DashboardConfig

    cfg = DashboardConfig()
    assert cfg.confirm_model == "complex_only"


def test_dashboard_confirm_model_from_toml(tmp_path: Path) -> None:
    """confirm_model is loaded from sova.toml [dashboard] section."""
    toml_content = """
[dashboard]
confirm_model = "always"
"""
    toml_file = tmp_path / "sova.toml"
    toml_file.write_text(toml_content)

    cfg = load_config(tmp_path)
    assert cfg.dashboard.confirm_model == "always"


def test_dashboard_confirm_model_never(tmp_path: Path) -> None:
    """confirm_model accepts 'never' value."""
    toml_content = """
[dashboard]
confirm_model = "never"
"""
    toml_file = tmp_path / "sova.toml"
    toml_file.write_text(toml_content)

    cfg = load_config(tmp_path)
    assert cfg.dashboard.confirm_model == "never"


def test_dashboard_confirm_model_rejects_invalid() -> None:
    """Invalid confirm_model value is rejected."""
    from sova.config.models import DashboardConfig

    with pytest.raises(ValidationError):
        DashboardConfig(confirm_model="sometimes")


def test_legacy_conf_ignored_without_toml(tmp_path: Path) -> None:
    """Legacy .conf files are no longer loaded; defaults are returned instead."""
    conf_dir = tmp_path / ".claude" / "scripts"
    conf_dir.mkdir(parents=True)
    conf_file = conf_dir / "old-config.conf"
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

    def test_find_slug_for_path(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        project = tmp_path / "myproj"
        project.mkdir()
        registry.register_project(project, slug="myproj")

        assert registry.find_slug_for_path(project) == "myproj"
        assert registry.find_slug_for_path(tmp_path / "other") is None

    def test_register_nonexistent_raises(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        import pytest

        with pytest.raises(ValueError, match="Not a directory"):
            registry.register_project(tmp_path / "nonexistent")

    def test_validate_slug_rejects_invalid(self) -> None:
        import pytest

        from sova.config.registry import _validate_slug

        assert _validate_slug("my-project") == "my-project"
        assert _validate_slug("proj123") == "proj123"
        assert _validate_slug("../../../etc") == "etc"
        with pytest.raises(ValueError, match="Invalid slug"):
            _validate_slug("!!!@@@")

    def test_validate_slug_normalizes(self) -> None:
        from sova.config.registry import _validate_slug

        assert _validate_slug("My-Project") == "my-project"

    def test_get_project_path_sanitizes_traversal(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        assert registry.get_project_path("../../../etc") is None

    def test_register_sanitizes_traversal_slug(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        project = tmp_path / "proj"
        project.mkdir()

        slug = registry.register_project(project, slug="../../bad")
        assert slug == "bad"

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

    def test_get_project_path_returns_none_for_invalid_slug(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        assert registry.get_project_path("!!!@@@") is None

    def test_get_project_path_rejects_traversal_alias(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        project = tmp_path / "foo"
        project.mkdir()
        registry.register_project(project, slug="foo")
        assert registry.get_project_path("../../foo") is None

    def test_get_project_path_returns_none_for_deleted_dir(self, tmp_path: Path, monkeypatch: object) -> None:
        from sova.config import registry

        reg_file = tmp_path / "projects.json"
        monkeypatch.setattr(registry, "_REGISTRY_FILE", reg_file)
        monkeypatch.setattr(registry, "_REGISTRY_DIR", tmp_path)

        project = tmp_path / "proj"
        project.mkdir()
        slug = registry.register_project(project)
        project.rmdir()
        assert registry.get_project_path(slug) is None

    def test_validate_project_path_rejects_file(self, tmp_path: Path) -> None:
        from sova.config.registry import _validate_project_path

        f = tmp_path / "file.txt"
        f.write_text("data")
        with pytest.raises(ValueError, match="Not a directory"):
            _validate_project_path(f)

    def test_validate_project_path_resolves_symlinks(self, tmp_path: Path) -> None:
        from sova.config.registry import _validate_project_path

        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "real-link"
        link.symlink_to(real, target_is_directory=True)
        result = _validate_project_path(link)
        assert result == real.resolve()


class TestFieldConstraints:
    """Pydantic Field constraints reject invalid numeric values."""

    @pytest.mark.parametrize(
        "model_cls, field, invalid_value",
        [
            (AgentConfig, "max_budget", Decimal("-1")),
            (AgentConfig, "max_budget", Decimal("0")),
            (AgentConfig, "step_timeout", 0),
            (AgentConfig, "step_timeout", -1),
            (ReviewConfig, "max_rounds", 0),
            (ReviewConfig, "max_rounds", -1),
            (CIConfig, "poll_interval", 0),
            (CIConfig, "poll_interval", -60),
            (CIConfig, "max_wait", 0),
            (CIConfig, "max_wait", -1),
            (CIConfig, "no_checks_grace_period", -1),
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
            "agent-step_timeout-zero",
            "agent-step_timeout-negative",
            "review-max_rounds-zero",
            "review-max_rounds-negative",
            "ci-poll_interval-zero",
            "ci-poll_interval-negative",
            "ci-max_wait-zero",
            "ci-max_wait-negative",
            "ci-no_checks_grace_period-negative",
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

    @pytest.mark.parametrize("bad_type", ["linear", "manual", "unknown"])
    def test_task_source_rejects_unsupported_type(self, bad_type: str) -> None:
        with pytest.raises(ValidationError):
            TaskSourceConfig(type=bad_type)

    def test_task_source_accepts_jira_type(self) -> None:
        cfg = TaskSourceConfig(type="jira")
        assert cfg.type == "jira"

    def test_defaults_still_accepted(self) -> None:
        """All constrained models accept their default values."""
        AgentConfig()
        ReviewConfig()
        CIConfig()
        WatchConfig()
        WorktreeConfig()
        TriageConfig()
        PipelineConfig()


class TestJiraStatusMappingConfig:
    def test_valid_mapping_accepted(self) -> None:
        cfg = TaskSourceConfig(
            type="jira",
            jira_status_mapping={"ON_QA": "done", "Selected for Development": "triaged"},
        )
        assert cfg.jira_status_mapping == {"ON_QA": "done", "Selected for Development": "triaged"}

    def test_invalid_state_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Invalid SOVA state"):
            TaskSourceConfig(
                type="jira",
                jira_status_mapping={"ON_QA": "testing"},
            )

    def test_empty_mapping_accepted(self) -> None:
        cfg = TaskSourceConfig(type="jira")
        assert cfg.jira_status_mapping == {}

    def test_all_valid_states_accepted(self) -> None:
        mapping = {
            "S1": "backlog",
            "S2": "triaged",
            "S3": "researched",
            "S4": "in_progress",
            "S5": "in_review",
            "S6": "done",
            "S7": "needs_spec",
            "S8": "human_only",
        }
        cfg = TaskSourceConfig(type="jira", jira_status_mapping=mapping)
        assert len(cfg.jira_status_mapping) == 8

    def test_mapping_loaded_from_toml(self, tmp_path: Path) -> None:
        toml_content = """
[project]
github_repo = "user/repo"

[task_source]
type = "jira"
jira_base_url = "https://test.atlassian.net"
jira_email = "test@example.com"
jira_api_token = "token"
jira_project_key = "TEST"
jira_status_mapping = { "ON_QA" = "done", "Selected for Development" = "triaged" }
"""
        (tmp_path / "sova.toml").write_text(toml_content)
        cfg = load_config(tmp_path)
        assert cfg.task_source.jira_status_mapping == {
            "ON_QA": "done",
            "Selected for Development": "triaged",
        }


class TestIntegrationGatesConfig:
    def test_defaults_all_false(self) -> None:
        cfg = IntegrationGatesConfig()
        assert cfg.ci_passed is False
        assert cfg.sova_reviewed is False
        assert cfg.coderabbit_reviewed is False
        assert cfg.threads_resolved is False

    def test_project_config_includes_gates(self) -> None:
        cfg = ProjectConfig()
        assert hasattr(cfg, "integration_gates")
        assert cfg.integration_gates.ci_passed is False

    def test_toml_loading(self, tmp_path: Path) -> None:
        toml_content = """
[project]
github_repo = "user/repo"

[integration_gates]
ci_passed = true
sova_reviewed = true
coderabbit_reviewed = false
threads_resolved = true
"""
        (tmp_path / "sova.toml").write_text(toml_content)
        cfg = load_config(tmp_path)
        assert cfg.integration_gates.ci_passed is True
        assert cfg.integration_gates.sova_reviewed is True
        assert cfg.integration_gates.coderabbit_reviewed is False
        assert cfg.integration_gates.threads_resolved is True


class TestMemoryGuardConfig:
    def test_defaults(self) -> None:
        from sova.config.models import MemoryGuardConfig

        cfg = MemoryGuardConfig()
        assert cfg.enabled is True
        assert cfg.warn_threshold_gb == 3.0
        assert cfg.block_threshold_gb == 1.5

    def test_project_config_includes_memory_guard(self) -> None:
        cfg = ProjectConfig()
        assert hasattr(cfg, "memory_guard")
        assert cfg.memory_guard.enabled is True

    def test_toml_loading(self, tmp_path: Path) -> None:
        toml_content = """
[project]
github_repo = "user/repo"

[memory_guard]
enabled = false
warn_threshold_gb = 8.0
block_threshold_gb = 2.0
"""
        (tmp_path / "sova.toml").write_text(toml_content)
        cfg = load_config(tmp_path)
        assert cfg.memory_guard.enabled is False
        assert cfg.memory_guard.warn_threshold_gb == 8.0
        assert cfg.memory_guard.block_threshold_gb == 2.0

    def test_block_gte_warn_raises(self) -> None:
        from sova.config.models import MemoryGuardConfig

        with pytest.raises(ValueError, match="block_threshold_gb.*must be less than.*warn_threshold_gb"):
            MemoryGuardConfig(warn_threshold_gb=4.0, block_threshold_gb=4.0)

        with pytest.raises(ValueError, match="block_threshold_gb.*must be less than.*warn_threshold_gb"):
            MemoryGuardConfig(warn_threshold_gb=2.0, block_threshold_gb=5.0)


class TestPipelineAutoIntegrate:
    def test_default_false(self) -> None:
        cfg = PipelineConfig()
        assert cfg.auto_integrate is False

    def test_toml_loading(self, tmp_path: Path) -> None:
        toml_content = """
[project]
github_repo = "user/repo"

[pipeline]
auto_integrate = true
"""
        (tmp_path / "sova.toml").write_text(toml_content)
        cfg = load_config(tmp_path)
        assert cfg.pipeline.auto_integrate is True


class TestSpecAutoApproveThreshold:
    def test_default_simple(self) -> None:
        from sova.config.models import SpecConfig

        cfg = SpecConfig()
        assert cfg.auto_approve_threshold == "simple"

    def test_accepts_valid_thresholds(self) -> None:
        from sova.config.models import SpecConfig

        for threshold in ("none", "simple", "moderate", "complex"):
            cfg = SpecConfig(auto_approve_threshold=threshold)
            assert cfg.auto_approve_threshold == threshold

    def test_backward_compat_true_preserves_default(self, tmp_path: Path) -> None:
        """Old auto_approve_simple=true preserves the default threshold."""
        toml_content = """
[project]
github_repo = "user/repo"

[spec]
auto_approve_simple = true
"""
        (tmp_path / "sova.toml").write_text(toml_content)
        cfg = load_config(tmp_path)
        assert cfg.spec.auto_approve_threshold == "simple"

    def test_backward_compat_false_disables_approval(self, tmp_path: Path) -> None:
        """Old auto_approve_simple=false maps to threshold='none' to disable auto-approval."""
        toml_content = """
[project]
github_repo = "user/repo"

[spec]
auto_approve_simple = false
"""
        (tmp_path / "sova.toml").write_text(toml_content)
        cfg = load_config(tmp_path)
        assert cfg.spec.auto_approve_threshold == "none"

    def test_backward_compat_false_does_not_override_explicit_threshold(self, tmp_path: Path) -> None:
        """Explicit auto_approve_threshold takes precedence over old auto_approve_simple=false."""
        toml_content = """
[project]
github_repo = "user/repo"

[spec]
auto_approve_simple = false
auto_approve_threshold = "moderate"
"""
        (tmp_path / "sova.toml").write_text(toml_content)
        cfg = load_config(tmp_path)
        assert cfg.spec.auto_approve_threshold == "moderate"

    def test_toml_loading(self, tmp_path: Path) -> None:
        toml_content = """
[project]
github_repo = "user/repo"

[spec]
auto_approve_threshold = "moderate"
"""
        (tmp_path / "sova.toml").write_text(toml_content)
        cfg = load_config(tmp_path)
        assert cfg.spec.auto_approve_threshold == "moderate"


class TestSupervisorAutoReview:
    def test_default_true(self) -> None:
        from sova.config.models import SupervisorConfig

        cfg = SupervisorConfig()
        assert cfg.auto_review is True

    def test_toml_loading(self, tmp_path: Path) -> None:
        toml_content = """
[project]
github_repo = "user/repo"

[supervisor]
auto_review = false
"""
        (tmp_path / "sova.toml").write_text(toml_content)
        cfg = load_config(tmp_path)
        assert cfg.supervisor.auto_review is False
