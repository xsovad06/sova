# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-08-19

Initial public release.

### Agent Pipeline

- Role-based architecture with four specialized agent types: Triage, Researcher, Developer, and Reviewer, plus custom DAG-based roles
- Developer pipeline: 16 gate-checked steps from sync to handoff-to-reviewer
- Address-review pipeline: 10 steps from rebase to handoff-to-user
- Researcher pipeline: 4 steps (fetch-task, research, spec, extract-memory)
- Planner pipeline: 4 steps (scan-project, generate-tasks, validate-tasks, extract-memory)
- Autonomous Developer-Reviewer chaining with configurable circuit breaker to prevent infinite loops
- Worktree isolation for parallel-safe task execution (multiple issues developed concurrently)
- Gate checks between every pipeline step to catch problems early
- CI failure auto-recovery: agents detect and fix failing CI checks automatically
- LLM-assisted rebase with merge conflict resolution
- Per-run and per-issue budget limits to prevent runaway costs

### CLI

- 19 standalone commands and 5 command groups (43 total subcommands)
- Core workflow: `sova run`, `sova triage`, `sova harden`, `sova watch`, `sova parallel`
- Server management: `sova server start|stop|status`
- Project setup: `sova install`, `sova setup`, `sova uninstall`, `sova init-db`, `sova doctor`
- PR operations: `sova address-pr`, `sova maintain-pr`, `sova review-pr`, `sova learn-from-pr`
- Monitoring: `sova status`, `sova costs`, `sova config`, `sova dashboard`
- Knowledge management: `sova memory search|prune|export|import|health|consolidate`
- Command distribution: `sova commands list|diff|update|sync|drift|backport`
- Supervisor control: `sova supervisor status|poll`
- MCP server: `sova mcp serve`

### Dashboard

- 24-page web UI with Catppuccin Mocha dark theme and Tailwind CSS
- 27 API routers and 39 backend services
- Multi-project support with per-request config isolation via URL routing (`/p/{slug}/`)
- Real-time agent output streaming via SSE
- Multi-agent control panel: start, stop, view logs, handoff actions
- Issue lifecycle visualization (development through post-merge)
- Per-task and per-model cost tracking and aggregation
- Batch operations for triage and hardening with parallel concurrency
- Supervisor dashboard with dependency graph and task progression engine
- Fleet management page for multi-machine deployments
- Oversight agent findings view
- PR metrics and timeline analysis
- Spec management and approval workflow
- Custom role editor with visual DAG builder
- Settings management with runtime configuration
- Project onboarding wizard with directory browser
- Style guide with live component examples

### Integrations

- GitHub Issues with Projects V2 board integration (full support)
- Jira Cloud with lifecycle enrichment and JQL-based task filtering
- CodeRabbit review quota tracking and automatic CHANGES_REQUESTED dismissal
- Configurable CodeRabbit review trigger after PR creation
- A2A (Agent-to-Agent) protocol for inter-agent communication

### Knowledge System

- 4-tier knowledge management: project rules, agent memory, session memory, shared knowledge
- 28 distributable commands with SHA-256 manifest tracking and conflict detection
- 5 persona auto-detections: Django, FastAPI, Odoo, PatternFly, Python
- 6 distributable guideline templates
- 7 pre-push invariant checks (bash)
- Command and skill distribution system (`sova commands install|update|diff`)

### Scheduler

- Priority-based watch loop with configurable poll intervals
- Parallel executor with asyncio.Semaphore for concurrent agent management
- Combined server mode: dashboard and scheduler in one process (`sova server start`)
- VM deployment support: `sova server install-service` for systemd/launchd, `sova server restart`, `sova server digest`
- systemd service file for Linux deployments
- launchd plist for macOS deployments

### Supervisor

- TaskProgressionEngine: deterministic state machine with 14 gate checks
- Dependency graph engine with topological sort and epic container support
- GitHub API rate limit tracking with identity-keyed cooldown
- CodeRabbit quota tracking to avoid exceeding review limits
- CI budget tracking via GitHub Actions billing API
- Memory pressure gate to pause spawning under resource constraints
- LLM-assisted planning mode (optional, Anthropic Sonnet)
- Auto-retry for recoverable pipeline failures with configurable limits
- Per-issue handoff files for parallel agent isolation

### Database

- 25 ORM models with async SQLAlchemy 2.0
- 31 Alembic migrations with self-healing fallback
- SQLite default (zero-config), PostgreSQL optional
- TaskRun, StepExecution, FailureRecord, CostRecord for full pipeline observability
- IssueLifecycle and LifecyclePhaseRecord for cross-run issue tracking

### Infrastructure

- CI pipeline: lint (ShellCheck + Ruff), tests (pytest), invariants
- Apache 2.0 license
- MCP server for provider-agnostic tool integration
- LiteLLM multi-provider integration with automatic fallback
- Anthropic Batch API support (Vertex AI and direct)
- Resource monitoring via psutil (CPU, memory, I/O per agent)
- Desktop notifications via terminal-notifier (macOS) with JXA fallback
- Slack notifications (optional)

### Known Limitations

- Alpha status: API surface is not yet stable
- Claude Code CLI dependency for full pipeline execution
- GitHub Issues fully supported; Jira Cloud supported with partial lifecycle; Linear planned
- Single-developer focus (no multi-user team coordination)
- No PyPI distribution; install from source via git clone
- macOS primary; Linux supported for server deployments; Windows untested
- SQLite default is suitable for single-developer use; PostgreSQL requires manual configuration
