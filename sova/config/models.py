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
        "on_qa",
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
    jira_display_name: str = ""

    @property
    def is_jira(self) -> bool:
        return self.type == "jira" and bool(self.jira_project_key) and bool(self.jira_base_url)

    def jira_issue_key(self, issue_number: str) -> str:
        """Return the full JIRA issue key, e.g. ``RHCLOUD-48767``."""
        return f"{self.jira_project_key}-{issue_number}"

    def jira_browse_url(self, issue_number: str) -> str:
        """Return the JIRA browse URL for an issue."""
        return f"{self.jira_base_url.rstrip('/')}/browse/{self.jira_issue_key(issue_number)}"

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

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_TASK_")


class LLMConfig(BaseSettings):
    """LLM provider configuration."""

    provider: Literal["claude-code", "litellm", "hybrid"] = "claude-code"
    model: str = ""
    fallback_model: str = ""
    api_base: str = ""
    routing: dict[str, str] = Field(default_factory=dict)
    batch_eligible_tasks: list[str] = Field(default_factory=lambda: ["triage", "triage_enrich"])
    batch_gcs_bucket: str = ""
    batch_gcs_prefix: str = "sova-batch"
    batch_poll_interval: int = Field(60, gt=0)
    batch_timeout: int = Field(86400, gt=0)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_LLM_")

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
    fallback_models: list[str] = Field(default_factory=list)
    max_budget: Decimal = Field(Decimal("10.00"), gt=0)
    max_issue_budget: Decimal = Field(Decimal("50.00"), gt=0)
    step_timeout: int = Field(1800, gt=0)
    skip_manual_test: bool = True
    auto_approve_fixes: bool = False

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_AGENT_")


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

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_REVIEW_PANEL_")

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

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_REVIEW_")


class DevelopConfig(BaseSettings):
    """Inner check loop configuration for the develop step."""

    max_fix_cycles: int = Field(3, ge=0)
    check_timeout: int = Field(300, gt=0)
    guard_test_weakening: bool = True
    max_fix_time: int = Field(600, gt=0)
    fix_timeout: int = Field(180, gt=0)
    step_timeout: int = Field(1200, gt=0)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_DEVELOP_")


class CIConfig(BaseSettings):
    """CI monitoring configuration."""

    poll_interval: int = Field(60, gt=0)
    max_wait: int = Field(1500, gt=0)
    no_checks_grace_period: int = Field(120, ge=0)
    max_fix_attempts: int = Field(2, ge=0)
    flaky_checks: list[str] = Field(default_factory=list)
    exclude_checks: list[str] = Field(default_factory=list)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_CI_")


class WatchConfig(BaseSettings):
    """Watch mode configuration."""

    interval_active: int = Field(300, gt=0)
    interval_idle: int = Field(1800, gt=0)
    auto_select_issues: bool = True
    veto_seconds: int = Field(30, gt=0)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_WATCH_")


class WorktreeConfig(BaseSettings):
    """Worktree management configuration."""

    copy_files: list[str] = Field(default_factory=lambda: [".env", ".env.local"])
    ttl_done_days: int = Field(3, gt=0)
    ttl_paused_days: int = Field(7, gt=0)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_WORKTREE_")


class CommitConfig(BaseSettings):
    """Commit and PR configuration."""

    format: Literal["conventional", "freeform"] = "conventional"
    ai_coauthor: bool = True
    author: str = ""
    pr_title_format: Literal["conventional", "freeform"] = "conventional"
    branch_naming: Literal["conventional", "freeform"] = "conventional"
    pr_auto_link_issues: bool = True

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_COMMIT_")


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

    # Quality gate
    min_quality_score: int = Field(4, ge=0, le=8)
    auto_enrich: bool = False

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_TRIAGE_")


class ServerConfig(BaseSettings):
    """Server daemon configuration."""

    host: str = "127.0.0.1"
    port: int = 8111
    pid_file: str = ""
    scheduler_enabled: bool = True

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_SERVER_")


