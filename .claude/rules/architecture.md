# Architecture

## Component Overview

SOVA has four main components:

### 1. CLI (`sova/cli/`)
- Python CLI built with Typer, entry point `sova` (via pyproject.toml)
- Subcommands: `run`, `watch`, `parallel`, `triage`, `harden`, `install`, `setup`, `uninstall`, `dashboard`, `server`, `commands`, `memory`, `status`, `costs`, `cleanup`, `doctor`, `address-pr`, `maintain-pr`, `review-pr`, `linked-issues`, `pr-verdict`, `refresh-pr-body`, `learn-from-pr`, `init-db`, `migrate-config`, `config` (show, `config get`, `config set`), `mcp`, `supervisor`, `briefing`, `assess` (`assess set`, `assess get`, `assess clear`), `db` (`db status`, `db backup`), `checks` (`checks should-skip`, `checks record`), `board` (`board add`), `audit` (`audit report-path`, `audit disclosure`)
- Registered in `sova/cli/app.py`, implementations in `sova/cli/commands/`

### 2. Agent Core (`sova/core/`, `sova/roles/`)
- `core/workflow.py` (WorkflowEngine): executes step pipelines with DB persistence (TaskRun, StepExecution, FailureRecord)
- `core/state.py`: 19-state TaskStatus StrEnum (no transition validation: `TaskRun.status` writes come from several pipeline variants and recovery paths that don't fit one table)
- `core/context.py`: ExecutionContext dataclass threading state through steps
- `core/output.py`: OutputWriter for per-run DB-backed output persistence, read_lines, retention cleanup
- `core/steps/`: 31 BaseStep implementations with execute/validate_output/verify_output/can_skip. Four pipeline variants:
  - **Developer pipeline** (17 steps): sync -> assess -> create_worktree -> capture_baseline -> develop -> simplify -> self_review -> commit -> validate -> push -> create_pr -> wait_for_external_reviews -> address_external_findings -> monitor_ci -> confidence_score -> extract_memory -> handoff_to_reviewer
  - **Address-review pipeline** (12 steps): ensure_worktree -> rebase -> address_review -> rearrange_commits -> validate -> push -> resolve_review_threads -> refresh_pr_body -> monitor_ci -> resolve_external_reviews -> extract_memory -> handoff_to_reviewer (re-review of the new head; `handoff_to_user` remains in `STEP_REGISTRY` for projects that configure a manual stop via `[pipelines] address_review`)
  - **Researcher pipeline** (4 steps): fetch_task -> research -> spec -> extract_memory
  - **Planner pipeline** (4 steps): scan_project -> generate_tasks -> validate_tasks -> extract_memory
- `core/dag.py` (DAGExecutor): runs command-based workflow graphs with topological sort, condition evaluation, and cycle detection
- `core/agent_dispatch.py`: dispatch boundary between the text-only LLMProvider path and AgentRuntime. A step declaring `requires_tools = True` dispatches through `get_runtime()` and fails fast when that runtime is unusable, never degrading to a provider that cannot edit files. Every step that edits the worktree sets the flag; `ResearchStep` does not, and on a text-only provider runs a read-only agent through `dispatch_runtime_prompt()` and writes the section itself (full entry: docs/architecture-deep-dive.md, "Agent dispatch boundary").
- `roles/`: AgentRole ABC with 6 implementations: triage, researcher, developer, reviewer, custom, planner
- `roles/custom.py` (CustomRole): executes user-defined DAG workflows via DAGExecutor
- `roles/dispatcher.py`: routes tasks to appropriate roles based on state; `get_role_async()` falls back to DB lookup for custom roles
- **Role chaining**: Developer -> Reviewer -> Developer handoff chain runs autonomously by default via `HandoffAction.auto_execute`, bounded by `pipeline.max_address_review_cycles`; both auto-spawn directions (`pipeline.auto_handoff`, `pipeline.auto_address_review`) are configurable in `sova.toml`. Issue stays `IN_REVIEW` until a human merges via `/integrate-pr`. See `docs/architecture-deep-dive.md` for the full entry.
- **Address-review budget is a resolver state, not a separate gate**: `resolve_next_action()`'s `review_budget_exhausted` rule compares completed `sova-addressed` cycles against `pipeline.max_address_review_cycles` (default 3, 0=unlimited) ahead of every verdict- and thread-based rule (conflict, draft and red or pending CI still win), resolves an over-budget PR to `PR_REVIEW_EXHAUSTED` with Integrate, and holds the PR for the user (never Integrate) when the review history is unresolvable (full entry: docs/architecture-deep-dive.md).
- **Step gate checks (two-phase)**: a fast structural `validate_output()` (`validation.gate_timeout`), then an optional heavyweight `verify_output()` (only `ValidateStep` overrides it). `validate_output()` checks every form of change (unstaged, staged, commits ahead of base, untracked); never return `GateCheckResult(passed=True)` unconditionally (full entry: docs/architecture-deep-dive.md).
- **Context persistence at step boundaries**: `_sync_task_run_context()` persists `worktree_path`, `branch_name` and `pr_number` to the TaskRun after every step, so resume can restore them (full entry: docs/architecture-deep-dive.md).

### 3. Dashboard (`sova/dashboard/`)
- Python/FastAPI web UI with app factory pattern (`create_app(project_dir=None)`)
- Jinja2 templates + Tailwind CSS (prebuilt via `make css`), Catppuccin dark theme
- 23 templates: agents, run_detail, lifecycle, costs, pr_metrics, queue, reliability, specs, settings, connections, setup, home, style_guide, roles, role_editor, commands, spec, base, supervisor, fleet, oversight, briefing, dependency_health
- **Design system**: CSS variables (Catppuccin Mocha) in `static/style.css`, Tailwind config in `tailwind.config.js` (repo root), SVG icon macro in `_icons.html`, component macros in `_components.html`. Run `make css` after adding or removing Tailwind classes in templates/JS (the prebuilt `static/tailwind.min.css` is checked in, not built by CI)
- 32 API routers under `/api`: auth, overview, runs, costs, feed, handoff, health, lifecycle, memory, models, tasks, queue, quota, reliability, settings, setup, agents, work, roles, spec, prs, dependencies, resources, supervisor, oversight, fleet_manager, fleet_insights, telemetry, a2a, mcp, briefing, dependency_health
- 48 services: run, cost, memory, models, control (facade), feed, handoff, health, lifecycle, queue, batch, reliability, work, work_item, work_state, work_verdict, settings, setup, agent_lifecycle, agent_output, agent_recovery, agent_adopt, agent_handoff, agent_pool, agent_db, agent_status, agent_context, agent_progress, agent_validation, agent_approval, agent_finalize, agent_resource, role, spec, pr, pr_metrics, resource, llm_suggestion, output_stream, fleet, fleet_manager, supervisor, oversight, telemetry_push, merge_queue_monitor, mcp_service, awareness, dependency_health
- Old pages (dashboard, overview, control, runs, tasks, work, logs, memory) redirect to the current equivalent (agents); the Memory API, service and `sova memory` CLI remain
- **Sidebar**: six always-visible pages (Agents, Queue, Supervisor, Oversight, Costs, Settings) and a "More" group for the rest (open on a child page, else the `sova.sidebar.moreOpen` localStorage preference written only by an explicit toggle) (`base.html`, `initSidebarMore()` in `app.js`)
- **Page modules**: the Agents page script is eight ES modules under `static/js/pages/agents/`, each exporting `init()`; `actions.js` dispatches clicks (`data-action`, no inline `onclick`). Cross-module state goes through functions or getters, never a mutated or exported `var`, and no module touches an imported binding at its top level. `tests/test_dashboard_agents_modules.py` guards the wiring (full entry: docs/architecture-deep-dive.md).
- **State display has one source**: `work_state.py` owns every state label and color token (`state_label()`, `state_color()`); each state-emitting API attaches `state_label`/`state_color`, and the client only maps a token to badge classes or a hex, keeping no state table of its own (full entry: docs/architecture-deep-dive.md).
- **Shared client helpers have one definition each**: `escapeHtml`, `esc`, `timeAgo`, `setText`, `renderSectionError` and `statusBadge` live in `app.js` only, and a page's inline script calls them only after load (`tests/test_dashboard_agents_modules.py` checks both; full entry: docs/architecture-deep-dive.md).
- **Multi-agent control**: concurrent agents per project with slot limits; `start_agent()` and `start_command()` both call `_check_issue_conflict()`, which rejects a same-issue duplicate found in memory or in the DB (alive PID) and is skipped under `force=True` (full entry: docs/architecture-deep-dive.md).
- **Batch operations**: triage/harden many issues concurrently (`asyncio.Semaphore`, `DEFAULT_CONCURRENCY`) with a global progress bar (full entry: docs/architecture-deep-dive.md).
- **Handoff system**: agents write `.claude/agent-control/handoff.json`; `_process_auto_handoff()` (`agent_handoff.py`) auto-triggers `auto_execute=True` actions after agent exit, per `pipeline.auto_handoff` / `pipeline.auto_address_review` (full entry: docs/architecture-deep-dive.md).
- **Claude command execution**: `agent_lifecycle.start_command()` runs Claude Code commands from handoff actions (re-exported via `control_service` facade)
- Tests: `tests/test_dashboard.py` + `tests/test_batch_service.py` (pytest + httpx ASGITransport, in-memory SQLite via `sqlite+aiosqlite://`), run via `make test-py`

### 4. Scheduler (`sova/scheduler/`)
- `watch.py` (WatchLoop): async poll with priority scan (RESEARCHED > TRIAGED > BACKLOG), veto window, asyncio.Event for shutdown. Dispatches one task at a time (`max_parallel_agents` has no effect here; `sova parallel` is the concurrent path)
- `server.py` (SOVAServer): combined dashboard + scheduler in one process; the scheduler endpoints and `/api/health` (which reports `degraded` when the watch loop stops scanning, and `restart_required` when the checkout HEAD has moved since startup) live here, not in dashboard routers (full entry: docs/architecture-deep-dive.md, "SOVAServer (`server.py`)").
- Deploy: systemd unit, launchd plist and a newsyslog reference under `deploy/`; `ReadWritePaths` must list the whole project dir, the unit is `Type=notify` with `KillMode=process` so a restart leaves agents for the next server to adopt (`recovery.kill_orphans` opts into killing), and `uvicorn.run(access_log=False)` keeps the plist's log files bounded (full entry: docs/architecture-deep-dive.md, "Deploy").
- CLI: `sova server start/stop/status/restart/digest/install-service`

## Model Selection System

Routing is config-driven (`llm.routing.{task_type}`, `llm.routing.{tier}`, `roles.{role}_model`) behind the `LLMProvider` ABC. **Always pin explicit model IDs, never bare aliases** (`opus`, `sonnet`, `haiku`) in `agent.model`, `roles.researcher_model`, `roles.triage_model`, `oversight.analysis_model` and every `agent.fallback_models` entry: the Claude CLI resolves an alias, and a new release can point it at a model unavailable on Vertex. Discovery, layer-1 enumeration and backend-aware alias resolution are summarized in the design doc (full entry: docs/model-selection-architecture.md, "Routing and Discovery Summary").

## Supporting Modules

Deep-dive per-module notes (what each `sova/` subpackage does, and the non-obvious decisions behind it) live in `docs/architecture-deep-dive.md` rather than always-loaded here. Grep that file for a topic below to read its full entry:

- `sova/adapters/`
- `sova/llm/`
- `sova/git/`
- `sova/ipc/`
- Codex gets the neutral half of the headless guardrail preamble, and `read_only` is enforced by the sandbox, not the prompt
- `sova/agents/`
- `sova/knowledge/`
- `sova/utils/`
- `sova/commands/`
- `sova/config/`
- `sova/db/`
- `sova/supervisor/`
- Supervisor API caching layer
- `.claude/benchmark/`
- Benchmark logs are for interactive sessions only; must not double-count SOVA's own autonomous runs

## Config System
- **SOVA config**: `sova.toml` per project (Pydantic Settings, env var overrides via `SOVA_` prefix)
- **DB URL**: `SOVA_DATABASE_URL` for PostgreSQL, default `.claude/sova.db`; one database per project, never shared (full entry: docs/architecture-deep-dive.md).
- **`load_config()` is cached**: process-wide per project, validated by a file-stamp fingerprint (SQLite) or a short TTL (PostgreSQL), and reads the same database as `get_session()`; call `invalidate_config_cache()` after a committed settings write (full entry: docs/database-guidelines.md, "Config Cache").
- **Budget limits**: per-run and per-issue caps prevent runaway costs. See `docs/performance-guidelines.md` for defaults and config keys.
- **Check worker cap**: `scrub_check_env(config=...)` (pass the config in hand, or `project_dir=` when only a project path is; `scrub_orchestrator_env()` for a git-only call), `agent_dispatch` and the dashboard's `start_command()` and `start_agent()` runtime spawns (`with_check_worker_cap()`) set `PYTEST_XDIST_AUTO_NUM_WORKERS` (read by `make check`'s `-n auto`) to `testing.check_workers`, or when it is 0 to a share of the cores and available memory split between the SOVA pipelines active machine-wide (one lease file each, `sova/utils/agent_leases.py`; the dashboard leases for its command agents; a computed share inherited from a spawner is recomputed per check, a `testing.check_workers` one is kept), unless the operator set it (full entry: docs/testing-guidelines.md, "Running Tests").
- **Settings metadata registry**: every field in a config model (`models.py`) must also have a `SettingMeta` entry in `sova/dashboard/settings_meta.py` with key, label, description, group, and value_type. Without it, the field won't appear in the dashboard settings UI. When adding a new config group (e.g., `external_reviews`), also add it to `GROUPS` dict and `GROUP_ORDER` list.
- **Enumerable settings** (`SettingMeta.options_source`): a model setting whose values are discovered at runtime sets `options_source="llm"` or `"runtime"` instead of a static `options` tuple, and renders as a grouped dropdown with a "Custom..." free-text escape that fails open to the plain text editor; `codex.model` stays free text (full entry: docs/architecture-deep-dive.md).

