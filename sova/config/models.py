"""Pydantic configuration models for SOVA."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TaskSourceConfig(BaseSettings):
    """Task source configuration."""

    type: Literal["github", "jira", "linear", "manual"] = "github"
    config: str = ""

    # GitHub Issues filtering
    milestone: str = ""
    labels: str = ""

    # JIRA
    jira_base_url: str = ""
    jira_email: str = ""

    model_config = SettingsConfigDict(env_prefix="SOVA_TASK_")


class AgentConfig(BaseSettings):
    """Agent behavior configuration."""

    model: str = "opus"
    max_budget: Decimal = Decimal("10.00")
    skip_manual_test: bool = True
    auto_approve_fixes: bool = False

    model_config = SettingsConfigDict(env_prefix="SOVA_AGENT_")


class ReviewConfig(BaseSettings):
    """Automated review configuration."""

    enabled: bool = True
    max_rounds: int = 2

    model_config = SettingsConfigDict(env_prefix="SOVA_REVIEW_")


class CIConfig(BaseSettings):
    """CI monitoring configuration."""

    poll_interval: int = 60
    max_wait: int = 600
    flaky_checks: list[str] = Field(default_factory=list)

    model_config = SettingsConfigDict(env_prefix="SOVA_CI_")


class WatchConfig(BaseSettings):
    """Watch mode configuration."""

    interval_active: int = 300
    interval_idle: int = 1800
    auto_select_issues: bool = True
    veto_seconds: int = 30

    model_config = SettingsConfigDict(env_prefix="SOVA_WATCH_")


class WorktreeConfig(BaseSettings):
    """Worktree management configuration."""

    copy_files: list[str] = Field(default_factory=lambda: [".env", ".env.local"])
    ttl_done_days: int = 3
    ttl_paused_days: int = 7

    model_config = SettingsConfigDict(env_prefix="SOVA_WORKTREE_")


class CommitConfig(BaseSettings):
    """Commit and PR configuration."""

    format: Literal["conventional", "freeform"] = "conventional"
    no_ai_coauthor: bool = False
    author: str = ""
    pr_title_format: Literal["conventional", "freeform"] = "conventional"
    pr_auto_link_issues: bool = True
    branch_naming: Literal["conventional", "freeform"] = "conventional"

    model_config = SettingsConfigDict(env_prefix="SOVA_COMMIT_")


class TriageConfig(BaseSettings):
    """Triage role configuration."""

    auto_label: bool = True
    min_confidence: float = 0.7

    model_config = SettingsConfigDict(env_prefix="SOVA_TRIAGE_")


class ServerConfig(BaseSettings):
    """Server daemon configuration."""

    host: str = "127.0.0.1"
    port: int = 8111
    pid_file: str = ""
    scheduler_enabled: bool = True

    model_config = SettingsConfigDict(env_prefix="SOVA_SERVER_")


class NotificationConfig(BaseSettings):
    """Notification configuration for human-in-the-loop."""

    desktop: bool = False
    slack_webhook_url: str = ""

    model_config = SettingsConfigDict(env_prefix="SOVA_NOTIFICATION_")


class RolesConfig(BaseSettings):
    """Agent roles configuration."""

    default: str = "developer"
    researcher_model: str = "opus"
    triage_model: str = "sonnet"
    nicknames: dict[str, str] = Field(default_factory=dict)

    model_config = SettingsConfigDict(env_prefix="SOVA_ROLES_")


class ProjectConfig(BaseSettings):
    """Root configuration model for a SOVA project.

    Loaded from sova.toml, with env var overrides (SOVA_ prefix).
    """

    # Project settings
    github_repo: str = ""
    github_user: str = ""
    base_branch: str = "main"
    test_cmd: str = "make test"
    lint_cmd: str = "make lint"
    format_cmd: str = "make format"

    # Persona
    persona_map: str = ""

    # Knowledge
    shared_knowledge_dir: str = "~/.claude/shared-knowledge"
    invariants_dir: str = ""
    max_parallel_agents: int = 2

    # Scanner
    scanner_github_check: bool = True

    # Notifications
    slack_channel: str = ""

    # Nested config sections
    task_source: TaskSourceConfig = Field(default_factory=TaskSourceConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    ci: CIConfig = Field(default_factory=CIConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    worktree: WorktreeConfig = Field(default_factory=WorktreeConfig)
    commit: CommitConfig = Field(default_factory=CommitConfig)
    triage: TriageConfig = Field(default_factory=TriageConfig)
    roles: RolesConfig = Field(default_factory=RolesConfig)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    model_config = SettingsConfigDict(env_prefix="SOVA_")

    @property
    def shared_knowledge_path(self) -> Path:
        return Path(self.shared_knowledge_dir).expanduser()