class NotificationConfig(BaseSettings):
    """Notification configuration for human-in-the-loop."""

    desktop: bool = False
    slack_webhook_url: str = Field("", repr=False)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_NOTIFICATION_")


class RolesConfig(BaseSettings):
    """Agent roles configuration."""

    default: str = "developer"
    researcher_model: str = "opus"
    triage_model: str = "sonnet"
    nicknames: dict[str, str] = Field(default_factory=dict)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_ROLES_")


class SpecConfig(BaseSettings):
    """Specification step configuration."""

    threshold: Literal["always", "trivial", "simple", "moderate", "complex", "never"] = "moderate"
    auto_approve_simple: bool = True

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_SPEC_")


class PipelineConfig(BaseSettings):
    """Pipeline orchestration configuration."""

    auto_handoff: bool = True
    auto_address_review: bool = True
    max_address_review_cycles: int = Field(2, ge=0)

    # Deprecated: auto-retry system removed in v0.x. These fields are accepted
    # but have no effect so existing sova.toml files do not break.
    max_retries: int = Field(0, ge=0)
    retry_delay_seconds: int = Field(0, ge=0)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_PIPELINE_")


class SonarCloudConfig(BaseSettings):
    """SonarCloud-specific configuration."""

    project_key: str = ""
    coverage_threshold: Decimal = Decimal("80.0")

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_SONARCLOUD_")


class CodeRabbitConfig(BaseSettings):
    """CodeRabbit-specific configuration (uses gh CLI auth)."""

    trigger_review: bool = False

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_CODERABBIT_")


class ExternalReviewsConfig(BaseSettings):
    """External review tool integration for the developer pipeline."""

    enabled: bool = False
    tools: list[str] = Field(default_factory=list)
    poll_interval: int = Field(30, gt=0)
    timeout: int = Field(15, gt=0)

    sonarcloud: SonarCloudConfig = Field(default_factory=SonarCloudConfig)
    coderabbit: CodeRabbitConfig = Field(default_factory=CodeRabbitConfig)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_EXTERNAL_REVIEWS_")


class EgressConfig(BaseSettings):
    """Egress filter configuration for outbound text scanning."""

    mode: Literal["off", "warn", "block"] = "warn"

    model_config = SettingsConfigDict(extra="forbid", env_prefix="SOVA_EGRESS_")


class SecurityConfig(BaseSettings):
    """Prompt injection guard configuration."""

    prompt_guard: bool = True
    prompt_guard_threshold: float = Field(0.7, ge=0, le=1)
    custom_deny_patterns: list[str] = Field(default_factory=list)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_SECURITY_")


class KnowledgeConfig(BaseSettings):
    """Knowledge retrieval configuration."""

    max_context_tokens: int = Field(2000, ge=0)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_KNOWLEDGE_")


class DashboardConfig(BaseSettings):
    """Dashboard UI configuration."""

    kanban_columns: Literal["step_based", "role_based"] = "step_based"
    confirm_model: Literal["complex_only", "always", "never"] = "complex_only"
    gc_on_startup: bool = False
    port: int = 8111

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_DASHBOARD_")


class OutputConfig(BaseSettings):
    """Agent output storage configuration."""

    retention_days: int = Field(30, ge=1)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_OUTPUT_")


class TestingConfig(BaseSettings):
    """Test baseline and regression detection configuration."""

    baseline_enabled: bool = True
    baseline_timeout: int = Field(300, gt=0)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_TESTING_")


class MonitoringConfig(BaseSettings):
    """Resource monitoring configuration for agent processes."""

    enabled: bool = True
    interval: float = Field(5.0, gt=0)
    tdp_override: float | None = Field(None, gt=0, description="Override auto-detected TDP (watts)")
    safety_margin: float = Field(0.2, ge=0.0, le=0.5, description="Fraction of capacity to reserve")
    co2_grams_per_kwh: float = Field(
        436.0, ge=0.0, description="Grid carbon intensity (g CO2/kWh) for emissions estimation"
    )

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_MONITORING_")


