# Architecture

## Component Overview

SOVA has four main components:

### 1. CLI (`sova/cli/`)
- Python CLI built with Typer, entry point `sova` (via pyproject.toml)
- Subcommands: `run`, `triage`, `install`, `setup`, `dashboard`, `server`, `commands`, `memory`, `migrate`, `status`, `costs`, `cleanup`, `address-pr`, `maintain-pr`, `review-pr`, `learn-from-pr`
- Registered in `sova/cli/app.py`, implementations in `sova/cli/commands/`

### 2. Agent Core (`sova/core/`, `sova/roles/`)
- `core/workflow.py` -- WorkflowEngine: executes step pipelines with DB persistence (TaskRun, StepExecution, FailureRecord)
- `core/state.py` -- 17-state TaskStatus StrEnum with transition validation
- `core/context.py` -- ExecutionContext dataclass threading state through steps
- `core/steps/` -- 16 BaseStep implementations with execute/validate_output/can_skip. Two pipeline variants:
  - **Developer pipeline** (12 steps): sync -> assess -> create_worktree -> develop -> simplify -> self_review -> commit -> validate -> push -> create_pr -> monitor_ci -> handoff_to_reviewer
  - **Address-review pipeline** (5 steps): address_review -> commit -> validate -> push -> handoff_to_user
- `roles/` -- AgentRole ABC with 4 implementations: triage, researcher, developer, reviewer
- `roles/dispatcher.py` -- routes tasks to appropriate roles based on state
- **Role chaining**: Developer -> Reviewer -> Developer handoff chain runs autonomously. `HandoffAction.auto_execute` triggers auto-spawn of the next agent when the current one exits. Developer writes handoff to Reviewer (auto), Reviewer writes handoff back to Developer if findings exist (auto) or to user if clean (manual "Integrate PR" button). Issue stays `IN_REVIEW` until human merges via `/integrate-pr` or `/approve-merge`.
- **Step gate checks**: every `validate_output()` must check all forms of change -- unstaged diff (`git diff --stat HEAD`), staged diff (`git diff --cached --stat`), and commits ahead of base (`git log {base}..HEAD --oneline`). LLM agents may commit directly, leaving working-tree diffs empty even when real work was done. Never return `GateCheckResult(passed=True)` unconditionally.
- **Context persistence at step boundaries**: `_sync_task_run_context()` persists `worktree_path`, `branch_name`, and `pr_number` to the TaskRun after every step. This ensures the checkpoint/resume system can restore context even if the run pauses or crashes mid-pipeline.

### 3. Dashboard (`sova/dashboard/`)
- Python/FastAPI web UI with app factory pattern (`create_app(project_dir=None)`)
- Jinja2 templates + Tailwind CSS (via CDN), Catppuccin dark theme
- 12 pages: dashboard, agents, work, run_detail, costs, queue, logs, settings, memory, setup, home, style_guide
- **Design system**: CSS variables (Catppuccin Mocha) in `static/style.css`, shared Tailwind config in `_head.html`, SVG icon macro in `_icons.html`, component macros in `_components.html`
- 13 API routers under `/api`: overview, runs, costs, control, handoff, memory, logs, tasks, queue, settings, setup, agents, work
- 12 services: run, cost, memory, control, handoff, queue, batch, work, task, log, settings, setup
- Old pages (overview, control, runs, tasks) redirect to new equivalents (dashboard, agents, work)
- **Multi-agent control**: manages concurrent agent processes per project with slot limits and per-issue dedup
- **Batch operations**: triage/harden multiple issues with parallel concurrency (`asyncio.Semaphore`, default 3 for triage, 2 for harden via `DEFAULT_CONCURRENCY`). `BatchJob.max_concurrency` configurable per-batch. Global progress bar in `base.html` (visible on all pages), batch ID persistence via `sessionStorage`, `GET /api/queue/batch/active` endpoint for discovering running batches after page navigation or browser refresh
- **Handoff system**: agents write `.claude/agent-control/handoff.json` to pass state between agents
  - `handoff_service.py` -- read/write/archive handoff files (mtime-cached)
  - Dashboard renders handoff action buttons on the agents page (awaiting_action/completed/failed)
  - `_process_auto_handoff()` in `control_service.py` auto-triggers `HandoffAction` entries with `auto_execute=True` after agent exit, enabling autonomous role chaining
  - Enables chaining: `integrate-pr` (full pipeline) or `ship-pr` -> `agent-resume` -> `approve-merge` (step-by-step)
