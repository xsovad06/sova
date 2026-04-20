# Architecture

## Component Overview

SOVA has four main components, plus a legacy bash agent during migration:

### 1. CLI (`sova/cli/`)
- Python CLI built with Typer, entry point `sova` (via pyproject.toml)
- Subcommands: `run`, `triage`, `install`, `setup`, `dashboard`, `server`, `commands`, `memory`, `status`, `costs`, `cleanup`, `address-pr`, `maintain-pr`, `review-pr`, `learn-from-pr`
- Registered in `sova/cli/app.py`, implementations in `sova/cli/commands/`

### 2. Agent Core (`sova/core/`, `sova/roles/`)
- `core/workflow.py` -- WorkflowEngine: executes 12-step pipeline with DB persistence (TaskRun, StepExecution, FailureRecord)
- `core/state.py` -- 16-state TaskStatus StrEnum with transition validation
- `core/context.py` -- ExecutionContext dataclass threading state through steps
- `core/steps/` -- 12 BaseStep implementations with execute/validate_output/can_skip
- `roles/` -- AgentRole ABC with 4 implementations: triage, researcher, developer, reviewer
- `roles/dispatcher.py` -- routes tasks to appropriate roles based on state

### 3. Dashboard (`sova/dashboard/`)
- Python/FastAPI web UI with app factory pattern (`create_app(project_dir=None)`)
- Jinja2 templates + Tailwind CSS (via CDN), Catppuccin dark theme
- 6 pages: overview, runs, run_detail, costs, control, memory
- 6 API routers under `/api`: overview, runs, costs, control, handoff, memory
- 5 services: run_service, cost_service, memory_service, control_service, handoff_service
- **Control service**: spawns Claude CLI processes, streams output, creates TaskRun + CostRecord DB entries
- **Handoff system**: agents write `.claude/agent-control/handoff.json` to pass state between agents
  - `handoff_service.py` -- read/write/archive handoff files (mtime-cached)
  - Dashboard renders handoff action buttons on the control page (awaiting_action/completed/failed)
  - Enables chaining: `ship-pr` -> `agent-resume` -> `approve-merge`
- **Claude command execution**: `control_service.start_command()` runs Claude Code commands from handoff actions
- Tests: `tests/test_dashboard.py` (pytest + httpx ASGITransport), run via `make test-py`

### 4. Scheduler (`sova/scheduler/`)
- `watch.py` -- WatchLoop: async poll with priority scan (RESEARCHED > TRIAGED > BACKLOG), veto window, asyncio.Event for shutdown
- `parallel.py` -- ParallelExecutor: asyncio.Semaphore for max_parallel_agents
- `server.py` -- SOVAServer: combined FastAPI dashboard + scheduler in one process, PID file lifecycle
- Deploy: `deploy/sova-server.service` (systemd) + `deploy/com.sova.server.plist` (launchd)
- CLI: `sova server start/stop/status`

### 5. Legacy Agent (`agent/`)
- `orchestrator.sh` -- the original bash autonomous agent (~3500 lines)
- `install.sh`, `setup-wizard.sh`, `detect-persona.sh`
- `adapters/` -- bash task source adapters (github, jira, linear, manual)
- Kept during migration; will be removed in Phase 6 (cutover, issue #45)

## Supporting Modules

- **`sova/adapters/`** -- TaskAdapter ABC + GitHub implementation (state via `agent:` labels), factory
- **`sova/llm/`** -- Claude CLI async wrapper (`client.py`), cost recording (`cost.py`), model routing
- **`sova/git/`** -- branch/commit/push/PR operations (`operations.py`), worktree lifecycle (`worktree.py`)
- **`sova/ipc/`** -- AgentProcess (spawn/stop/stream), AgentHandoff + DashboardHandoff models, notifications (desktop + Slack)
- **`sova/knowledge/`** -- Memory CRUD + search + promote, tier loading, persona detection, review patterns
- **`sova/commands/`** -- command distribution: catalog (discover + classify), templates (regex rendering), manifest (SHA-256 tracking), distribution (install/update/diff with conflict detection)
- **`sova/config/`** -- Pydantic Settings v2, TOML loader with legacy `.conf` fallback, project registry
- **`sova/db/`** -- SQLAlchemy 2.0 async ORM models (TaskRun, StepExecution, FailureRecord, CostRecord, Memory, TaskAssessmentRecord), session factory (SQLite default, PostgreSQL optional)

## Config System
- **SOVA config**: `sova.toml` per project (Pydantic Settings, env var overrides via `SOVA_` prefix)
- **Legacy config**: `agent/pak-agent.conf.default` template, per-project `.claude/scripts/pak-agent.conf` (shell-sourceable)
- **DB URL**: `SOVA_DATABASE_URL` env var for PostgreSQL; defaults to `.claude/sova.db` (SQLite)

## Naming Convention

The project's full name is **SOVA** (Software Orchestration Via Agents). Previously known as Project Automation Kit (PAK).

- **CLI command**: `sova`
- **PyPI package**: `sova`
- **Config files**: `sova.toml`, `sova.db`
- **Legacy**: `pak` CLI and `pak-agent.conf` still work during migration

## Development Workflow
- `Makefile` at repo root provides all development targets
- `make serve` -- start dashboard
- `make check` -- lint + test (CI-equivalent)
- `make test` -- bash (shellcheck + invariant --help) + python (pytest, 403+ tests)
- `make lint` -- shellcheck + ruff
- `make format` -- ruff auto-format

## Key Design Decisions
- **Python for SOVA**: unified stack for CLI, agent, dashboard. Zero-config SQLite for single dev.
- **Bash for legacy agent**: zero runtime deps, kept during migration
- **Role-based agents**: triage, researcher, developer, reviewer with dispatcher routing
- **Gate checks between steps**: every step validates output before the next starts
- **Ephemeral agents**: spawn, work, write handoff, die. No persistent sessions.
- **Worktree isolation**: each task gets its own git worktree (parallel-safe)
- **Adapter pattern for task sources**: swap GitHub/JIRA/Linear without touching core
- **Mandatory pipeline**: Triage -> Researcher -> Developer (enforced by Gate 3); `--force` bypasses
- **Handoff protocol**: JSON-based inter-agent state passing via file + DB
- **Short-lived agent model**: agents run, write handoff, exit; dashboard provides the interactive bridge
- **Markdown commands**: Claude Code loads them as slash commands, 20 commands with category frontmatter
- **Persona auto-detection**: detects project tech stack and loads relevant guidance
- **DB persistence**: TaskRun, CostRecord, StepExecution tracked in SQLite/PostgreSQL
- **Combined server**: `sova server start` runs dashboard + scheduler in one async process