class CodeRabbitQuotaConfig(BaseSettings):
    """CodeRabbit rate-limit quota tracking configuration."""

    enabled: bool = False
    plan: Literal["free", "pro", "pro_plus"] = "free"
    reviews_per_hour: int | None = Field(None, ge=0)
    window_minutes: int = Field(60, gt=0)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_CODERABBIT_QUOTA_")

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

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_INTEGRATION_GATES_")


class IntegrationConfig(BaseSettings):
    """Merge execution and post-merge behavior configuration."""

    merge_method: Literal["auto", "squash", "rebase", "merge"] = "auto"
    delete_branch: bool = True
    merge_queue_enabled: Literal["auto", "true", "false"] = "auto"
    merge_queue_poll_interval: int = Field(30, gt=0)
    merge_queue_timeout: int = Field(1800, gt=0)
    post_merge_state: str = "done"

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_INTEGRATION_")


class PRMonitorConfig(BaseSettings):
    """PR monitoring background loop configuration."""

    enabled: bool = False
    poll_interval: int = Field(120, gt=0)
    notify_on_approval: bool = True
    notify_on_changes_requested: bool = True
    notify_on_ci_failure: bool = True
    notify_on_ready_to_merge: bool = True
    auto_retry_coderabbit: bool = True

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_PR_MONITOR_")


class RTKConfig(BaseSettings):
    """RTK (context compression) integration configuration."""

    enabled: bool = True

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_RTK_")


class MemoryGuardConfig(BaseSettings):
    """Pre-spawn memory availability gate (dashboard + supervisor)."""

    enabled: bool = True
    warn_threshold_gb: float = Field(3.0, ge=1.0)
    block_threshold_gb: float = Field(1.5, ge=0.5)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_MEMORY_GUARD_")

    @model_validator(mode="after")
    def _block_below_warn(self) -> MemoryGuardConfig:
        if self.block_threshold_gb >= self.warn_threshold_gb:
            msg = (
                f"block_threshold_gb ({self.block_threshold_gb}) must be less than "
                f"warn_threshold_gb ({self.warn_threshold_gb})"
            )
            raise ValueError(msg)
        return self


class WatchdogConfig(BaseSettings):
    """Agent watchdog: detects stuck, zombie, and bypassed agent processes."""

    enabled: bool = False
    check_interval_seconds: int = Field(600, gt=0)
    pipeline_adopt_timeout_minutes: int = Field(5, gt=0)
    no_output_warn_minutes: int = Field(15, gt=0)
    no_output_kill_minutes: int = Field(25, gt=0)
    step_warn_minutes: int = Field(45, gt=0)
    step_kill_minutes: int = Field(60, gt=0)
    cooldown_minutes: int = Field(10, gt=0)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_WATCHDOG_")

    @model_validator(mode="after")
    def _validate_thresholds(self) -> WatchdogConfig:
        if self.no_output_kill_minutes <= self.no_output_warn_minutes:
            msg = (
                f"no_output_kill_minutes ({self.no_output_kill_minutes}) must be "
                f"greater than no_output_warn_minutes ({self.no_output_warn_minutes})"
            )
            raise ValueError(msg)
        if self.step_kill_minutes <= self.step_warn_minutes:
            msg = (
                f"step_kill_minutes ({self.step_kill_minutes}) must be "
                f"greater than step_warn_minutes ({self.step_warn_minutes})"
            )
            raise ValueError(msg)
        return self


