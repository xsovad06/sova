# Architecture

## Component Overview

SOVA has four main components:

### 1. CLI (`sova/cli/`)
- Python CLI built with Typer, entry point `sova` (via pyproject.toml)
- Subcommands: `run`, `watch`, `parallel`, `triage`, `harden`, `install`, `setup`, `uninstall`, `dashboard`, `server`, `commands`, `memory`, `status`, `costs`, `cleanup`, `doctor`, `address-pr`, `maintain-pr`, `review-pr`, `learn-from-pr`, `init-db`, `migrate-config`, `config` (show, `config set`), `mcp`, `supervisor`, `briefing`
- Registered in `sova/cli/app.py`, implementations in `sova/cli/commands/`

### 2. Agent Core (`sova/core/`, `sova/roles/`)
- `core/workflow.py` -- WorkflowEngine: executes step pipelines with DB persistence (TaskRun, StepExecution, FailureRecord)
- `core/state.py` -- 19-state TaskStatus StrEnum with transition validation
- `core/context.py` -- ExecutionContext dataclass threading state through steps
- `core/output.py` -- OutputWriter for per-run DB-backed output persistence, read_lines, retention cleanup
- `core/steps/`: 28 BaseStep implementations with execute/validate_output/verify_output/can_skip. Four pipeline variants:
  - **Developer pipeline** (17 steps): sync -> assess -> create_worktree -> capture_baseline -> develop -> simplify -> self_review -> commit -> validate -> push -> create_pr -> wait_for_external_reviews -> address_external_findings -> monitor_ci -> confidence_score -> extract_memory -> handoff_to_reviewer
  - **Address-review pipeline** (10 steps): ensure_worktree -> rebase -> address_review -> rearrange_commits -> validate -> push -> monitor_ci -> resolve_external_reviews -> extract_memory -> handoff_to_reviewer (re-review of the new head; `handoff_to_user` remains in `STEP_REGISTRY` for projects that configure a manual stop via `[pipelines] address_review`)
  - **Researcher pipeline** (4 steps): fetch_task -> research -> spec -> extract_memory
  - **Planner pipeline** (4 steps): scan_project -> generate_tasks -> validate_tasks -> extract_memory
