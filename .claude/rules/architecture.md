# Architecture

## Component Overview

SOVA has four main components:

### 1. CLI (`sova/cli/`)
- Python CLI built with Typer, entry point `sova` (via pyproject.toml)
- Subcommands: `run`, `watch`, `parallel`, `triage`, `harden`, `install`, `setup`, `dashboard`, `server`, `commands`, `memory`, `migrate`, `status`, `costs`, `cleanup`, `address-pr`, `maintain-pr`, `review-pr`, `learn-from-pr`
- Registered in `sova/cli/app.py`, implementations in `sova/cli/commands/`

### 2. Agent Core (`sova/core/`, `sova/roles/`)
- `core/workflow.py` -- WorkflowEngine: executes step pipelines with DB persistence (TaskRun, StepExecution, FailureRecord)
- `core/state.py` -- 17-state TaskStatus StrEnum with transition validation
- `core/context.py` -- ExecutionContext dataclass threading state through steps
- `core/output.py` -- OutputWriter for per-run log file persistence (moved from dashboard/services/)
- `core/steps/` -- 17 BaseStep implementations with execute/validate_output/can_skip. Two pipeline variants:
  - **Developer pipeline** (13 steps): sync -> assess -> create_worktree -> develop -> simplify -> self_review -> commit -> validate -> push -> create_pr -> monitor_ci -> extract_memory -> handoff_to_reviewer
  - **Address-review pipeline** (7 steps): rebase -> address_review -> commit -> validate -> push -> extract_memory -> handoff_to_user
- `roles/` -- AgentRole ABC with 4 implementations: triage, researcher, developer, reviewer
- `roles/dispatcher.py` -- routes tasks to appropriate roles based on state
- **Role chaining**: Developer -> Reviewer -> Developer handoff chain runs autonomously. `HandoffAction.auto_execute` triggers auto-spawn of the next agent when the current one exits. Developer writes handoff to Reviewer (auto), Reviewer writes handoff back to Developer if findings exist (auto) or to user if clean (manual "Integrate PR" button). Issue stays `IN_REVIEW` until human merges via `/integrate-pr` or `/approve-merge`.
- **Step gate checks**: every `validate_output()` must check all forms of change -- unstaged diff (`git diff --stat HEAD`), staged diff (`git diff --cached --stat`), and commits ahead of base (`git log {base}..HEAD --oneline`). LLM agents may commit directly, leaving working-tree diffs empty even when real work was done. Never return `GateCheckResult(passed=True)` unconditionally.
- **Context persistence at step boundaries**: `_sync_task_run_context()` persists `worktree_path`, `branch_name`, and `pr_number` to the TaskRun after every step. This ensures the checkpoint/resume system can restore context even if the run pauses or crashes mid-pipeline. Dashboard's `_create_task_run()` also accepts `pr_number` so reviewer runs spawned via auto-handoff have it recorded immediately (without relying on WorkflowEngine sync).

### 3. Dashboard (`sova/dashboard/`)
- Python/FastAPI web UI with app factory pattern (`create_app(project_dir=None)`)
- Jinja2 templates + Tailwind CSS (via CDN), Catppuccin dark theme
- 13 pages: dashboard, agents, work, run_detail, lifecycle, costs, queue, logs, settings, memory, setup, home, style_guide
- **Design system**: CSS variables (Catppuccin Mocha) in `static/style.css`, shared Tailwind config in `_head.html`, SVG icon macro in `_icons.html`, component macros in `_components.html`
- 14 API routers under `/api`: overview, runs, costs, control, handoff, lifecycle, memory, logs, tasks, queue, settings, setup, agents, work
- 20 services: run, cost, memory, control (facade), handoff, lifecycle, queue, batch, work, task, log, settings, setup, agent_lifecycle, agent_output, agent_recovery, agent_handoff, agent_pool, agent_db, output (re-export facade for core/output)
- Old pages (overview, control, runs, tasks) redirect to new equivalents (dashboard, agents, work)
- **Multi-agent control**: manages concurrent agent processes per project with slot limits and per-issue dedup. Both `start_agent()` and `start_command()` call `_check_issue_conflict()` which rejects duplicates via two checks: in-memory (`pa.agents`) and DB (`TaskRun` with alive PID). The DB check catches CLI-spawned agents not tracked in-memory. The `max_concurrent` slot check alone doesn't prevent same-issue duplicates. `_check_issue_conflict()` auto-recovers dead-PID DB runs by marking them "interrupted" on detection. When `force=True` (passed through from `start_agent`), both in-memory and live external conflicts are skipped so `--force` retries are not blocked by stale state.
- **Batch operations**: triage/harden multiple issues with parallel concurrency (`asyncio.Semaphore`, default 3 for triage, 2 for harden via `DEFAULT_CONCURRENCY`). `BatchJob.max_concurrency` configurable per-batch. Global progress bar in `base.html` (visible on all pages), batch ID persistence via `sessionStorage`, `GET /api/queue/batch/active` endpoint for discovering running batches after page navigation or browser refresh
- **Handoff system**: agents write `.claude/agent-control/handoff.json` to pass state between agents
  - `handoff_service.py` -- read/write/archive handoff files (mtime-cached)
  - Dashboard renders handoff action buttons on the agents page (awaiting_action/completed/failed). Failed runs also show a "Re-run" button that pre-fills issue, role, and PR number.
  - `_process_auto_handoff()` in `agent_handoff.py` auto-triggers `HandoffAction` entries with `auto_execute=True` after agent exit, enabling autonomous role chaining
  - Enables chaining: `integrate-pr` (full pipeline) or `ship-pr` -> `agent-resume` -> `approve-merge` (step-by-step)
