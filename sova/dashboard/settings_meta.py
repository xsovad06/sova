"""Settings metadata registry -- groups, labels, descriptions for config keys."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SettingMeta:
    key: str
    label: str
    description: str
    group: str
    value_type: str = "string"


_LABEL_POLL_INTERVAL = "Poll interval (s)"

GROUPS: dict[str, str] = {
    "project": "Project",
    "llm": "LLM Provider",
    "agent": "Agent",
    "pipeline": "Pipeline",
    "task_source": "Task Source",
    "review": "Code Review",
    "develop": "Development",
    "ci": "CI / CD",
    "watch": "Watch Mode",
    "worktree": "Worktrees",
    "commit": "Commits & PRs",
    "triage": "Triage",
    "roles": "Roles",
    "spec": "Specification",
    "server": "Server & Notifications",
    "external_reviews": "External Reviews",
    "security": "Security",
    "dashboard": "Dashboard",
    "integration": "Integration",
    "supervisor": "Supervisor",
    "agent_health": "Agent Health",
    "fleet": "Fleet & Telemetry",
    "awareness": "Awareness",
    "oversight": "Oversight Agent",
}

GROUP_ORDER: list[str] = [
    "project",
    "llm",
    "agent",
    "pipeline",
    "spec",
    "roles",
    "task_source",
    "commit",
    "review",
    "develop",
    "ci",
    "triage",
    "watch",
    "worktree",
    "server",
    "external_reviews",
    "security",
    "dashboard",
    "integration",
    "supervisor",
    "agent_health",
    "fleet",
    "awareness",
    "oversight",
]

_REGISTRY: list[SettingMeta] = [
    # -- Project --
    SettingMeta(
        "github_repo", "GitHub repository", "Owner/repo slug for GitHub API calls (e.g. user/my-project)", "project"
    ),
    SettingMeta("github_user", "GitHub user", "GitHub account to authenticate with for this project", "project"),
    SettingMeta("base_branch", "Base branch", "Default branch to branch from and merge into", "project"),
    SettingMeta("test_cmd", "Test command", "Shell command to run the test suite", "project"),
    SettingMeta("lint_cmd", "Lint command", "Shell command to run the linter", "project"),
    SettingMeta("format_cmd", "Format command", "Shell command to auto-format code", "project"),
    SettingMeta(
        "check_cmd",
        "Check command",
        "Full CI-equivalent command (e.g. make check). If empty, commands fall back to lint_cmd + test_cmd",
        "project",
    ),
    SettingMeta("persona_map", "Persona map", "Path to a custom persona mapping file", "project"),
    SettingMeta(
        "shared_knowledge_dir", "Shared knowledge dir", "Directory for cross-project shared knowledge files", "project"
    ),
    SettingMeta("invariants_dir", "Invariants dir", "Directory containing invariant check scripts", "project"),
    SettingMeta(
        "max_parallel_agents",
        "Max parallel agents",
        "Maximum number of agents that can run concurrently",
        "project",
        "number",
    ),
    SettingMeta(
        "scanner_github_check",
        "GitHub check scanner",
        "Post GitHub Check results after invariant scans",
        "project",
        "boolean",
    ),
    SettingMeta("slack_channel", "Slack channel", "Default Slack channel for notifications", "project"),
    # -- LLM Provider --
    SettingMeta(
        "llm.provider",
        "Provider",
        "LLM provider backend (claude-code, litellm, or hybrid for automatic local/cloud routing)",
        "llm",
    ),
    SettingMeta(
        "llm.model",
        "Model",
        "Default model for LiteLLM provider (e.g. claude-sonnet-4-6, gpt-4o, ollama/qwen3-coder)",
        "llm",
    ),
    SettingMeta(
        "llm.fallback_model",
        "Fallback model",
        "Fallback model if primary fails (LiteLLM only)",
        "llm",
    ),
    SettingMeta(
        "llm.api_base",
        "API base URL",
        "Base URL for LiteLLM proxy mode (leave empty for direct API calls)",
        "llm",
    ),
    SettingMeta(
        "llm.routing",
        "Model routing",
        "Model routing by complexity tier or task type (values: haiku/sonnet/opus/ollama/model-name)",
        "llm",
        "object",
    ),
    SettingMeta(
        "llm.batch_eligible_tasks",
        "Batch-eligible tasks",
        "Task types routed through the Batch API when available (50% discount, async delivery)",
        "llm",
        "list",
    ),
    SettingMeta(
        "llm.batch_gcs_bucket",
        "Batch GCS bucket",
        "Google Cloud Storage bucket for Vertex AI batch I/O (required for Vertex backend)",
        "llm",
    ),
    SettingMeta(
        "llm.batch_gcs_prefix",
        "Batch GCS prefix",
        "Path prefix within the GCS bucket for batch files (default: sova-batch)",
        "llm",
    ),
    SettingMeta(
        "llm.batch_poll_interval",
        "Batch poll interval",
        "Seconds between polling for batch completion (default 60)",
        "llm",
        "number",
    ),
    SettingMeta(
        "llm.batch_timeout",
        "Batch timeout",
        "Maximum seconds to wait for batch completion (default 86400 = 24h)",
        "llm",
        "number",
    ),
    # -- Agent --
    SettingMeta(
        "agent.runtime",
        "Agent runtime",
        "Coding agent backend to use (see sova.toml [agent] for available runtimes)",
        "agent",
    ),
    SettingMeta("agent.model", "Model", "Claude model to use for agent work (opus, sonnet, haiku)", "agent"),
    SettingMeta(
        "agent.max_budget", "Max budget (USD)", "Maximum spend per agent run before auto-abort", "agent", "number"
    ),
    SettingMeta(
        "agent.max_issue_budget",
        "Max issue budget (USD)",
        "Maximum cumulative spend across all runs for a single issue",
        "agent",
        "number",
    ),
    SettingMeta(
        "agent.step_timeout",
        "Step timeout (s)",
        "Maximum seconds per pipeline step before killing the agent (must be > 0)",
        "agent",
        "number",
    ),
    SettingMeta(
        "agent.skip_manual_test",
        "Skip manual test",
        "Skip the manual testing step in the developer pipeline",
        "agent",
        "boolean",
    ),
    SettingMeta(
        "agent.auto_approve_fixes",
        "Auto-approve fixes",
        "Automatically approve review fix suggestions without human confirmation",
        "agent",
        "boolean",
    ),
    # -- Pipeline --
    SettingMeta(
        "pipeline.auto_handoff",
        "Auto-handoff to reviewer",
        "Automatically spawn Reviewer after Developer creates a PR and CI passes",
        "pipeline",
        "boolean",
    ),
    SettingMeta(
        "pipeline.auto_address_review",
        "Auto-address review findings",
        "Automatically spawn Developer to fix Reviewer findings",
        "pipeline",
        "boolean",
    ),
    SettingMeta(
        "pipeline.max_address_review_cycles",
        "Max address-review cycles",
        "Maximum auto address-review runs per PR before requiring manual intervention (0 = unlimited)",
        "pipeline",
        "number",
    ),
    # -- Spec --
    SettingMeta(
        "spec.threshold",
        "Complexity threshold",
        "Minimum task complexity to trigger spec generation (always/trivial/simple/moderate/complex/never)",
        "spec",
    ),
    SettingMeta(
        "spec.auto_approve_simple",
        "Auto-approve simple specs",
        "Automatically approve specs for simple tasks with no open questions",
        "spec",
        "boolean",
    ),
    # -- Task Source --
    SettingMeta("task_source.type", "Source type", "Where tasks come from (github, jira, linear)", "task_source"),
    SettingMeta("task_source.config", "Config", "Source-specific configuration string", "task_source"),
    SettingMeta("task_source.milestone", "Milestone filter", "Only process issues in this milestone", "task_source"),
    SettingMeta(
        "task_source.labels", "Label filter", "Only process issues with these labels (comma-separated)", "task_source"
    ),
    SettingMeta(
        "task_source.github_project_number",
        "Project board number",
        "GitHub Projects V2 board number (0 = disabled)",
        "task_source",
        "number",
    ),
    SettingMeta(
        "task_source.jira_api_token",
        "JIRA API token",
        "API token for JIRA authentication (Atlassian account settings)",
        "task_source",
        "secret",
    ),
    SettingMeta("task_source.jira_base_url", "JIRA base URL", "Base URL for JIRA API calls", "task_source"),
    SettingMeta("task_source.jira_email", "JIRA email", "Email for JIRA authentication", "task_source"),
    SettingMeta("task_source.jira_project_key", "JIRA project key", "Jira project key (e.g. RHCLOUD)", "task_source"),
    SettingMeta("task_source.jira_component", "JIRA component", "Filter issues by Jira component name", "task_source"),
    SettingMeta(
        "task_source.jira_jql_filter",
        "JIRA JQL filter",
        "Additional JQL clause appended to queries (e.g. assignee = currentUser())",
        "task_source",
    ),
    SettingMeta(
        "task_source.jira_status_mapping",
        "JIRA status mapping",
        'Map JIRA board status names to SOVA states (e.g. ON_QA = "done", Selected for Development = "triaged")',
        "task_source",
        "object",
    ),
    SettingMeta(
        "task_source.jira_state_transitions",
        "JIRA state transitions",
        'Map SOVA states to JIRA transition names for status changes (e.g. in_progress = "Start Work")',
        "task_source",
        "object",
    ),
    SettingMeta(
        "task_source.jira_track_agent_work",
        "Track agent work (Jira)",
        "Create Jira sub-tasks to track agent activity on parent issues",
        "task_source",
        "boolean",
    ),
    # -- Review --
    SettingMeta("review.enabled", "Enabled", "Run automated code review after development", "review", "boolean"),
    SettingMeta("review.max_rounds", "Max rounds", "Maximum review-fix cycles before stopping", "review", "number"),
    SettingMeta(
        "review.panel.enabled",
        "Panel review",
        "Use parallel focused dimension reviewers instead of a single monolithic review (3-5x cost)",
        "review",
        "boolean",
    ),
    SettingMeta(
        "review.panel.dimensions",
        "Dimensions",
        "Review dimensions to evaluate in parallel (correctness, security, error_handling, design, test_coverage)",
        "review",
        "list",
    ),
    SettingMeta(
        "review.panel.dimension_models",
        "Dimension models",
        "Per-dimension model overrides (e.g. security=opus, test_coverage=haiku)",
        "review",
        "object",
    ),
    SettingMeta(
        "review.panel.line_proximity",
        "Line proximity",
        "Lines within this distance with the same category are deduplicated",
        "review",
        "number",
    ),
    # -- Develop Step --
    SettingMeta(
        "develop.max_fix_cycles",
        "Max fix cycles",
        "Maximum check-fix cycles after development before giving up (0 = disabled)",
        "develop",
        "number",
    ),
    SettingMeta(
        "develop.check_timeout",
        "Check timeout (s)",
        "Timeout in seconds for each check command execution",
        "develop",
        "number",
    ),
    SettingMeta(
        "develop.guard_test_weakening",
        "Guard test weakening",
        "Detect and reject LLM fixes that weaken test files instead of fixing code",
        "develop",
        "boolean",
    ),
    # -- CI --
    SettingMeta("ci.poll_interval", _LABEL_POLL_INTERVAL, "Seconds between CI status checks", "ci", "number"),
    SettingMeta("ci.max_wait", "Max wait (s)", "Maximum seconds to wait for CI to complete", "ci", "number"),
    SettingMeta(
        "ci.no_checks_grace_period",
        "Grace period (s)",
        "Seconds to wait before declaring 'no CI checks found'",
        "ci",
        "number",
    ),
    SettingMeta(
        "ci.max_fix_attempts",
        "Max fix attempts",
        "Maximum LLM-driven CI fix attempts before giving up (0 = disabled)",
        "ci",
        "number",
    ),
    SettingMeta(
        "ci.flaky_checks",
        "Flaky checks",
        "CI check names to ignore when they fail (comma-separated list)",
        "ci",
        "list",
    ),
    SettingMeta(
        "ci.exclude_checks",
        "Exclude checks",
        "Substring patterns for CI checks to skip entirely during polling (e.g. 'bonfire-tekton')",
        "ci",
        "list",
    ),
    # -- Watch --
    SettingMeta(
        "watch.interval_active",
        "Active interval (s)",
        "Seconds between polls when agents are running",
        "watch",
        "number",
    ),
    SettingMeta(
        "watch.interval_idle",
        "Idle interval (s)",
        "Seconds between polls when no agents are running",
        "watch",
        "number",
    ),
    SettingMeta(
        "watch.auto_select_issues",
        "Auto-select issues",
        "Automatically pick and assign issues from the backlog",
        "watch",
        "boolean",
    ),
    SettingMeta(
        "watch.veto_seconds",
        "Veto window (s)",
        "Seconds to wait for human veto before starting auto-selected work",
        "watch",
        "number",
    ),
    # -- Worktree --
    SettingMeta(
        "worktree.copy_files",
        "Copy files",
        "Files to copy into each new worktree (e.g. .env, .env.local)",
        "worktree",
        "list",
    ),
    SettingMeta(
        "worktree.ttl_done_days",
        "TTL done (days)",
        "Days to keep worktrees for completed runs before cleanup",
        "worktree",
        "number",
    ),
    SettingMeta(
        "worktree.ttl_paused_days",
        "TTL paused (days)",
        "Days to keep worktrees for paused runs before cleanup",
        "worktree",
        "number",
    ),
    # -- Commit --
    SettingMeta(
        "commit.format", "Commit format", "Commit message style: conventional (type(scope): msg) or freeform", "commit"
    ),
    SettingMeta("commit.ai_coauthor", "AI co-author", "Include AI co-author lines in commits", "commit", "boolean"),
    SettingMeta(
        "commit.author", "Author override", "Override the Git author for commits (empty = use git config)", "commit"
    ),
    SettingMeta("commit.pr_title_format", "PR title format", "PR title style: conventional or freeform", "commit"),
    SettingMeta(
        "commit.branch_naming",
        "Branch naming",
        "Branch name style: conventional (feat/fix/refactor) or freeform",
        "commit",
    ),
    SettingMeta(
        "commit.pr_auto_link_issues",
        "PR auto-link issues",
        "Automatically link issues in PR body via Closes #N",
        "commit",
        "boolean",
    ),
    # -- Triage --
    SettingMeta(
        "triage.mode",
        "Triage mode",
        "How triage writes results: full (label + body + transition), comment (post comment), dry_run (assess only)",
        "triage",
    ),
    SettingMeta(
        "triage.auto_label", "Auto-label", "Automatically apply suitability labels during triage", "triage", "boolean"
    ),
    SettingMeta(
        "triage.write_body",
        "Write to body",
        "Append assessment text to the issue description (in full mode)",
        "triage",
        "boolean",
    ),
    SettingMeta(
        "triage.write_transition",
        "Trigger transitions",
        "Move issues to TRIAGED state after assessment",
        "triage",
        "boolean",
    ),
    SettingMeta(
        "triage.min_confidence",
        "Min confidence",
        "Minimum confidence threshold (0-1) for auto-triage decisions",
        "triage",
        "number",
    ),
    SettingMeta(
        "triage.labels",
        "Custom labels",
        "Map suitability outcomes to custom label names (empty value = skip labeling for that outcome)",
        "triage",
        "object",
    ),
    SettingMeta(
        "triage.skip_title_prefixes",
        "Skip title prefixes",
        "Tickets with these title prefixes are auto-classified as human_only (e.g. [QE], [Spike])",
        "triage",
    ),
    SettingMeta(
        "triage.skip_labels",
        "Skip labels",
        "Tickets with any of these labels are auto-classified as human_only (e.g. post-mvp, QE)",
        "triage",
    ),
    SettingMeta(
        "triage.min_quality_score",
        "Min quality score",
        "Minimum issue body quality score (0-8) for agent:ready. Issues below this are blocked or enriched.",
        "triage",
        "number",
    ),
    SettingMeta(
        "triage.auto_enrich",
        "Auto-enrich",
        "Use a focused LLM call to add missing structural sections to low-quality issue bodies during triage",
        "triage",
        "boolean",
    ),
    # -- Roles --
    SettingMeta(
        "roles.default",
        "Default role",
        "Role to assign when none is specified (developer, researcher, triage)",
        "roles",
    ),
    SettingMeta("roles.researcher_model", "Researcher model", "Claude model for the researcher role", "roles"),
    SettingMeta("roles.triage_model", "Triage model", "Claude model for the triage role", "roles"),
    SettingMeta(
        "roles.nicknames", "Role nicknames", "Short aliases for role names (e.g. dev=developer)", "roles", "object"
    ),
    # -- Notifications --
    SettingMeta(
        "notification.desktop",
        "Desktop notifications",
        "Show native desktop notifications for agent events",
        "server",
        "boolean",
    ),
    SettingMeta(
        "notification.slack_webhook_url",
        "Slack webhook URL",
        "Incoming webhook URL for Slack notifications",
        "server",
        "secret",
    ),
    # -- Server --
    SettingMeta("server.host", "Host", "IP address or hostname to bind the dashboard server to", "server"),
    SettingMeta("server.port", "Port", "TCP port for the dashboard server", "server", "number"),
    SettingMeta("server.pid_file", "PID file", "Path to the server PID file (empty = default location)", "server"),
    SettingMeta(
        "server.scheduler_enabled",
        "Scheduler enabled",
        "Run the watch-loop scheduler alongside the dashboard",
        "server",
        "boolean",
    ),
    # -- External Reviews --
    SettingMeta(
        "external_reviews.enabled",
        "Enabled",
        "Enable external review tool integration (e.g. SonarCloud, CodeRabbit, Sourcery)",
        "external_reviews",
        "boolean",
    ),
    SettingMeta(
        "external_reviews.tools",
        "Review tools",
        "External review tools to poll (e.g. sonarcloud, coderabbit, sourcery)",
        "external_reviews",
        "list",
    ),
    SettingMeta(
        "external_reviews.poll_interval",
        _LABEL_POLL_INTERVAL,
        "Seconds between external review status checks",
        "external_reviews",
        "number",
    ),
    SettingMeta(
        "external_reviews.timeout",
        "Timeout (min)",
        "Minutes to wait for external reviews before proceeding",
        "external_reviews",
        "number",
    ),
    SettingMeta(
        "external_reviews.sonarcloud.project_key",
        "SonarCloud project key",
        "SonarCloud project key (e.g. org_repo)",
        "external_reviews",
    ),
    SettingMeta(
        "external_reviews.sonarcloud.coverage_threshold",
        "Coverage threshold",
        "Minimum coverage percentage on new code (must match SonarCloud quality gate)",
        "external_reviews",
        "number",
    ),
    # -- Security --
    SettingMeta(
        "egress.mode",
        "Egress filter mode",
        "Egress filter: off (passthrough), warn (redact and post), block (skip the post)",
        "security",
    ),
    SettingMeta(
        "security.prompt_guard",
        "Prompt injection guard",
        "Scan assembled LLM prompts for injection patterns before sending",
        "security",
        "boolean",
    ),
    SettingMeta(
        "security.prompt_guard_threshold",
        "Guard threshold",
        "Risk score threshold (0-1) above which prompts are blocked",
        "security",
        "number",
    ),
    SettingMeta(
        "security.custom_deny_patterns",
        "Custom deny patterns",
        "Additional regex patterns to flag as prompt injection (one per line)",
        "security",
        "list",
    ),
    # -- Dashboard --
    SettingMeta(
        "dashboard.kanban_columns",
        "Kanban column grouping",
        "How kanban columns are grouped: step_based (by pipeline step) or role_based (by role/status category)",
        "dashboard",
        "string",
    ),
    SettingMeta(
        "dashboard.gc_on_startup",
        "GC on startup",
        "Run issue-aware GC (worktree and branch cleanup) on dashboard startup (single-project mode only)",
        "dashboard",
        "boolean",
    ),
    SettingMeta(
        "dashboard.port",
        "Dashboard port",
        "TCP port the dashboard server listens on",
        "dashboard",
        "number",
    ),
    # -- Testing (in Development group) --
    SettingMeta(
        "testing.baseline_enabled",
        "Test baseline",
        "Capture a test baseline snapshot before development to detect regressions",
        "develop",
        "boolean",
    ),
    SettingMeta(
        "testing.baseline_timeout",
        "Baseline timeout (s)",
        "Maximum seconds for the baseline test suite run (must be > 0)",
        "develop",
        "number",
    ),
    # -- Output --
    SettingMeta(
        "output.retention_days",
        "Output retention (days)",
        "Days to keep agent output lines after run completion",
        "dashboard",
        "number",
    ),
    # -- Resource Monitoring (in Agent Health group) --
    SettingMeta(
        "monitoring.enabled",
        "Resource monitoring",
        "Collect CPU, memory, and I/O metrics for agent processes",
        "agent_health",
        "boolean",
    ),
    SettingMeta(
        "monitoring.interval",
        "Monitoring interval (s)",
        "Seconds between resource samples (must be > 0)",
        "agent_health",
        "number",
    ),
    SettingMeta(
        "monitoring.tdp_override",
        "TDP override (W)",
        "Override auto-detected chip TDP in watts for energy estimation (empty = auto-detect)",
        "agent_health",
        "number",
    ),
    SettingMeta(
        "monitoring.safety_margin",
        "Safety margin",
        "Fraction of CPU/memory to reserve when computing capacity recommendations (0.0-0.5)",
        "agent_health",
        "number",
    ),
    SettingMeta(
        "monitoring.co2_grams_per_kwh",
        "Carbon intensity (g CO2/kWh)",
        "Grid carbon intensity for CO2 emissions estimation (default 436 = world average)",
        "agent_health",
        "number",
    ),
    # -- Knowledge --
    SettingMeta(
        "knowledge.max_context_tokens",
        "Knowledge context tokens",
        "Token budget for relevance-filtered memory injection into agent prompts",
        "agent",
        "number",
    ),
    # -- Integration --
    SettingMeta(
        "integration_gates.ci_passed",
        "Require CI passed",
        "Block PR integration unless all CI checks have passed",
        "integration",
        "boolean",
    ),
    SettingMeta(
        "integration_gates.sova_reviewed",
        "Require SOVA review",
        "Block PR integration unless SOVA reviewer has approved",
        "integration",
        "boolean",
    ),
    SettingMeta(
        "integration_gates.coderabbit_reviewed",
        "Require CodeRabbit review",
        "Block PR integration unless CodeRabbit has reviewed (approved or commented)",
        "integration",
        "boolean",
    ),
    SettingMeta(
        "integration_gates.threads_resolved",
        "Require threads resolved",
        "Block PR integration unless all review conversation threads are resolved",
        "integration",
        "boolean",
    ),
    SettingMeta(
        "integration.merge_method",
        "Merge method",
        "PR merge strategy: auto (repo default), squash, rebase, or merge",
        "integration",
    ),
    SettingMeta(
        "integration.delete_branch",
        "Delete branch after merge",
        "Remove the remote branch after successful merge",
        "integration",
        "boolean",
    ),
    SettingMeta(
        "integration.merge_queue_enabled",
        "Merge queue",
        "Merge queue handling: auto (detect via GraphQL), true (always use), false (never use)",
        "integration",
    ),
    SettingMeta(
        "integration.merge_queue_poll_interval",
        "Queue poll interval (s)",
        "Seconds between merge queue status checks",
        "integration",
        "number",
    ),
    SettingMeta(
        "integration.merge_queue_timeout",
        "Queue timeout (s)",
        "Maximum seconds to wait for merge queue processing before timing out",
        "integration",
        "number",
    ),
    SettingMeta(
        "integration.post_merge_state",
        "Post-merge state",
        "Issue state after merge: done (close) or on_qa (add label, keep open). Jira accepts arbitrary state names.",
        "integration",
    ),
    # -- RTK --
    SettingMeta(
        "rtk.enabled",
        "RTK compression",
        "Inject RTK PreToolUse hook into .claude/settings.json during install (no-op when RTK is not installed)",
        "agent",
        "boolean",
    ),
    # -- CodeRabbit Quota --
    SettingMeta(
        "coderabbit_quota.enabled",
        "CodeRabbit quota tracking",
        "Track CodeRabbit review rate limits for PR throttling",
        "external_reviews",
        "boolean",
    ),
    SettingMeta(
        "coderabbit_quota.plan",
        "CodeRabbit plan",
        "CodeRabbit plan tier (free, pro, pro_plus): sets default reviews_per_hour",
        "external_reviews",
    ),
    SettingMeta(
        "coderabbit_quota.reviews_per_hour",
        "CR reviews per hour",
        "Maximum CodeRabbit reviews per rolling window (unset = derive from plan, 0 = unlimited)",
        "external_reviews",
        "number",
    ),
    SettingMeta(
        "coderabbit_quota.window_minutes",
        "CR quota window (min)",
        "Rolling window duration in minutes for rate limit tracking",
        "external_reviews",
        "number",
    ),
    # -- PR Monitor --
    SettingMeta(
        "pr_monitor.enabled",
        "PR monitor",
        "Enable background PR monitoring with state-change notifications and CodeRabbit auto-retry",
        "external_reviews",
        "boolean",
    ),
    SettingMeta(
        "pr_monitor.poll_interval",
        "PR monitor poll interval (s)",
        "Seconds between PR state polling cycles",
        "external_reviews",
        "number",
    ),
    SettingMeta(
        "pr_monitor.notify_on_approval",
        "Notify on approval",
        "Desktop notification when a PR is approved",
        "external_reviews",
        "boolean",
    ),
    SettingMeta(
        "pr_monitor.notify_on_changes_requested",
        "Notify on changes requested",
        "Desktop notification when a reviewer requests changes",
        "external_reviews",
        "boolean",
    ),
    SettingMeta(
        "pr_monitor.notify_on_ci_failure",
        "Notify on CI failure",
        "Desktop notification when CI fails on a PR",
        "external_reviews",
        "boolean",
    ),
    SettingMeta(
        "pr_monitor.notify_on_ready_to_merge",
        "Notify on ready to merge",
        "Desktop notification when a PR is approved with green CI",
        "external_reviews",
        "boolean",
    ),
    SettingMeta(
        "pr_monitor.auto_retry_coderabbit",
        "Auto-retry CodeRabbit",
        "Automatically request CodeRabbit re-review when rate limit expires",
        "external_reviews",
        "boolean",
    ),
    # -- Supervisor --
    SettingMeta(
        "supervisor.enabled",
        "Enabled",
        "Enable the dependency-aware task progression engine",
        "supervisor",
        "boolean",
    ),
    SettingMeta(
        "supervisor.auto_triage",
        "Auto-triage",
        "Automatically spawn triage agent for new backlog issues",
        "supervisor",
        "boolean",
    ),
    SettingMeta(
        "supervisor.auto_research",
        "Auto-research",
        "Automatically spawn researcher after triage when dependencies are met",
        "supervisor",
        "boolean",
    ),
    SettingMeta(
        "supervisor.auto_develop",
        "Auto-develop",
        "Automatically spawn developer after research (requires spec approval by default)",
        "supervisor",
        "boolean",
    ),
    SettingMeta(
        "supervisor.auto_address_review",
        "Auto-address review",
        "Automatically spawn address-review agent when SOVA review verdict is revise or block",
        "supervisor",
        "boolean",
    ),
    SettingMeta(
        "supervisor.auto_integrate",
        "Auto-integrate",
        "Automatically run integration pipeline when PR is approved",
        "supervisor",
        "boolean",
    ),
    SettingMeta(
        "supervisor.auto_rebase",
        "Auto-rebase",
        "Automatically rebase PRs with merge conflicts using LLM-assisted conflict resolution",
        "supervisor",
        "boolean",
    ),
    SettingMeta(
        "supervisor.require_approval",
        "Require approval",
        "Store actionable decisions for human review instead of executing immediately",
        "supervisor",
        "boolean",
    ),
    SettingMeta(
        "supervisor.respect_dependencies",
        "Respect dependencies",
        "Gate agent spawning on dependency graph (block until all deps are DONE)",
        "supervisor",
        "boolean",
    ),
    SettingMeta(
        "supervisor.respect_ownership",
        "Respect ownership",
        "Skip issues assigned to other users; claim unassigned issues on spawn",
        "supervisor",
        "boolean",
    ),
    SettingMeta(
        "supervisor.file_overlap_gate",
        "File overlap gate",
        "Defer tasks whose predicted file changes overlap with in-flight branches",
        "supervisor",
        "boolean",
    ),
    SettingMeta(
        "supervisor.file_overlap_threshold",
        "File overlap threshold",
        "Minimum overlap ratio (common files / union) to trigger the gate (0.0 = any overlap blocks)",
        "supervisor",
        "number",
    ),
    SettingMeta(
        "supervisor.poll_interval_seconds",
        _LABEL_POLL_INTERVAL,
        "Seconds between progression evaluation cycles",
        "supervisor",
        "number",
    ),
    SettingMeta(
        "supervisor.log_retention_days",
        "Log retention (days)",
        "Days to retain supervisor decision logs before purging",
        "supervisor",
        "number",
    ),
    SettingMeta(
        "supervisor.max_spawns_per_cycle",
        "Max spawns per cycle",
        "Maximum number of agents the supervisor spawns per poll cycle",
        "supervisor",
        "number",
    ),
    SettingMeta(
        "supervisor.max_researcher_failures",
        "Max researcher failures",
        "Block researcher spawn after this many consecutive failures (0 = unlimited)",
        "supervisor",
        "number",
    ),
    SettingMeta(
        "supervisor.ci_warn_minutes",
        "CI warn threshold (minutes)",
        "Warn in dashboard when remaining CI minutes drop below this value",
        "supervisor",
        "number",
    ),
    SettingMeta(
        "supervisor.ci_block_minutes",
        "CI block threshold (minutes)",
        "Block developer spawns when remaining CI minutes drop below this value",
        "supervisor",
        "number",
    ),
    SettingMeta(
        "supervisor.persona_path",
        "Persona path",
        "Override path for the supervisor persona file (default: ~/.config/sova/supervisor_persona.md)",
        "supervisor",
    ),
    SettingMeta(
        "supervisor.llm_planning",
        "LLM Planning",
        "Enable LLM-based planning before each supervisor cycle. "
        "Requires ANTHROPIC_API_KEY. When disabled, the supervisor runs in "
        "purely deterministic mode.",
        "supervisor",
        "boolean",
    ),
    SettingMeta(
        "supervisor.task_queue",
        "Task queue",
        "Ordered list of issue numbers for the supervisor to evaluate (empty = evaluate all)",
        "supervisor",
        "list",
    ),
    # -- Agent Health: Memory Guard --
    SettingMeta(
        "memory_guard.enabled",
        "Memory guard",
        "Check available system memory before spawning agents",
        "agent_health",
        "boolean",
    ),
    SettingMeta(
        "memory_guard.warn_threshold_gb",
        "Memory warn threshold (GB)",
        "Show a warning banner when available memory drops below this value",
        "agent_health",
        "number",
    ),
    SettingMeta(
        "memory_guard.block_threshold_gb",
        "Memory block threshold (GB)",
        "Block agent spawning when available memory drops below this value",
        "agent_health",
        "number",
    ),
    # -- Agent Health: Watchdog --
    SettingMeta(
        "watchdog.enabled",
        "Agent watchdog",
        "Detect stuck, zombie, and bypassed agent processes",
        "agent_health",
        "boolean",
    ),
    SettingMeta(
        "watchdog.check_interval_seconds",
        "Watchdog interval (s)",
        "Seconds between watchdog scan cycles",
        "agent_health",
        "number",
    ),
    SettingMeta(
        "watchdog.pipeline_adopt_timeout_minutes",
        "Pipeline adopt timeout (min)",
        "Minutes before a run with current_step='agent' is killed (pipeline never adopted)",
        "agent_health",
        "number",
    ),
    SettingMeta(
        "watchdog.no_output_warn_minutes",
        "No-output warn (min)",
        "Minutes without output before emitting a warning feed event",
        "agent_health",
        "number",
    ),
    SettingMeta(
        "watchdog.no_output_kill_minutes",
        "No-output kill (min)",
        "Minutes without output before killing the agent process",
        "agent_health",
        "number",
    ),
    SettingMeta(
        "watchdog.step_warn_minutes",
        "Step warn (min)",
        "Minutes on a single pipeline step before emitting a warning feed event",
        "agent_health",
        "number",
    ),
    SettingMeta(
        "watchdog.step_kill_minutes",
        "Step kill (min)",
        "Minutes on a single pipeline step before killing the agent process",
        "agent_health",
        "number",
    ),
    SettingMeta(
        "watchdog.cooldown_minutes",
        "Cooldown (min)",
        "Minutes between repeated alerts for the same run and signal type",
        "agent_health",
        "number",
    ),
    # -- Fleet & Telemetry --
    SettingMeta(
        "telemetry.hub_url",
        "Telemetry hub URL",
        "Remote hub URL to push run telemetry to (empty = disabled)",
        "fleet",
    ),
    SettingMeta(
        "telemetry.hub_token",
        "Telemetry hub token",
        "Bearer token for hub authentication (or set via SOVA_TELEMETRY_HUB_TOKEN env var)",
        "fleet",
        "secret",
    ),
    SettingMeta(
        "telemetry.machine_id",
        "Machine ID",
        "Unique machine identifier for telemetry (auto-derived from hostname+username if empty)",
        "fleet",
    ),
    SettingMeta(
        "fleet.cache_ttl_seconds",
        "Fleet cache TTL (s)",
        "Seconds to cache fleet-wide aggregation results before re-querying project databases",
        "fleet",
        "number",
    ),
    SettingMeta(
        "fleet.query_timeout_seconds",
        "Fleet query timeout (s)",
        "Maximum seconds to wait for a single project database query before skipping that project",
        "fleet",
        "number",
    ),
    SettingMeta(
        "fleet.telemetry_window_days",
        "Telemetry window (days)",
        "Number of days of remote telemetry data to include in fleet insights",
        "fleet",
        "number",
    ),
    SettingMeta(
        "fleet.sova_repo",
        "SOVA repository",
        "GitHub repository (owner/repo) where fleet-proposed issues are created",
        "fleet",
        "string",
    ),
    # -- Awareness --
    SettingMeta(
        "awareness.enabled",
        "Enabled",
        "Enable the awareness subsystem (briefing, providers)",
        "awareness",
        "boolean",
    ),
    SettingMeta(
        "awareness.providers",
        "Providers",
        "List of enabled provider names (gmail, gcal, reminders, pr_status, agent_runs)",
        "awareness",
        "list",
    ),
    SettingMeta(
        "awareness.gmail_token_path",
        "Gmail token path",
        "Path to Google OAuth token pickle (empty = default location)",
        "awareness",
    ),
    SettingMeta(
        "awareness.gmail_lookback_hours",
        "Gmail lookback (hours)",
        "How far back to fetch unread emails",
        "awareness",
        "number",
    ),
    SettingMeta(
        "awareness.gmail_ignore_labels",
        "Gmail ignore labels",
        "Gmail labels to exclude from briefing",
        "awareness",
        "list",
    ),
    SettingMeta(
        "awareness.gcal_calendars",
        "Calendar IDs",
        "Google Calendar IDs to include (primary = default calendar)",
        "awareness",
        "list",
    ),
    SettingMeta(
        "awareness.gcal_lookahead_hours",
        "Calendar lookahead (hours)",
        "How far ahead to show calendar events",
        "awareness",
        "number",
    ),
    SettingMeta(
        "awareness.reminders_lists",
        "Reminders lists",
        "Apple Reminders lists to include",
        "awareness",
        "list",
    ),
    SettingMeta(
        "awareness.pr_github_user",
        "GitHub user for PRs",
        "GitHub username for review-requested detection across projects",
        "awareness",
    ),
    # -- Oversight Agent --
    SettingMeta(
        "oversight.enabled",
        "Enabled",
        "Enable the oversight agent background daemon for autonomous project monitoring",
        "oversight",
        "boolean",
    ),
    SettingMeta(
        "oversight.wake_interval_minutes",
        "Wake interval (min)",
        "Minutes between oversight agent wake cycles (minimum 1)",
        "oversight",
        "number",
    ),
    SettingMeta(
        "oversight.auto_create_issues",
        "Auto-create issues",
        "Automatically create GitHub Issues from oversight findings above the confidence threshold",
        "oversight",
        "boolean",
    ),
    SettingMeta(
        "oversight.auto_triage",
        "Auto-triage",
        "Automatically run sova triage on global issues created by the oversight agent",
        "oversight",
        "boolean",
    ),
    SettingMeta(
        "oversight.confidence_threshold",
        "Confidence threshold",
        "Minimum confidence score (0.0 to 1.0) for a finding to be filed as an issue",
        "oversight",
        "number",
    ),
    SettingMeta(
        "oversight.persona_path",
        "Persona path",
        "Override path for the operations persona file (default: ~/.config/sova/operations_persona.md)",
        "oversight",
    ),
    SettingMeta(
        "oversight.analysis_model",
        "Analysis model",
        "LLM model tier for oversight analysis (e.g. sonnet, haiku)",
        "oversight",
    ),
    SettingMeta(
        "oversight.dedup_window_days",
        "Dedup window (days)",
        "Number of days to look back when deduplicating analysis findings (minimum 1)",
        "oversight",
        "number",
    ),
    SettingMeta(
        "oversight.analysis_timeout_seconds",
        "Analysis timeout (sec)",
        "Timeout in seconds for the LLM analysis call (minimum 10)",
        "oversight",
        "number",
    ),
]

_META_BY_KEY: dict[str, SettingMeta] = {m.key: m for m in _REGISTRY}


def get_meta(key: str) -> SettingMeta | None:
    """Look up metadata for a config key."""
    return _META_BY_KEY.get(key)


def get_grouped_config(flat_config: dict) -> list[dict]:
    """Transform a flat config dict into grouped sections with metadata.

    Returns a list of group dicts:
      [{"id": "agent", "label": "Agent", "settings": [...]}, ...]

    Each setting in a group has: key, label, description, value, value_type, raw_key.
    Settings without a registered group go into "other".
    """
    groups: dict[str, list[dict]] = {}

    for key in sorted(flat_config.keys()):
        if key == "_error":
            continue

        value = flat_config[key]
        meta = get_meta(key)

        if meta:
            group_id = meta.group
            entry = {
                "key": key,
                "label": meta.label,
                "description": meta.description,
                "value": value,
                "value_type": meta.value_type,
            }
        else:
            group_id = _infer_group(key)
            entry = {
                "key": key,
                "label": _humanize_key(key),
                "description": "",
                "value": value,
                "value_type": _infer_type(value),
            }

        groups.setdefault(group_id, []).append(entry)

    result = []
    for gid in GROUP_ORDER:
        if gid in groups:
            result.append(
                {
                    "id": gid,
                    "label": GROUPS.get(gid, gid.title()),
                    "settings": groups.pop(gid),
                }
            )

    for gid in sorted(groups.keys()):
        result.append(
            {
                "id": gid,
                "label": GROUPS.get(gid, gid.replace("_", " ").title()),
                "settings": groups[gid],
            }
        )

    return result


def _infer_group(key: str) -> str:
    """Infer group from dotted key prefix."""
    if "." in key:
        return key.split(".")[0]
    return "project"


def _humanize_key(key: str) -> str:
    """Convert a dotted config key to a human-readable label."""
    part = key.split(".")[-1]
    return part.replace("_", " ").capitalize()


def _infer_type(value: object) -> str:
    """Infer display type from Python value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "string"
