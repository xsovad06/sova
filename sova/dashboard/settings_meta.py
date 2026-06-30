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


GROUPS: dict[str, str] = {
    "project": "Project",
    "llm": "LLM Provider",
    "agent": "Agent",
    "pipeline": "Pipeline",
    "task_source": "Task Source",
    "review": "Code Review",
    "ci": "CI / CD",
    "watch": "Watch Mode",
    "worktree": "Worktrees",
    "commit": "Commits & PRs",
    "triage": "Triage",
    "roles": "Roles",
    "spec": "Specification",
    "notification": "Notifications",
    "server": "Server",
    "external_reviews": "External Reviews",
    "egress": "Egress Filter",
    "security": "Security",
    "dashboard": "Dashboard",
    "output": "Output Storage",
    "monitoring": "Resource Monitoring",
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
    "ci",
    "triage",
    "watch",
    "worktree",
    "notification",
    "server",
    "external_reviews",
    "egress",
    "security",
    "dashboard",
    "output",
    "monitoring",
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
    # -- CI --
    SettingMeta("ci.poll_interval", "Poll interval (s)", "Seconds between CI status checks", "ci", "number"),
    SettingMeta("ci.max_wait", "Max wait (s)", "Maximum seconds to wait for CI to complete", "ci", "number"),
    SettingMeta(
        "ci.no_checks_grace_period",
        "Grace period (s)",
        "Seconds to wait before declaring 'no CI checks found'",
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
        "commit.pr_auto_link_issues",
        "Auto-link issues in PR",
        "Add 'Closes #N' to PR bodies automatically",
        "commit",
        "boolean",
    ),
    SettingMeta(
        "commit.branch_naming",
        "Branch naming",
        "Branch name style: conventional (feat/fix/refactor) or freeform",
        "commit",
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
    # -- Notification --
    SettingMeta(
        "notification.desktop",
        "Desktop notifications",
        "Show native desktop notifications for agent events",
        "notification",
        "boolean",
    ),
    SettingMeta(
        "notification.slack_webhook_url",
        "Slack webhook URL",
        "Incoming webhook URL for Slack notifications",
        "notification",
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
        "Poll interval (s)",
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
    # -- Egress Filter --
    SettingMeta(
        "egress.mode",
        "Filter mode",
        "Egress filter mode: off (passthrough), warn (redact and post), block (skip the post)",
        "egress",
    ),
    # -- Security --
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
    # -- Output Storage --
    SettingMeta(
        "output.retention_days",
        "Retention (days)",
        "Days to keep agent output lines after run completion",
        "output",
        "number",
    ),
    # -- Resource Monitoring --
    SettingMeta(
        "monitoring.enabled",
        "Enabled",
        "Collect CPU, memory, and I/O metrics for agent processes",
        "monitoring",
        "boolean",
    ),
    SettingMeta(
        "monitoring.interval",
        "Sample interval (s)",
        "Seconds between resource samples (must be > 0)",
        "monitoring",
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