- **Claude command execution**: `agent_lifecycle.start_command()` runs Claude Code commands from handoff actions (re-exported via `control_service` facade)
- Tests: `tests/test_dashboard.py` + `tests/test_batch_service.py` (pytest + httpx ASGITransport, in-memory SQLite via `sqlite+aiosqlite://`), run via `make test-py`

### 4. Scheduler (`sova/scheduler/`)
- `watch.py` -- WatchLoop: async poll with priority scan (RESEARCHED > TRIAGED > BACKLOG), veto window, asyncio.Event for shutdown
- `parallel.py` -- ParallelExecutor: asyncio.Semaphore for max_parallel_agents
- `server.py` -- SOVAServer: combined FastAPI dashboard + scheduler in one process, PID file lifecycle
- Deploy: `deploy/sova-server.service` (systemd) + `deploy/com.sova.server.plist` (launchd)
- CLI: `sova server start/stop/status`

## Supporting Modules

- **`sova/adapters/`** -- TaskAdapter ABC + GitHub implementation (state via `agent:` labels + Projects V2 board), factory, per-project `gh` auth via `sova/utils/gh.py`
- **`sova/llm/`** -- Claude CLI async wrapper (`client.py`), cost recording (`cost.py`), model routing. `--output-format json` returns `{result, total_cost_usd, usage, duration_ms, session_id}`. `--output-format stream-json` emits JSONL: `type: "assistant"` (content blocks), `type: "content_block_delta"` (streaming text), `type: "result"` (final cost/usage). Dashboard's `_parse_stream_line()` extracts readable text from these events.
- **`sova/git/`** -- branch/commit/push (`branch.py`), PR operations (`pr.py`), LLM rebase (`rebase.py`), worktree lifecycle (`worktree.py`). `operations.py` is a thin re-export facade. Always wrap `json.loads(result.stdout)` in try/except when parsing `gh` CLI output -- even successful commands can return empty stdout in edge cases.
- **`sova/ipc/`** -- AgentProcess (spawn/stop/stream), AgentHandoff + DashboardHandoff models, notifications (desktop + Slack)
- **`sova/knowledge/`** -- Memory CRUD + search + promote, tier loading, persona detection, review patterns
- **`sova/commands/`** -- command distribution: catalog (discover + classify), templates (regex rendering), manifest (SHA-256 tracking), distribution (install/update/diff with conflict detection)
- **`sova/config/`** -- Pydantic Settings v2, TOML loader, project registry, per-request project context (`context.py`, moved from dashboard/project_context.py)
- **`sova/db/`** -- SQLAlchemy 2.0 async ORM models (TaskRun, StepExecution, FailureRecord, CostRecord, Memory, TaskAssessmentRecord, IssueLifecycle, LifecyclePhaseRecord), session factory (SQLite default, PostgreSQL optional). **Session pattern**: always use `async with await get_session() as session:` (context manager auto-closes). Never use manual `session = await get_session(); ...; await session.close()` -- exceptions between acquire and close leak sessions.

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
- **Split pipelines at role boundaries**: each role has its own step pipeline (developer: 13 steps, address-review: 7 steps). A single monolithic pipeline that crosses role boundaries (develop + review + address in one sequence) breaks agent isolation and prevents independent lifecycles. The role detects which pipeline variant to run from issue state + context (IN_REVIEW with pr_number = address-review mode).
- **Issue state ownership is human**: agents never auto-move issues to DONE. Issues stay IN_REVIEW until the human merges via `/integrate-pr` or `/approve-merge`. The agent prepares the PR; the human approves and merges.
- **Handoff protocol**: JSON-based inter-agent state passing via file + DB
- **Short-lived agent model**: agents run, write handoff, exit; dashboard provides the interactive bridge
- **Markdown commands**: Claude Code loads them as slash commands, 28 commands with category frontmatter
- **Persona auto-detection**: detects project tech stack and loads relevant guidance
- **DB persistence**: TaskRun, CostRecord, StepExecution tracked in SQLite/PostgreSQL
- **Dual TaskRun write paths**: the dashboard's `control_service` creates a TaskRun tracking the outer process lifecycle (spawned/done/failed), while `WorkflowEngine` creates a separate TaskRun tracking inner workflow state (pending/developing/paused/done). These are independent DB records. Cost aggregation uses `TaskRun.total_cost_usd` (always written by both paths); `CostRecord` is only reliable for per-model/per-phase breakdowns.
- **Combined server**: `sova server start` runs dashboard + scheduler in one async process
- **File-backed service test isolation**: dashboard services that read project files (log_service, handoff_service, control_service) use module-level project dir + mtime caches. Tests MUST monkeypatch `get_project_dir` to point at `tmp_path`, otherwise they read real project files and produce flaky results. Caches (`_log_cache`, `_handoff_cache`) may also need clearing between tests.
- **Idempotent finalization**: when multiple codepaths can finalize state (e.g., reader thread EOF + `stop_agent`, background sweep + `_wait_and_finalize`), guard with a status check before writing: `if record["status"] != "running": return`. Without this, a later finalizer overwrites a correct terminal status with a stale one. Applies to run journal finalization, TaskRun status updates, and any file-based or DB-backed state shared between concurrent async tasks. Two concrete instances: (1) `_finalize_task_run()` in `agent_db.py` checks `if task_run.status in ("done", "failed", "interrupted"): return` before overwriting, so manual `mark_run_failed()` results survive the `_wait_and_finalize` callback; (2) any endpoint that sets a run's status to terminal (e.g., `mark-failed`) must also call `stop_agent(run_id=...)` to kill the subprocess, otherwise `_wait_and_finalize()` blocks forever on `await process.wait()`.
- **Non-fatal side effects**: wrap optional side effects (tracker state transitions, board moves, journaling) in try/except so failures don't block the primary operation. Always pass `exc_info=True` to the logger so stack traces are preserved: `except Exception: log.warning("event.failed", exc_info=True)`. Pattern used in `_move_on_board()`, `_transition_to_in_progress()`, notification calls.
- **Testing shell-backed async code**: mock `run` at the module level (`patch("sova.adapters.github.run", new_callable=AsyncMock)`). Use a `_shell_result()` helper for `ShellResult` objects. For multi-call methods, use `mock.side_effect = [result1, result2, ...]`. For methods with internal mocking, use `patch.object(instance, "method", new_callable=AsyncMock)`. Pattern used across `test_adapters.py`, `test_git.py`.
- **Stale run recovery + dismiss**: `recover_stale_runs()` queries all non-terminal TaskRuns (`status.notin_(_TERMINAL)`) on dashboard startup and marks dead-PID ones as "interrupted" -- not just `status == "running"`, so runs stuck in "pending", "assessing", etc. are also caught. `POST /api/agents/interrupted/dismiss` changes interrupted runs to "failed" so users can clear the banner. The "interrupted" status must be in `_TERMINAL` sets in `work_service.py`.
- **SOVA review state lives in the DB, not GitHub's review API**: SOVA's reviewer posts findings as formal PR reviews when possible (with body-only fallback), but the authoritative review verdict is always in the `TaskRun` DB record, not GitHub's `reviewDecision` field. Query `TaskRun` (role in `reviewer`/`command:review-pr`, status=done) for `handoff_json.next_action` to get the adapter-agnostic verdict (block/revise/approve). Dashboard uses `get_sova_review_verdict()` in `agent_recovery.py`. GitHub's `reviews` array may be empty even after a SOVA review if the formal review API call failed and only the issue comment fallback succeeded.
- **`gh pr create` outputs a URL, not JSON**: unlike `gh pr view`, `gh pr create` does NOT support `--json`. It returns the PR URL as plain text. Parse PR number from the URL path: `int(url.rstrip("/").split("/")[-1])`. All other `gh pr` subcommands (`view`, `list`, `checks`) support `--json`.
- **Roles must self-discover missing context**: when a role is spawned fresh from the dashboard (not via `--resume`), `ExecutionContext` fields populated by earlier pipeline steps (e.g., `pr_number` from `CreatePRStep`) are `None`. Roles that depend on upstream context must look it up from GitHub rather than failing. Pattern: `ReviewerRole` calls `find_pr_for_issue()` when `ctx.pr_number` is missing.
- **PR deduplication in CreatePRStep**: `CreatePRStep.execute()` calls `find_pr_for_issue()` before `create_pr()`. If an open PR already exists for the issue, it adopts it (`ctx.pr_number = existing.number`) instead of creating a duplicate. `find_pr_for_issue()` verifies PRs by checking for issue-linking keywords (`Closes/Fixes/Resolves #N`) in the body or branch name pattern (`issue-N`), not free-text search, to avoid false positives.
- **Post-stage suspicious file guard**: `commit()` in `sova/git/branch.py` checks staged files against `_SUSPICIOUS_PATHS` (`.venv`, `.env`, `credentials.json`, etc.) after `git add` but before `git commit`. Uses `Path(f).parts` for component matching so nested paths like `src/.env` or `vendor/credentials.json` are caught, not just exact filenames. Bad files are unstaged with `git reset HEAD` and a RuntimeError is raised. This catches `.gitignore` edge cases (symlinks, pattern mismatches).
- **macOS notifications via terminal-notifier**: `send_desktop_notification()` in `sova/ipc/notifications.py` uses `terminal-notifier` (Homebrew) as the primary path on macOS, supporting custom app icon (`-appIcon`), subtitle, sound, and notification grouping. Falls back to JXA (`osascript -l JavaScript`) if `terminal-notifier` is not installed. JXA fallback uses `json.dumps()` for injection-safe escaping and `soundName: "Glass"`. All call sites pass title="SOVA", subtitle="{Role} {status} #{issue}", message="{project} | {details}", group="sova-{issue}".
- **Dual handoff persistence (file + DB)**: agents write both DB-backed `AgentHandoff` (for orchestrator/scheduler history via `write_handoff()`) and file-based `DashboardHandoff` (for dashboard polling via `write_handoff_file()`). Dashboard reads the file for speed (no async DB in polling loop); scheduler queries the DB for cross-run state. `DashboardHandoff` has `next_actions` (UI buttons); `AgentHandoff` has `pending_findings` (agent context). Both written in every role's `_write_handoff()`.
- **Auto-handoff must clear handoff file before spawning next agent**: `_process_auto_handoff()` in `agent_handoff.py` calls `handoff_service.clear_handoff()` before spawning the next agent via `start_agent()` or `start_command()`. Without this, the newly spawned agent or a concurrent dashboard poll may re-process the same handoff, leading to duplicate agent spawns. The clear must happen synchronously before the spawn call, not after.
- **Adapter ABC contract**: `TaskAdapter` in `sova/adapters/base.py` defines 13 methods: `list_tasks`, `get_task`, `get_state`, `transition_state`, `assign`, `add_label`, `remove_label`, `post_comment`, `post_pr_comment`, `post_pr_review`, `edit_body`, `link_pr`, plus `github_user` field. When adding new agent capabilities that interact with the tracker, add the method to the ABC first, then implement in `GitHubAdapter`. Factory: `create_adapter(type, repo, github_user, project_number)`.
- **LLM for user-facing outputs with structured fallback**: workflow steps producing user-facing content (PR descriptions, review comments) use a focused LLM call (Sonnet, ~$0.01) with structured context (commit log, diff stats, issue body). Fallback MUST preserve available data -- build a structured body from already-fetched data rather than discarding to a bare stub. Pattern: `_generate_pr_body()` + `_build_fallback_body()` in `create_pr.py`, `_run_review()` in `reviewer.py`.
- **Rebase with LLM conflict resolution**: `rebase_with_conflict_resolution()` in `sova/git/rebase.py` rebases onto the latest base branch and uses the LLM to resolve merge conflicts. Loop: detect conflicted files, invoke LLM to fix markers + `git add`, `git rebase --continue`. Aborts on LLM failure or after `max_attempts` (default 3) so the worktree is never left in a broken rebase state. `RebaseStep` in the address-review pipeline runs this before `AddressReviewStep`. `PushStep` uses `--force-with-lease` when `ctx.pr_number` is set (post-rebase history rewrite).
- **Thin re-export wrappers during module splits**: when splitting a large module into submodules (e.g., `control_service.py` -> `agent_lifecycle.py` + `agent_output.py` + `agent_recovery.py`), convert the original module to a thin re-export facade (`from agent_lifecycle import X`) rather than deleting it. This preserves backward compatibility for all existing imports in routers, tests, and commands. Delete the old module only after all imports are migrated.
- **Facade re-exports and `patch.object` for tests**: when a module is split into submodules with a thin re-export facade, `patch.object(facade, "func")` only patches the facade's attribute -- it does NOT affect calls within the submodule itself (where `func` is resolved locally). Tests must `patch.object(actual_submodule, "func")`. For cross-module calls that need patching, use module-level attribute access (`import module; module.func()`) not local binding (`from module import func`) -- only the former is patchable.
- **Never non-visible overflow on containers with popout children**: the sidebar `<nav>` and its scrollable child div contain absolutely-positioned children (notification panel, CSS hover tooltips) that extend beyond their boundary at `left: 100%`. ANY non-visible overflow value (`hidden`, `auto`, `scroll`) on ANY ancestor creates a clipping boundary for positioned descendants. CSS spec: when one axis is non-visible, the other is computed to `auto` even if explicitly set to `visible` -- so `overflow-y: auto; overflow-x: visible` does NOT work (x becomes `auto` too). Use `max-width` and opacity transitions on individual child elements instead of overflow on the container. This applies to any fixed-position sidebar or panel that contains popout menus or tooltips.
- **Automatic memory extraction**: `ExtractMemoryStep` runs before every handoff step in both pipelines. `ReviewerRole.execute()` calls `extract_memories()` directly (reviewer doesn't use WorkflowEngine). A single Haiku LLM call (~$0.005-0.01) extracts 0-5 reusable learnings from run context (role, task, files changed, step summaries, review findings). Results are stored to the Memory DB table via `memory.store()` with deduplication (title similarity check against existing memories in same category). Confirmation counters (`[confirmed: N]` in content field) track reuse; memories auto-promote to "shared" tier at N=3. Extraction is fully non-fatal: failures are logged but never block the pipeline. Module: `sova/knowledge/extraction.py`.
- **Issue Lifecycle Control**: `IssueLifecycle` is the "spine" connecting all `TaskRun` records for a single issue into a unified journey with 6 phases (`LifecyclePhase` enum: development, post_pr, review, address_review, integrate, post_merge). `LifecyclePhaseRecord` tracks each phase execution (status, cost, attempt counter, linked TaskRun). Phase transitions are advisory (warnings, not strict enforcement) -- matches `--force` philosophy. The `post_pr` phase is passive (no TaskRun; inferred from PR creation). `lifecycle_service.py` provides CRUD, phase transitions, and backward-compatible reconstruction from pre-existing TaskRuns via `build_lifecycle_view()`. Integration hooks in `agent_lifecycle.py` (`_link_run_to_lifecycle`, `_finalize_lifecycle_phase`) are non-fatal side effects. Dashboard UI at `/lifecycle/{issue_number}` shows a phase rail with live polling.
- **Doc counts drift after refactors**: step count, service count, adapter method count, CLI subcommand list, and test count go stale after module splits, service extractions, or ABC changes. After any structural refactor, run actual counts (`find`, `grep -c`, `wc -l`) and update AGENTS.md, architecture.md, and CLAUDE.md. The `/review` command checks doc freshness automatically.
- **`create_all` vs Alembic for schema management**: `Base.metadata.create_all` only creates missing tables -- it does NOT add columns to existing tables. Adding a column to an ORM model does nothing to a pre-existing SQLite file; queries crash with `OperationalError: no such column`. Conversely, DBs created via `create_all` may already have columns that a later Alembic migration tries to add, crashing with a duplicate column error. Always use Alembic migrations for production schema changes. Keep `create_all` only for test fixtures with in-memory DBs (`init_db(run_migrations=False)`). Alembic `add_column` migrations should be idempotent: check `PRAGMA table_info()` before adding.