- **Claude command execution**: `control_service.start_command()` runs Claude Code commands from handoff actions
- Tests: `tests/test_dashboard.py` + `tests/test_batch_service.py` (pytest + httpx ASGITransport), run via `make test-py`

### 4. Scheduler (`sova/scheduler/`)
- `watch.py` -- WatchLoop: async poll with priority scan (RESEARCHED > TRIAGED > BACKLOG), veto window, asyncio.Event for shutdown
- `parallel.py` -- ParallelExecutor: asyncio.Semaphore for max_parallel_agents
- `server.py` -- SOVAServer: combined FastAPI dashboard + scheduler in one process, PID file lifecycle
- Deploy: `deploy/sova-server.service` (systemd) + `deploy/com.sova.server.plist` (launchd)
- CLI: `sova server start/stop/status`

## Supporting Modules

- **`sova/adapters/`** -- TaskAdapter ABC + GitHub implementation (state via `agent:` labels + Projects V2 board), factory, per-project `gh` auth via `sova/utils/gh.py`
- **`sova/llm/`** -- Claude CLI async wrapper (`client.py`), cost recording (`cost.py`), model routing. `--output-format json` returns `{result, total_cost_usd, usage, duration_ms, session_id}`. `--output-format stream-json` emits JSONL: `type: "assistant"` (content blocks), `type: "content_block_delta"` (streaming text), `type: "result"` (final cost/usage). Dashboard's `_parse_stream_line()` extracts readable text from these events.
- **`sova/git/`** -- branch/commit/push/PR operations (`operations.py`), worktree lifecycle (`worktree.py`)
- **`sova/ipc/`** -- AgentProcess (spawn/stop/stream), AgentHandoff + DashboardHandoff models, notifications (desktop + Slack)
- **`sova/knowledge/`** -- Memory CRUD + search + promote, tier loading, persona detection, review patterns
- **`sova/commands/`** -- command distribution: catalog (discover + classify), templates (regex rendering), manifest (SHA-256 tracking), distribution (install/update/diff with conflict detection)
- **`sova/config/`** -- Pydantic Settings v2, TOML loader, project registry
- **`sova/db/`** -- SQLAlchemy 2.0 async ORM models (TaskRun, StepExecution, FailureRecord, CostRecord, Memory, TaskAssessmentRecord), session factory (SQLite default, PostgreSQL optional)

## Config System
- **SOVA config**: `sova.toml` per project (Pydantic Settings, env var overrides via `SOVA_` prefix)
- **Migration**: `sova migrate config` converts legacy `pak-agent.conf` to `sova.toml`
- **DB URL**: `SOVA_DATABASE_URL` env var for PostgreSQL; defaults to `.claude/sova.db` (SQLite)

## Naming Convention

The project's full name is **SOVA** (Software Orchestration Via Agents).

- **CLI command**: `sova`
- **PyPI package**: `sova`
- **Config files**: `sova.toml`, `sova.db`

## Development Workflow
- `Makefile` at repo root provides all development targets
- `make serve` -- start dashboard
- `make check` -- lint + test (CI-equivalent)
- `make test` -- bash (shellcheck + invariant --help) + python (pytest)
- `make lint` -- shellcheck + ruff
- `make format` -- ruff auto-format

