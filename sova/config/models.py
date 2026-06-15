"""Pydantic configuration models for SOVA."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_VALID_TASK_STATES = frozenset(
    {
        "backlog",
        "triaged",
        "researched",
        "in_progress",
        "in_review",
        "done",
        "needs_spec",
        "human_only",
    }
)


class TaskSourceConfig(BaseSettings):
    """Task source configuration."""

    type: Literal["github", "jira"] = "github"
    config: str = ""

    # GitHub Issues filtering
    milestone: str = ""
    labels: str = ""

    # GitHub Projects V2 board integration (0 = disabled)
    github_project_number: int = 0

    # Jira Cloud
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = Field("", repr=False)
    jira_project_key: str = ""
    jira_component: str = ""
    jira_jql_filter: str = ""
    jira_state_transitions: dict[str, str] = Field(default_factory=dict)
    jira_status_mapping: dict[str, str] = Field(default_factory=dict)

    @field_validator("jira_status_mapping")
    @classmethod
    def _validate_status_mapping(cls, v: dict[str, str]) -> dict[str, str]:
        for jira_status, sova_state in v.items():
            if sova_state not in _VALID_TASK_STATES:
                raise ValueError(
                    f"Invalid SOVA state {sova_state!r} for JIRA status {jira_status!r}. "
                    f"Valid states: {', '.join(sorted(_VALID_TASK_STATES))}"
                )
        return v

    model_config = SettingsConfigDict(env_prefix="SOVA_TASK_")


class LLMConfig(BaseSettings):
    """LLM provider configuration."""

    provider: Literal["claude-code", "litellm"] = "claude-code"
    model: str = ""
    fallback_model: str = ""
    api_base: str = ""

    model_config = SettingsConfigDict(env_prefix="SOVA_LLM_")

    @model_validator(mode="after")
    def _default_model_for_litellm(self) -> LLMConfig:
        """Ensure litellm provider always has an explicit model."""
        if self.provider == "litellm" and not self.model:
            self.model = "claude-sonnet-4-6"
        return self


class AgentConfig(BaseSettings):
    """Agent behavior configuration."""

    model: str = "opus"
    max_budget: Decimal = Field(Decimal("10.00"), gt=0)
    max_issue_budget: Decimal = Field(Decimal("50.00"), gt=0)
    step_timeout: int = Field(1800, gt=0)
    skip_manual_test: bool = True
    auto_approve_fixes: bool = False

    model_config = SettingsConfigDict(env_prefix="SOVA_AGENT_")


class ReviewConfig(BaseSettings):
    """Automated review configuration."""

    enabled: bool = True
    max_rounds: int = Field(2, gt=0)

    model_config = SettingsConfigDict(env_prefix="SOVA_REVIEW_")


class CIConfig(BaseSettings):
    """CI monitoring configuration."""

    poll_interval: int = Field(60, gt=0)
    max_wait: int = Field(900, gt=0)
    no_checks_grace_period: int = Field(120, ge=0)
    max_fix_attempts: int = Field(2, ge=0)
    flaky_checks: list[str] = Field(default_factory=list)

    model_config = SettingsConfigDict(env_prefix="SOVA_CI_")


class WatchConfig(BaseSettings):
    """Watch mode configuration."""

    interval_active: int = Field(300, gt=0)
    interval_idle: int = Field(1800, gt=0)
    auto_select_issues: bool = True
    veto_seconds: int = Field(30, gt=0)

    model_config = SettingsConfigDict(env_prefix="SOVA_WATCH_")


class WorktreeConfig(BaseSettings):
    """Worktree management configuration."""

    copy_files: list[str] = Field(default_factory=lambda: [".env", ".env.local"])
    ttl_done_days: int = Field(3, gt=0)
    ttl_paused_days: int = Field(7, gt=0)

    model_config = SettingsConfigDict(env_prefix="SOVA_WORKTREE_")


class CommitConfig(BaseSettings):
    """Commit and PR configuration."""

    format: Literal["conventional", "freeform"] = "conventional"
    ai_coauthor: bool = True
    author: str = ""
    pr_title_format: Literal["conventional", "freeform"] = "conventional"
    pr_auto_link_issues: bool = True
    branch_naming: Literal["conventional", "freeform"] = "conventional"

    model_config = SettingsConfigDict(env_prefix="SOVA_COMMIT_")


class TriageConfig(BaseSettings):
    """Triage role configuration."""

    auto_label: bool = True
    min_confidence: float = Field(0.7, ge=0, le=1)

    mode: Literal["full", "comment", "dry_run"] = "full"
    write_body: bool = True
    write_transition: bool = True
    labels: dict[str, str] = Field(default_factory=dict)
    skip_title_prefixes: list[str] = Field(default_factory=list)
    skip_labels: list[str] = Field(default_factory=list)

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


class PipelineConfig(BaseSettings):
    """Pipeline orchestration configuration."""

    auto_handoff: bool = True
    auto_address_review: bool = True
    max_address_review_cycles: int = Field(2, ge=0)

    model_config = SettingsConfigDict(env_prefix="SOVA_PIPELINE_")


class SonarCloudConfig(BaseSettings):
    """SonarCloud-specific configuration."""

    project_key: str = ""

    model_config = SettingsConfigDict(env_prefix="SOVA_SONARCLOUD_")


class CodeRabbitConfig(BaseSettings):
    """CodeRabbit-specific configuration (uses gh CLI auth)."""

    model_config = SettingsConfigDict(env_prefix="SOVA_CODERABBIT_")


class ExternalReviewsConfig(BaseSettings):
    """External review tool integration for the developer pipeline."""

    enabled: bool = False
    tools: list[str] = Field(default_factory=list)
    poll_interval: int = Field(30, gt=0)
    timeout: int = Field(15, gt=0)

    sonarcloud: SonarCloudConfig = Field(default_factory=SonarCloudConfig)
    coderabbit: CodeRabbitConfig = Field(default_factory=CodeRabbitConfig)

    model_config = SettingsConfigDict(env_prefix="SOVA_EXTERNAL_REVIEWS_")


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
    shared_knowledge_categories: list[str] = Field(
        default_factory=lambda: ["review_pattern", "common_mistake", "codebase_pattern", "ci_pattern"],
    )
    ignored_shared_hashes: list[str] = Field(default_factory=list)
    invariants_dir: str = ""
    max_parallel_agents: int = 2

    # Scanner
    scanner_github_check: bool = True

    # Notifications
    slack_channel: str = ""

    # Nested config sections
    llm: LLMConfig = Field(default_factory=LLMConfig)
    task_source: TaskSourceConfig = Field(default_factory=TaskSourceConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    ci: CIConfig = Field(default_factory=CIConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    worktree: WorktreeConfig = Field(default_factory=WorktreeConfig)
    commit: CommitConfig = Field(default_factory=CommitConfig)
    triage: TriageConfig = Field(default_factory=TriageConfig)
    roles: RolesConfig = Field(default_factory=RolesConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    external_reviews: ExternalReviewsConfig = Field(default_factory=ExternalReviewsConfig)

    model_config = SettingsConfigDict(env_prefix="SOVA_")

    @property
    def shared_knowledge_path(self) -> Path:
        return Path(self.shared_knowledge_dir).expanduser()