- `core/dag.py` -- DAGExecutor: runs command-based workflow graphs with topological sort, condition evaluation, and cycle detection
- `roles/` -- AgentRole ABC with 6 implementations: triage, researcher, developer, reviewer, custom, planner
- `roles/custom.py` -- CustomRole: executes user-defined DAG workflows via DAGExecutor
- `roles/dispatcher.py` -- routes tasks to appropriate roles based on state; `get_role_async()` falls back to DB lookup for custom roles
- **Role chaining**: Developer -> Reviewer -> Developer handoff chain runs autonomously by default. `HandoffAction.auto_execute` triggers auto-spawn of the next agent when the current one exits. Developer writes handoff to Reviewer, Reviewer writes handoff back to Developer if findings exist, or to user if clean (manual "Integrate PR" button). The address-review pipeline closes the loop by handing back to the Reviewer (`HandoffToReviewerStep`, re-review of the new head) rather than to the user, since the `sova_reviewed` integration gate requires an approving verdict on the current head and an "addressed" verdict alone can never unlock integration; the Reviewer -> Developer direction stays bounded by `pipeline.max_address_review_cycles`. Both auto-spawn directions are configurable in `sova.toml`: `pipeline.auto_handoff` (Developer->Reviewer, default true) and `pipeline.auto_address_review` (Reviewer->Developer, default true). When disabled, handoff files are still written with `auto_execute=False` so the dashboard shows manual action buttons instead. Issue stays `IN_REVIEW` until human merges via `/integrate-pr` or `/approve-merge`.
- **Address-review budget is a resolver state, not a separate gate**: the cap on address-review cycles lives in `resolve_next_action()` (`sova/dashboard/services/work_state.py`), not in a dedicated circuit breaker. Its `review_budget_exhausted` rule compares `PRFacts.address_cycles` (from `resolve_sova_verdict()`, via `count_address_review_runs()`) against `PRFacts.max_address_cycles` (`pipeline.max_address_review_cycles`, default 3, 0=unlimited) ahead of every verdict- and thread-based rule, and resolves an over-budget PR to `WorkItemState.PR_REVIEW_EXHAUSTED` with `action_id="integrate"` so the human decides. The supervisor and the dashboard both have to thread the cap into `_build_pr_facts()`; the old `sova/supervisor/gates/circuit_breaker.py` gate and the `_ADDRESS_CYCLE_ACTIONS` block in `progression.py` were deleted. `_check_address_review_circuit_breaker()` (`agent_handoff.py`) survives only to stop the auto-handoff chain at agent-exit time, before the resolver's next poll. See `docs/architecture-deep-dive.md` for the full entry.
- **Step gate checks (two-phase)**: gate checking is split into a fast structural `validate_output()` (capped by `validation.gate_timeout`, default 60s) and an optional heavyweight `verify_output()` (uses `validation.verify_timeout` if nonzero, otherwise the full step timeout). `validate_output()` checks all forms of change: unstaged diff (`git diff --stat HEAD`), staged diff (`git diff --cached --stat`), commits ahead of base (`git log {base}..HEAD --oneline`), and untracked new files (`git status --porcelain`, lines starting with `??`). Never return `GateCheckResult(passed=True)` unconditionally. `verify_output()` defaults to a no-op (returns `passed=True`) so existing steps are unaffected; only `ValidateStep` overrides it to run regression checks (`_check_regressions`/test suite execution) which need longer timeouts. `WorkflowEngine._validate_step_gate()` runs `validate_output()` first and on success chains to `_verify_step_output()`. Both timeouts are configurable in `sova.toml` (`validation.gate_timeout`, `validation.verify_timeout`). `DevelopStep.validate_output()` additionally collects filenames from all change sources (unstaged, staged, committed, untracked) and verifies that at least one is a substantive source file (not just lockfiles or metadata like `.sova/`, `.claude/`) via `_NON_SUBSTANTIVE_RE`. `DevelopStep.execute()` returns `success=False` when the inner check loop fails: `_run_inner_check_loop()` returns a `(passed: bool, summary: str)` tuple, and any non-passing outcome (exhausted cycles, budget exceeded, no changes produced, test weakening, LLM fix failure) sets `passed=False`.
- **Context persistence at step boundaries**: `_sync_task_run_context()` persists `worktree_path`, `branch_name`, and `pr_number` to the TaskRun after every step. This ensures the checkpoint/resume system can restore context even if the run pauses or crashes mid-pipeline. Dashboard's `_create_task_run()` also accepts `pr_number` so reviewer runs spawned via auto-handoff have it recorded immediately (without relying on WorkflowEngine sync).