## Key Design Decisions
- **Python for SOVA**: unified stack for CLI, agent, dashboard. Zero-config SQLite for single dev.
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
- **Dual TaskRun write paths**: the dashboard's `control_service` creates a TaskRun tracking the outer process lifecycle (spawned/done/failed), while `WorkflowEngine` creates a separate TaskRun tracking inner workflow state (pending/developing/paused/done). These are independent DB records. Cost aggregation uses `TaskRun.total_cost_usd` (always written by both paths); `CostRecord` is only reliable for per-model/per-phase breakdowns.
- **Combined server**: `sova server start` runs dashboard + scheduler in one async process
- **File-backed service test isolation**: dashboard services that read project files (log_service, handoff_service, control_service) use module-level project dir + mtime caches. Tests MUST monkeypatch `get_project_dir` to point at `tmp_path`, otherwise they read real project files and produce flaky results. Caches (`_log_cache`, `_handoff_cache`) may also need clearing between tests.
- **Idempotent finalization**: when multiple codepaths can finalize state (e.g., reader thread EOF + `stop_agent`, background sweep + `_wait_and_finalize`), guard with a status check before writing: `if record["status"] != "running": return`. Without this, a later finalizer overwrites a correct terminal status with a stale one. Applies to run journal finalization, TaskRun status updates, and any file-based or DB-backed state shared between concurrent async tasks.
- **Non-fatal side effects**: wrap optional side effects (tracker state transitions, board moves, journaling) in try/except so failures don't block the primary operation. Always pass `exc_info=True` to the logger so stack traces are preserved: `except Exception: log.warning("event.failed", exc_info=True)`. Pattern used in `_move_on_board()`, `_transition_to_in_progress()`, notification calls.
- **Testing shell-backed async code**: mock `run` at the module level (`patch("sova.adapters.github.run", new_callable=AsyncMock)`). Use a `_shell_result()` helper for `ShellResult` objects. For multi-call methods, use `mock.side_effect = [result1, result2, ...]`. For methods with internal mocking, use `patch.object(instance, "method", new_callable=AsyncMock)`. Pattern used across `test_adapters.py`, `test_git.py`.
- **Stale run recovery + dismiss**: `recover_stale_runs()` marks dead-PID TaskRuns as "interrupted" on dashboard startup. `POST /api/agents/interrupted/dismiss` changes interrupted runs to "failed" so users can clear the banner. The "interrupted" status must be in `_TERMINAL` sets in `work_service.py`.
- **`gh pr create` outputs a URL, not JSON**: unlike `gh pr view`, `gh pr create` does NOT support `--json`. It returns the PR URL as plain text. Parse PR number from the URL path: `int(url.rstrip("/").split("/")[-1])`. All other `gh pr` subcommands (`view`, `list`, `checks`) support `--json`.
- **Roles must self-discover missing context**: when a role is spawned fresh from the dashboard (not via `--resume`), `ExecutionContext` fields populated by earlier pipeline steps (e.g., `pr_number` from `CreatePRStep`) are `None`. Roles that depend on upstream context must look it up from GitHub rather than failing. Pattern: `ReviewerRole` calls `find_pr_for_issue()` when `ctx.pr_number` is missing.
- **Post-stage suspicious file guard**: `commit()` in `sova/git/operations.py` checks staged files against `_SUSPICIOUS_PATHS` (`.venv`, `.env`, `credentials.json`, etc.) after `git add` but before `git commit`. Bad files are unstaged with `git reset HEAD` and a RuntimeError is raised. This catches `.gitignore` edge cases (symlinks, pattern mismatches).
- **Dual handoff persistence (file + DB)**: agents write both DB-backed `AgentHandoff` (for orchestrator/scheduler history via `write_handoff()`) and file-based `DashboardHandoff` (for dashboard polling via `write_handoff_file()`). Dashboard reads the file for speed (no async DB in polling loop); scheduler queries the DB for cross-run state. `DashboardHandoff` has `next_actions` (UI buttons); `AgentHandoff` has `pending_findings` (agent context). Both written in every role's `_write_handoff()`.
- **Adapter ABC contract**: `TaskAdapter` in `sova/adapters/base.py` defines 12 methods: `list_tasks`, `get_task`, `get_state`, `transition_state`, `assign`, `add_label`, `remove_label`, `post_comment`, `post_pr_comment`, `edit_body`, `link_pr`, plus `github_user` field. When adding new agent capabilities that interact with the tracker, add the method to the ABC first, then implement in `GitHubAdapter`. Factory: `create_adapter(type, repo, github_user, project_number)`.
- **LLM for user-facing outputs with structured fallback**: workflow steps producing user-facing content (PR descriptions, review comments) use a focused LLM call (Sonnet, ~$0.01) with structured context (commit log, diff stats, issue body). Fallback MUST preserve available data -- build a structured body from already-fetched data rather than discarding to a bare stub. Pattern: `_generate_pr_body()` + `_build_fallback_body()` in `create_pr.py`, `_run_review()` in `reviewer.py`.
