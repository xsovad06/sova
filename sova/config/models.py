"""Pydantic configuration models for SOVA."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import ClassVar, Literal

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
    jira_track_agent_work: bool = False

    @property
    def is_jira(self) -> bool:
        return self.type == "jira" and bool(self.jira_project_key) and bool(self.jira_base_url)

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

    provider: Literal["claude-code", "litellm", "hybrid"] = "claude-code"
    model: str = ""
    fallback_model: str = ""
    api_base: str = ""
    routing: dict[str, str] = Field(default_factory=dict)

    model_config = SettingsConfigDict(env_prefix="SOVA_LLM_")

    @model_validator(mode="after")
    def _default_model_for_litellm(self) -> LLMConfig:
        """Ensure litellm provider always has an explicit model."""
        if self.provider in ("litellm", "hybrid") and not self.model:
            self.model = "claude-sonnet-4-6"
        return self


class AgentConfig(BaseSettings):
    """Agent behavior configuration."""

    # Keep in sync with sova/ipc/runtime.py:_RUNTIMES registry
    runtime: Literal["claude-code", "aider"] = "claude-code"
    model: str = "opus"
    max_budget: Decimal = Field(Decimal("10.00"), gt=0)
    max_issue_budget: Decimal = Field(Decimal("50.00"), gt=0)
    step_timeout: int = Field(1800, gt=0)
    skip_manual_test: bool = True
    auto_approve_fixes: bool = False

    model_config = SettingsConfigDict(env_prefix="SOVA_AGENT_")


class ReviewPanelConfig(BaseSettings):
    """Panel review configuration -- sequential focused dimension reviewers."""

    KNOWN_DIMENSIONS: ClassVar[frozenset[str]] = frozenset(
        {"correctness", "security", "error_handling", "design", "test_coverage"}
    )

    enabled: bool = False
    dimensions: list[str] = Field(
        default_factory=lambda: ["correctness", "security", "error_handling", "design", "test_coverage"],
    )
    dimension_models: dict[str, str] = Field(default_factory=dict)
    line_proximity: int = Field(3, ge=0)

    model_config = SettingsConfigDict(env_prefix="SOVA_REVIEW_PANEL_")

    @model_validator(mode="after")
    def _validate_dimensions(self) -> ReviewPanelConfig:
        unknown = set(self.dimensions) - self.KNOWN_DIMENSIONS
        if unknown:
            import warnings

            warnings.warn(
                f"Unknown review panel dimensions: {sorted(unknown)}. "
                f"Known: {sorted(self.KNOWN_DIMENSIONS)}. "
                "Unknown dimensions will use a generic prompt.",
                UserWarning,
                stacklevel=2,
            )
        return self


class ReviewConfig(BaseSettings):
    """Automated review configuration."""

    enabled: bool = True
    max_rounds: int = Field(2, gt=0)
    panel: ReviewPanelConfig = Field(default_factory=ReviewPanelConfig)

    model_config = SettingsConfigDict(env_prefix="SOVA_REVIEW_")


class DevelopConfig(BaseSettings):
    """Inner check loop configuration for the develop step."""

    max_fix_cycles: int = Field(3, ge=0)
    check_timeout: int = Field(300, gt=0)
    guard_test_weakening: bool = True

    model_config = SettingsConfigDict(env_prefix="SOVA_DEVELOP_")


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
    slack_webhook_url: str = Field("", repr=False)

    model_config = SettingsConfigDict(env_prefix="SOVA_NOTIFICATION_")


class RolesConfig(BaseSettings):
    """Agent roles configuration."""

    default: str = "developer"
    researcher_model: str = "opus"
    triage_model: str = "sonnet"
    nicknames: dict[str, str] = Field(default_factory=dict)

    model_config = SettingsConfigDict(env_prefix="SOVA_ROLES_")


class SpecConfig(BaseSettings):
    """Specification step configuration."""

    threshold: Literal["always", "trivial", "simple", "moderate", "complex", "never"] = "moderate"
    auto_approve_simple: bool = True

    model_config = SettingsConfigDict(env_prefix="SOVA_SPEC_")


class PipelineConfig(BaseSettings):
    """Pipeline orchestration configuration."""

    auto_handoff: bool = True
    auto_address_review: bool = True
    max_address_review_cycles: int = Field(2, ge=0)

    model_config = SettingsConfigDict(env_prefix="SOVA_PIPELINE_")


class SonarCloudConfig(BaseSettings):
    """SonarCloud-specific configuration."""

    project_key: str = ""
    coverage_threshold: Decimal = Decimal("80.0")

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


class EgressConfig(BaseSettings):
    """Egress filter configuration for outbound text scanning."""

    mode: Literal["off", "warn", "block"] = "warn"

    model_config = SettingsConfigDict(env_prefix="SOVA_EGRESS_")


class SecurityConfig(BaseSettings):
    """Prompt injection guard configuration."""

    prompt_guard: bool = True
    prompt_guard_threshold: float = Field(0.7, ge=0, le=1)
    custom_deny_patterns: list[str] = Field(default_factory=list)

    model_config = SettingsConfigDict(env_prefix="SOVA_SECURITY_")


class KnowledgeConfig(BaseSettings):
    """Knowledge retrieval configuration."""

    max_context_tokens: int = Field(2000, ge=0)

    model_config = SettingsConfigDict(env_prefix="SOVA_KNOWLEDGE_")


class DashboardConfig(BaseSettings):
    """Dashboard UI configuration."""

    kanban_columns: Literal["step_based", "role_based"] = "step_based"
    pr_author_filter: Literal["mine", "all"] = "mine"

    model_config = SettingsConfigDict(env_prefix="SOVA_DASHBOARD_")


class OutputConfig(BaseSettings):
    """Agent output storage configuration."""

    retention_days: int = Field(30, ge=1)

    model_config = SettingsConfigDict(env_prefix="SOVA_OUTPUT_")


class TestingConfig(BaseSettings):
    """Test baseline and regression detection configuration."""

    baseline_enabled: bool = True
    baseline_timeout: int = Field(300, gt=0)

    model_config = SettingsConfigDict(env_prefix="SOVA_TESTING_")


class MonitoringConfig(BaseSettings):
    """Resource monitoring configuration for agent processes."""

    enabled: bool = False
    interval: float = Field(5.0, gt=0)

    model_config = SettingsConfigDict(env_prefix="SOVA_MONITORING_")


class CodeRabbitQuotaConfig(BaseSettings):
    """CodeRabbit rate-limit quota tracking configuration."""

    enabled: bool = False
    plan: Literal["free", "pro", "pro_plus"] = "free"
    reviews_per_hour: int | None = Field(None, ge=0)
    window_minutes: int = Field(60, gt=0)

    model_config = SettingsConfigDict(env_prefix="SOVA_CODERABBIT_QUOTA_")

    @model_validator(mode="after")
    def _apply_plan_defaults(self) -> CodeRabbitQuotaConfig:
        """Derive reviews_per_hour from plan when not explicitly set (None).

        Explicit 0 means unlimited (no quota enforcement).
        """
        if self.reviews_per_hour is None:
            _plan_defaults = {"free": 4, "pro": 0, "pro_plus": 0}
            self.reviews_per_hour = _plan_defaults.get(self.plan, 4)
        return self


class IntegrationGatesConfig(BaseSettings):
    """Configurable gates that must pass before PR integration is allowed."""

    ci_passed: bool = False
    sova_reviewed: bool = False
    coderabbit_reviewed: bool = False
    threads_resolved: bool = False

    model_config = SettingsConfigDict(env_prefix="SOVA_INTEGRATION_GATES_")


class RTKConfig(BaseSettings):
    """RTK (context compression) integration configuration."""

    enabled: bool = True

    model_config = SettingsConfigDict(env_prefix="SOVA_RTK_")


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
    check_cmd: str = ""

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
    develop: DevelopConfig = Field(default_factory=DevelopConfig)
    ci: CIConfig = Field(default_factory=CIConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    worktree: WorktreeConfig = Field(default_factory=WorktreeConfig)
    commit: CommitConfig = Field(default_factory=CommitConfig)
    triage: TriageConfig = Field(default_factory=TriageConfig)
    roles: RolesConfig = Field(default_factory=RolesConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    spec: SpecConfig = Field(default_factory=SpecConfig)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    external_reviews: ExternalReviewsConfig = Field(default_factory=ExternalReviewsConfig)
    egress: EgressConfig = Field(default_factory=EgressConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    testing: TestingConfig = Field(default_factory=TestingConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    integration_gates: IntegrationGatesConfig = Field(default_factory=IntegrationGatesConfig)
    rtk: RTKConfig = Field(default_factory=RTKConfig)
    coderabbit_quota: CodeRabbitQuotaConfig = Field(default_factory=CodeRabbitQuotaConfig)

    model_config = SettingsConfigDict(env_prefix="SOVA_")

    @property
    def shared_knowledge_path(self) -> Path:
        return Path(self.shared_knowledge_dir).expanduser()