## Naming Convention

The project's full name is **SOVA** (Software Orchestration Via Agents).

- **CLI command**: `sova`
- **PyPI package**: `sova`
- **Config files**: `sova.toml`, `sova.db`

## Development Workflow
- `Makefile` at repo root provides all development targets
- `make serve`: start dashboard
- `make check`: lint + test (CI-equivalent)
- `make test`: bash (shellcheck + invariant --help) + python (pytest)
- `make lint`: shellcheck + ruff + `lint-js` (`node --check` on `static/*.js` as scripts and `static/js/**/*.js` as ES modules) + `lint-tree` (the whole-tree `no-double-dash` and `file-encoding` scans, also CI's invariants job, kept out of pytest)
- `make format`: ruff auto-format

## Key Design Decisions

Full narrative entries (the reasoning, the incident that motivated a fix, and the exact files touched) live in `docs/architecture-deep-dive.md`, and its index lists every topic title (full entry: docs/architecture-deep-dive.md, "Key Design Decisions Index"). Grep that doc for a topic before changing the behavior it describes.

## Cross-References (domain-specific details)

The following topics have detailed implementation guidance in dedicated guideline docs. Architecture.md covers the architectural decisions; the guideline docs cover patterns, code examples, and gotchas.

- **Migration system** (create_all vs Alembic, alembic_version cases, engine disposal, self-healing fallback): see `docs/database-guidelines.md`
- **Session management** (context manager pattern, expire_on_commit, JSON column NULL): see `docs/database-guidelines.md`
- **Exception hierarchy** (SonarCloud S5713, parent/child catch tuples): see `docs/error-handling-guidelines.md`
- **Non-fatal side effects** (try/except pattern, exc_info=True): see `docs/error-handling-guidelines.md`
- **Timeout hierarchy** (step timeout, CI polling, shell commands, budget limits): see `docs/performance-guidelines.md`
- **GitHub CLI integration** (GH_TOKEN override, gh auth switch, gh pr create URL, PR reviews vs comments): see `docs/integration-guidelines.md` and `docs/api-contracts-guidelines.md`
- **Subprocess safety** (suspicious file guard, shlex.quote): see `docs/security-guidelines.md`
- **Test isolation patterns** (file-backed services, patch.object facades, mock patterns): see `docs/testing-guidelines.md`
- **Codex runtime setup** (browser login with keyring storage, device-auth and stdin API-key fallbacks, trusted-automation-only `CODEX_API_KEY`, sandbox config, rollback): see `docs/codex-runtime-setup.md`