### 3. Dashboard (`sova/dashboard/`)
- Python/FastAPI web UI with app factory pattern (`create_app(project_dir=None)`)
- Jinja2 templates + Tailwind CSS (prebuilt via `make css`), Catppuccin dark theme
- 29 pages: dashboard, agents, run_detail, lifecycle, costs, pr_metrics, queue, reliability, specs, logs, settings, memory, setup, home, style_guide, roles, role_editor, commands, spec, control, overview, runs, tasks, base, supervisor, fleet, oversight, briefing, dependency_health
- **Design system**: CSS variables (Catppuccin Mocha) in `static/style.css`, Tailwind config in `tailwind.config.js` (repo root), SVG icon macro in `_icons.html`, component macros in `_components.html`. Run `make css` after adding or removing Tailwind classes in templates/JS (the prebuilt `static/tailwind.min.css` is checked in, not built by CI)
- 33 API routers under `/api`: auth, overview, runs, costs, control, feed, handoff, lifecycle, memory, models, logs, tasks, queue, quota, reliability, settings, setup, agents, work, roles, spec, prs, dependencies, resources, supervisor, oversight, fleet_manager, fleet_insights, telemetry, a2a, mcp, briefing, dependency_health
- 49 services: run, cost, memory, models, control (facade), feed, handoff, lifecycle, queue, batch, reliability, work, work_item, work_state, work_verdict, task, log, settings, setup, agent_lifecycle, agent_output, agent_recovery, agent_handoff, agent_pool, agent_db, agent_status, agent_context, agent_progress, agent_validation, agent_approval, agent_finalize, agent_resource, output (re-export facade for core/output), role, spec, pr, pr_metrics, resource, llm_suggestion, output_stream, fleet, fleet_manager, supervisor, oversight, telemetry_push, merge_queue_monitor, mcp_service, awareness, dependency_health
- Old pages (overview, control, runs, tasks, work) redirect to current equivalents (dashboard or agents)
- **Multi-agent control**: manages concurrent agent processes per project with slot limits and per-issue dedup. Both `start_agent()` and `start_command()` call `_check_issue_conflict()` which rejects duplicates via two checks: in-memory (`pa.agents`) and DB (`TaskRun` with alive PID). The DB check catches CLI-spawned agents not tracked in-memory. The `max_concurrent` slot check alone doesn't prevent same-issue duplicates. `_check_issue_conflict()` auto-recovers dead-PID DB runs by marking them "interrupted" on detection. When `force=True` (passed through from `start_agent`), both in-memory and live external conflicts are skipped so `--force` retries are not blocked by stale state.
- **Batch operations**: triage/harden multiple issues with parallel concurrency (`asyncio.Semaphore`, default 3 for triage, 2 for harden via `DEFAULT_CONCURRENCY`). `BatchJob.max_concurrency` configurable per-batch. Global progress bar in `base.html` (visible on all pages), batch ID persistence via `sessionStorage`, `GET /api/queue/batch/active` endpoint for discovering running batches after page navigation or browser refresh
- **Handoff system**: agents write `.claude/agent-control/handoff.json` to pass state between agents
  - `handoff_service.py` -- read/write/archive handoff files (mtime-cached)
  - Dashboard renders handoff action buttons on the agents page (awaiting_action/completed/failed). Failed runs also show a "Re-run" button that pre-fills issue, role, and PR number.
  - `_process_auto_handoff()` in `agent_handoff.py` auto-triggers `HandoffAction` entries with `auto_execute=True` after agent exit, enabling autonomous role chaining. The `auto_execute` flag is set per `pipeline.auto_handoff` / `pipeline.auto_address_review` config
  - Enables chaining: `integrate-pr` (full pipeline: rebase, CI, merge, cleanup)
- **Claude command execution**: `agent_lifecycle.start_command()` runs Claude Code commands from handoff actions (re-exported via `control_service` facade)
- Tests: `tests/test_dashboard.py` + `tests/test_batch_service.py` (pytest + httpx ASGITransport, in-memory SQLite via `sqlite+aiosqlite://`), run via `make test-py`

### 4. Scheduler (`sova/scheduler/`)
- `watch.py` -- WatchLoop: async poll with priority scan (RESEARCHED > TRIAGED > BACKLOG), veto window, asyncio.Event for shutdown
- `parallel.py` -- ParallelExecutor: asyncio.Semaphore for max_parallel_agents
- `server.py` -- SOVAServer: combined FastAPI dashboard + scheduler in one process, PID file lifecycle. Scheduler-specific API endpoints (`/api/scheduler/status`, `/api/scheduler/health`, `/api/scheduler/digest`) defined here, not in dashboard routers
- Deploy: `deploy/sova-server.service` (systemd) + `deploy/com.sova.server.plist` (launchd)
- CLI: `sova server start/stop/status/restart/digest/install-service`

## Model Selection System

Model routing is config-driven via three mechanisms: task-type-specific overrides (`llm.routing.{task_type}`), complexity-tier defaults (`llm.routing.{tier}`), and per-role fallbacks (`roles.{role}_model`). The system unifies LLM provider selection (Claude Code CLI, Anthropic API, LiteLLM multi-provider) behind an abstract `LLMProvider` ABC with pluggable error classification, availability checking, and fallback chaining. See [docs/model-selection-architecture.md](../../docs/model-selection-architecture.md) for the complete design, verified root causes, and migration roadmap.