class SupervisorConfig(BaseSettings):
    """Supervisor: dependency-aware task progression engine."""

    enabled: bool = False
    auto_triage: bool = False
    auto_research: bool = False
    auto_develop: bool = False
    auto_address_review: bool = False
    auto_integrate: bool = False
    auto_rebase: bool = False
    require_approval: bool = True
    respect_dependencies: bool = True
    respect_ownership: bool = True
    file_overlap_gate: bool = True
    file_overlap_threshold: float = Field(0.0, ge=0.0, le=1.0)
    max_spawns_per_cycle: int = Field(2, ge=1)
    poll_interval_seconds: int = Field(120, gt=0)
    log_retention_days: int = Field(30, gt=0)
    max_researcher_failures: int = Field(3, ge=0)
    ci_warn_minutes: int = Field(200, ge=0)
    ci_block_minutes: int = Field(50, ge=0)
    persona_path: str = ""
    llm_planning: bool = False
    auto_queue: bool = True
    max_queue_size: int = Field(10, ge=0)
    task_queue: list[int] = Field(default_factory=list, json_schema_extra={"items": {"exclusiveMinimum": 0}})

    @field_validator("task_queue", mode="before")
    @classmethod
    def _validate_task_queue_items(cls, v: list[int]) -> list[int]:
        if not isinstance(v, list):
            return v
        for item in v:
            if not isinstance(item, int) or item <= 0:
                msg = f"task_queue items must be positive integers, got {item!r}"
                raise ValueError(msg)
        return v

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_SUPERVISOR_")


class OversightConfig(BaseSettings):
    """Oversight agent configuration."""

    enabled: bool = False
    wake_interval_minutes: int = Field(60, ge=1)
    auto_create_issues: bool = False
    auto_triage: bool = False
    confidence_threshold: float = Field(0.7, ge=0.0, le=1.0)
    persona_path: str = ""
    analysis_model: str = "sonnet"
    dedup_window_days: int = Field(14, ge=1)
    analysis_timeout_seconds: int = Field(120, ge=10)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_OVERSIGHT_")


class TelemetryConfig(BaseSettings):
    """Outbound telemetry push to a remote hub after pipeline finalization."""

    hub_url: str = ""
    hub_token: str = Field("", repr=False)
    machine_id: str = ""

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_TELEMETRY_")


class FleetConfig(BaseSettings):
    """Fleet insights: cross-project aggregation from local SOVA databases."""

    cache_ttl_seconds: int = Field(300, gt=0)
    query_timeout_seconds: int = Field(10, gt=0)
    telemetry_window_days: int = Field(90, gt=0)
    sova_repo: str = "xsovad06/sova"

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_FLEET_")


class AwarenessConfig(BaseSettings):
    """Awareness subsystem configuration (briefing, email, calendar)."""

    enabled: bool = False
    providers: list[str] = Field(default_factory=list)

    # Gmail
    gmail_token_path: str = ""
    gmail_lookback_hours: int = Field(24, gt=0)
    gmail_ignore_labels: list[str] = Field(default_factory=lambda: ["SPAM", "TRASH"])

    # Google Calendar
    gcal_calendars: list[str] = Field(default_factory=lambda: ["primary"])
    gcal_lookahead_hours: int = Field(36, gt=0)

    # Apple Reminders
    reminders_lists: list[str] = Field(default_factory=lambda: ["Reminders"])

    # PR status (cross-project)
    pr_github_user: str = ""

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_AWARENESS_")


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
    integration: IntegrationConfig = Field(default_factory=IntegrationConfig)
    rtk: RTKConfig = Field(default_factory=RTKConfig)
    coderabbit_quota: CodeRabbitQuotaConfig = Field(default_factory=CodeRabbitQuotaConfig)
    pr_monitor: PRMonitorConfig = Field(default_factory=PRMonitorConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    memory_guard: MemoryGuardConfig = Field(default_factory=MemoryGuardConfig)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    fleet: FleetConfig = Field(default_factory=FleetConfig)
    awareness: AwarenessConfig = Field(default_factory=AwarenessConfig)
    oversight: OversightConfig = Field(default_factory=OversightConfig)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="SOVA_")

    @property
    def shared_knowledge_path(self) -> Path:
        return Path(self.shared_knowledge_dir).expanduser()