**Always pin explicit model IDs in `sova.toml`, never generic aliases.** `model = "opus"` broke when Claude Code updated its alias resolution to `claude-opus-5`, which was unavailable on Vertex: generic aliases (`opus`, `sonnet`, `haiku`) are resolved by the Claude CLI, not SOVA, so a new model release silently changes the target. Use full IDs (e.g. `claude-opus-4-6`, `claude-sonnet-4-5@20250929`). Applies to `model`, `researcher_model`, `triage_model` in `[agent]`, `analysis_model` in `[oversight]`, and every entry in `fallback_models` lists (a stale ID there causes cascading fallback failures). Verify availability on Vertex before pinning (404 = not found, 429 = available but quota-limited) and check every project's `sova.toml`, not just the current one, when rotating model IDs. `route_model()` auto-pins bare aliases to `agent.model` when in the same family as a defense-in-depth backstop, but that is not a substitute for pinning explicitly. Issue #619, PR #702, #828.

**Alias resolution is backend-aware, not just provider-aware (#1033).** `llm.provider="claude-code"` is not the whole story: `CLAUDE_CODE_USE_VERTEX`/`CLAUDE_CODE_USE_BEDROCK` silently redirect the CLI to a deployment that rejects a bare tier name and needs a fully-qualified, dialect-specific ID. `sova/llm/backends.py` (leaf module, no intra-`sova` imports) adds `detect_backend(cfg, env=None)` (provider + those two env vars, read from `env` if given else `os.environ` -> `firstparty`/`vertex`/`bedrock`/`litellm`), `routing_env_vars_present()` (a raw-`os.environ` pre-check for either var), `backend_can_serve(backend, model_id)` (fail-open except: a bare tier name on vertex/bedrock, an `@`-pinned Vertex ID on firstparty, or a Bedrock-dialect `anthropic.*`/`us.anthropic.*` ID off bedrock), and `tier_candidates_for()` (the ordered, servable candidate list for vertex/bedrock, built-in or `llm.tier_candidates`-overridden). `sova/llm/client.py:resolve_alias()` is the one choke point both `select_model()` and `create_provider()` share: it tries a `"{backend}:{alias}"`-scoped key in `llm.model_aliases` before the bare key, and when the result is a known-unservable tier name or pinned ID for the detected backend (including one reached directly rather than via an alias, via `tier_for_known_candidate()`), falls through to a servable one, SOVA-resolving a bare firstParty tier name itself (via `sova.llm.models.resolve_model_alias()`) rather than leaving it for the CLI; on `cfg.provider == "vertex"` the resolved candidate is additionally given the `vertex_ai/` prefix litellm requires, since that provider routes through `LiteLLMProvider` rather than the CLI. Because `resolve_alias()` runs in the parent process, not the spawned Claude CLI child whose model it is resolving, it detects the backend from that child's actual (scrubbed) environment (`scrub_agent_env(passthrough=configured_passthrough())`, built only when `cfg.provider == "claude-code"` and `routing_env_vars_present()` finds a routing var set), rather than this process's own raw `os.environ`, so a routing var set in the parent but not forwarded via `agent.env_passthrough` is never mistaken for actual routing. `tier_for_known_candidate()` lookup strips a leading `vertex_ai/` prefix first, since a value pinned while `cfg.provider == "vertex"` carries that litellm prefix and the candidate table stores bare IDs; without the strip, a pinned value whose backend later drifted off vertex was never recognized as needing correction (CodeRabbit, PR #1106). `llm.resolve_tier_aliases=false` restores byte-identical passthrough. See `docs/model-selection-architecture.md` section 2.7 for the full design.

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
- **DB URL**: `SOVA_DATABASE_URL` env var for PostgreSQL; defaults to `.claude/sova.db` (SQLite)
- **Budget limits**: per-run and per-issue caps prevent runaway costs. See `docs/performance-guidelines.md` for defaults and config keys.
- **Settings metadata registry**: every field in a config model (`models.py`) must also have a `SettingMeta` entry in `sova/dashboard/settings_meta.py` with key, label, description, group, and value_type. Without it, the field won't appear in the dashboard settings UI. When adding a new config group (e.g., `external_reviews`), also add it to `GROUPS` dict and `GROUP_ORDER` list.

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

Full narrative entries (the reasoning, the incident that motivated a fix, and the exact files touched) live in `docs/architecture-deep-dive.md` rather than always-loaded here. No historical knowledge was deleted, only relocated and indexed. Grep that file for a topic below to read its full entry:

- A network outage is a first-class condition, and "could not verify" is never "verified bad"
- An unverifiable outcome is recorded as `interrupted`, and an outage must not spend the address-review budget
- Network self-heal is narrow by construction, and resumes rather than respawns
- Cancellation must never abandon a DB transaction
- A model column with no migration is invisible until every query on that table fails
- A log line that parses as JSON is not necessarily a JSON object
- Python for SOVA
- Role-based agents
- Gate checks between steps
- Complexity-based timeout multipliers
- Partial work preservation on timeout
- Ephemeral agents
- Worktree isolation
- Adapter pattern for task sources
- Mandatory pipeline
- Split pipelines at role boundaries
- Issue state ownership is human
- Handoff protocol
- Short-lived agent model
- Markdown commands
- Persona auto-detection
- DB persistence
- Per-step token attribution
- Unified TaskRun via `--run-id` passthrough
- Combined server
- Idempotent finalization
- Stale run recovery + dismiss
- Lifespan shutdown must cancel ALL background tasks
- Subprocess isolation via `start_new_session=True`
- Environment scrubbing at the spawn boundary is the only correct fix for inherited provider-routing variables
- `check_available()` must report authentication, not just installation
- Codex credential ownership: keyring-first, `CODEX_API_KEY` opt-in scoped to the Codex child only
- Reviewer spawns as a trusted subprocess, not through a coding-agent runtime sandbox
- Reviewer post failure must not trigger address-review
- SOVA review verdict: one canonical assembly path, DB-reconciled label, SHA-anchored staleness
- An address cycle must be visible on GitHub and must supersede a verdict regardless of where that verdict was read from
- `_post_review()` must never raise; `_write_handoff()` must always run
- Pipeline variant detection must gate on `current_step`
- Roles must self-discover missing context
- PR deduplication in CreatePRStep
- State-adopting steps must replicate all side effects
- macOS notifications via terminal-notifier
- Dual handoff persistence (file + DB)
- Auto-handoff must clear handoff file before spawning next agent
- Stale review-only handoff bypass
- Seed cross-agent data before clearing the handoff file
- Adapter ABC contract
- LLM for user-facing outputs with structured fallback
- JIRA-aware pipeline outputs
- Rebase with LLM conflict resolution
- A resolution attempt is never trusted on the LLM's own report
- `sync_branch` stash guard
- Command distribution never writes through a symlink
- Inherited `.claude/` state is never the agent's own work, at two layers
- Worktree `.claude/` sync tolerates symlinked shared commands
- A reused worktree must prove it is usable before an agent is spawned into it, not after the agent discovers a missing command
- Pydantic request models must be at module scope in `app.py`
- Thin re-export wrappers during module splits
- Never non-visible overflow on containers with popout children
- Operations persona for oversight
- Awareness is read-only and deliberately separate from `TaskAdapter`
- Removing the config file means every registered setting needs a real editing surface, and two of them had none
- Memory extraction is a no-op
- Issue Lifecycle Control
- `classify_error()` is scoped to LLM invocation details and must not be pointed at arbitrary `TaskRun.error_message` text
- Doc counts drift after refactors
- Test count in AGENTS.md is a rounded-down-to-the-nearest-hundred estimate, never an exact figure
- Stale references persist after file/feature renames
- Adapter-type guards must not assume GitHub
- CI failure auto-recovery in MonitorCIStep
- Guard against no-op pushes in LLM fix loops
- Fix-loop LLM timeouts get a distinct marker, and the inner check loop stops before the outer step timeout kills it uncleanly, without stealing the runaway guard's pause/resume path
- Failure capture must prefer stdout's structured error over stderr, and a bare exit code is never enough to triage
- Address-review pipeline needs independent worktree discovery
- An unresolvable `branch_name` must fail loudly, not silently reach `git push`
- Force-push decisions come from a reachability check, not the `ctx.pr_number` proxy
- `WorkflowEngine` DB session calls resolve `project_dir` explicitly, not via the ambient contextvar
- Supervisor auto-rebase must reuse a branch's existing worktree, never collide with it
- Address-review finding loading uses four fallback sources
- External review thread lifecycle
- address-pr wrong-directory failure mode
- Verify `gh auth` account before review thread operations
- Headless agent autonomy
- Direct subprocess spawn for pipeline roles
- Two CLI invocation paths share one argv builder, and the prompt is never on that argv
- Pipeline step invocations are task_type-tagged for config-driven routing
- Headless agent prompts must frame CLI commands as bash code blocks
- Dashboard JS polling must clear stale UI on negative path
- New config sections need quadruple registration
- Shared single-instance UI state needs its own toggle helpers, not ad-hoc mutation
- Per-issue handoff files for parallel agent isolation
- `recover_stale_runs` must check external state for merge-role runs
- Background merge queue monitor
- Config-driven provider selection must be wired at startup
- `pull_request_target` reads workflow from base branch, not PR branch
- PR spam gate closes drive-by bounty PRs without blocking newcomers
- Pipeline outcome validation detects bypass at exit time
- Orphaned step finalization on terminal transition
- Auto-retry for recoverable pipeline failures: REMOVED.
- `budget_override` is distinct from `force`
- GitHub API calls must not run inside DB write transactions in sweep/recovery paths
- `_resolve_issue_worktree` creates worktrees proactively
- Subprocess agents must resolve project root from linked worktree CWD
- `start_agent()` recovers pr_number from DB history for developer role
- `PR_CHANGES_REQUESTED` split into `PR_SOVA_CHANGES` and `PR_EXTERNAL_CHANGES`
- Review-completed gate: three-source check plus thread resolution and run-completion requirements
- Integration gates default to on, and the label source of the review-completed gate excludes explicit rejections
- Unresolved-thread state is three-valued, but only an explicit unknown fails closed
- One shared next-action resolver replaces two independently-computed state machines
- Dual-evaluation PR state experiment
- A documentation push must not re-run the expensive CI suite, and the whole-PR test cannot deliver that
- `/integrate-pr` must not push on its own account; knowledge capture belongs to `/address-pr`
- Chat-style activity cockpit
- Confidence scoring is advisory unless gated
- Step pipelines are config-overridable, with two read-only consumers still assuming built-ins
- A new `ComputedPRState` fans out across four independent maps; missing one fails open to the wrong label
- Dashboard access control: single-user loopback model, no accounts
- A read-only provider status widget must not lift the availability check's fail-open contract into its own "authenticated" answer
- A convention that permits N near-duplicate implementations still needs a shared helper once SonarCloud's 3% new-code duplication gate is in scope
- Planner prompt sections are row-capped, not character-budgeted, and the health widget is in-memory only
- `.claude/commands/` is a rendered build artifact, not a hand-maintained tree
- Marketplace plugin is generated, never hand-authored
- State label swaps must replace the full label array in one call, not clear-then-add
- A stale `already_running` gate result must not permanently starve an issue's later gates
- A stale `RESET_STALE_STATE` candidate must not roll back an issue that already has an open PR
- The review loop must close without a human click, and every resolver action must be a supervisor action
- Address-review budget is a resolver state, not a separate gate
- A review is only trackable if each finding gets its own thread, and both review paths must produce one
- Termination provenance is recorded at the point the signal is sent, not guessed at exit
- The memory guard's runtime check lives in the watchdog's existing poll loop, not a new gate or poller
- `sova run` installs its own SIGTERM handler so the grace period before SIGKILL is spent preserving work, not lost to the default disposition
- Codex skills are mechanically rendered from `commands/*.md`, not hand-authored, and every SOVA-managed `.agents/skills/` entry is name-prefixed rather than directory-separated

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
