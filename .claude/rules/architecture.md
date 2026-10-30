# Architecture

## Component Overview

SOVA has four main components:

### 1. CLI (`sova/cli/`)
- Python CLI built with Typer, entry point `sova` (via pyproject.toml)
- Subcommands: `run`, `watch`, `parallel`, `triage`, `harden`, `install`, `setup`, `uninstall`, `dashboard`, `server`, `commands`, `memory`, `status`, `costs`, `cleanup`, `doctor`, `address-pr`, `maintain-pr`, `review-pr`, `learn-from-pr`, `init-db`, `migrate-config`, `config` (show, `config get`, `config set`), `mcp`, `supervisor`, `briefing`, `assess` (`assess set`, `assess get`, `assess clear`), `db` (`db status`, `db backup`), `checks` (`checks should-skip`, `checks record`)
- Registered in `sova/cli/app.py`, implementations in `sova/cli/commands/`

### 2. Agent Core (`sova/core/`, `sova/roles/`)
- `core/workflow.py` (WorkflowEngine): executes step pipelines with DB persistence (TaskRun, StepExecution, FailureRecord)
- `core/state.py`: 19-state TaskStatus StrEnum (no transition validation: `TaskRun.status` writes come from several pipeline variants and recovery paths that don't fit one table)
- `core/context.py`: ExecutionContext dataclass threading state through steps
- `core/output.py`: OutputWriter for per-run DB-backed output persistence, read_lines, retention cleanup
- `core/steps/`: 30 BaseStep implementations with execute/validate_output/verify_output/can_skip. Four pipeline variants:
  - **Developer pipeline** (17 steps): sync -> assess -> create_worktree -> capture_baseline -> develop -> simplify -> self_review -> commit -> validate -> push -> create_pr -> wait_for_external_reviews -> address_external_findings -> monitor_ci -> confidence_score -> extract_memory -> handoff_to_reviewer
  - **Address-review pipeline** (11 steps): ensure_worktree -> rebase -> address_review -> rearrange_commits -> validate -> push -> refresh_pr_body -> monitor_ci -> resolve_external_reviews -> extract_memory -> handoff_to_reviewer (re-review of the new head; `handoff_to_user` remains in `STEP_REGISTRY` for projects that configure a manual stop via `[pipelines] address_review`)
  - **Researcher pipeline** (4 steps): fetch_task -> research -> spec -> extract_memory
  - **Planner pipeline** (4 steps): scan_project -> generate_tasks -> validate_tasks -> extract_memory
- `core/dag.py` (DAGExecutor): runs command-based workflow graphs with topological sort, condition evaluation, and cycle detection
- `core/agent_dispatch.py`: dispatch boundary between the text-only LLMProvider path and AgentRuntime. `dispatch_command()`/`dispatch_prompt()` route a step declaring `requires_tools = True` through `sova.ipc.runtime.get_runtime()` and fail fast when that runtime is unusable, instead of silently degrading to a provider that cannot edit files. Every step whose own `/command` or assembled prompt edits the git worktree sets the flag and calls through this boundary: `DevelopStep`, `ValidateStep` and `MonitorCIStep` (their LLM fix loops, via `dispatch_prompt()`), `SelfReviewStep`, `SimplifyStep`, `AddressReviewStep` and `AddressExternalFindingsStep` (both route their findings prompt through `dispatch_prompt()` since neither is a named slash command), and `RearrangeCommitsStep`. `ResearchStep` deliberately stays on `invoke_command()`/LLMProvider: `/research` never edits the worktree, only appends a `## Research` section to the issue/ticket body via `gh`/`jira` CLI calls, and its own `validate_output()` already gates on that section actually existing, independent of which provider ran.
- `roles/`: AgentRole ABC with 6 implementations: triage, researcher, developer, reviewer, custom, planner
- `roles/custom.py` (CustomRole): executes user-defined DAG workflows via DAGExecutor
- `roles/dispatcher.py`: routes tasks to appropriate roles based on state; `get_role_async()` falls back to DB lookup for custom roles
- **Role chaining**: Developer -> Reviewer -> Developer handoff chain runs autonomously by default via `HandoffAction.auto_execute`, bounded by `pipeline.max_address_review_cycles`; both auto-spawn directions (`pipeline.auto_handoff`, `pipeline.auto_address_review`) are configurable in `sova.toml`. Issue stays `IN_REVIEW` until a human merges via `/integrate-pr`. See `docs/architecture-deep-dive.md` for the full entry.
- **Address-review budget is a resolver state, not a separate gate**: the cap on address-review cycles lives in `resolve_next_action()` (`sova/dashboard/services/work_state.py`), not in a dedicated circuit breaker. Its `review_budget_exhausted` rule compares `PRFacts.address_cycles` (from `resolve_sova_verdict()`, which derives it from `parse_review_history()`'s count of `sova-addressed` GitHub review markers) against `PRFacts.max_address_cycles` (`pipeline.max_address_review_cycles`, default 3, 0=unlimited) ahead of every verdict- and thread-based rule, and resolves an over-budget PR to `WorkItemState.PR_REVIEW_EXHAUSTED` with `action_id="integrate"` so the human decides. The supervisor and the dashboard both have to thread the cap into `_build_pr_facts()`; the old `sova/supervisor/gates/circuit_breaker.py` gate and the `_ADDRESS_CYCLE_ACTIONS` block in `progression.py` were deleted. `_check_address_review_circuit_breaker()` (`agent_handoff.py`) survives only to stop the auto-handoff chain at agent-exit time, before the resolver's next poll. `PRFacts.review_history_unresolved` (set from `work_verdict.py`'s `UNRESOLVED_KEY`) makes the `review_budget_exhausted` rule itself fail closed during a GitHub outage, mirroring the circuit breaker's own fail-closed check, rather than reading an unresolvable history as "0 cycles completed" and risking an uncapped loop; both are gated on `max_address_cycles > 0`, since an unlimited budget has nothing to fail closed on. See `docs/architecture-deep-dive.md` for the full entry.
- **Step gate checks (two-phase)**: gate checking is split into a fast structural `validate_output()` (capped by `validation.gate_timeout`, default 60s) and an optional heavyweight `verify_output()` (uses `validation.verify_timeout` if nonzero, otherwise the full step timeout). `validate_output()` checks all forms of change: unstaged diff (`git diff --stat HEAD`), staged diff (`git diff --cached --stat`), commits ahead of base (`git log {base}..HEAD --oneline`), and untracked new files (`git status --porcelain`, lines starting with `??`). Never return `GateCheckResult(passed=True)` unconditionally. `verify_output()` defaults to a no-op (returns `passed=True`) so existing steps are unaffected; only `ValidateStep` overrides it to run regression checks (`_check_regressions`/test suite execution) which need longer timeouts. `WorkflowEngine._validate_step_gate()` runs `validate_output()` first and on success chains to `_verify_step_output()`. Both timeouts are configurable in `sova.toml` (`validation.gate_timeout`, `validation.verify_timeout`). `DevelopStep.validate_output()` additionally collects filenames from all change sources (unstaged, staged, committed, untracked) and verifies that at least one is a substantive source file (not just lockfiles or metadata like `.sova/`, `.claude/`) via `_NON_SUBSTANTIVE_RE`. `DevelopStep.execute()` returns `success=False` when the inner check loop fails: `_run_inner_check_loop()` returns a `(passed: bool, summary: str)` tuple, and any non-passing outcome (exhausted cycles, budget exceeded, no changes produced, test weakening, LLM fix failure) sets `passed=False`.
- **Context persistence at step boundaries**: `_sync_task_run_context()` persists `worktree_path`, `branch_name`, and `pr_number` to the TaskRun after every step. This ensures the checkpoint/resume system can restore context even if the run pauses or crashes mid-pipeline. Dashboard's `_create_task_run()` also accepts `pr_number` so reviewer runs spawned via auto-handoff have it recorded immediately (without relying on WorkflowEngine sync).

### 3. Dashboard (`sova/dashboard/`)
- Python/FastAPI web UI with app factory pattern (`create_app(project_dir=None)`)
- Jinja2 templates + Tailwind CSS (prebuilt via `make css`), Catppuccin dark theme
- 25 templates: agents, run_detail, lifecycle, costs, pr_metrics, queue, reliability, specs, logs, settings, connections, memory, setup, home, style_guide, roles, role_editor, commands, spec, base, supervisor, fleet, oversight, briefing, dependency_health
- **Design system**: CSS variables (Catppuccin Mocha) in `static/style.css`, Tailwind config in `tailwind.config.js` (repo root), SVG icon macro in `_icons.html`, component macros in `_components.html`. Run `make css` after adding or removing Tailwind classes in templates/JS (the prebuilt `static/tailwind.min.css` is checked in, not built by CI)
- 33 API routers under `/api`: auth, overview, runs, costs, feed, handoff, health, lifecycle, memory, models, logs, tasks, queue, quota, reliability, settings, setup, agents, work, roles, spec, prs, dependencies, resources, supervisor, oversight, fleet_manager, fleet_insights, telemetry, a2a, mcp, briefing, dependency_health
- 48 services: run, cost, memory, models, control (facade), feed, handoff, health, lifecycle, queue, batch, reliability, work, work_item, work_state, work_verdict, log, settings, setup, agent_lifecycle, agent_output, agent_recovery, agent_handoff, agent_pool, agent_db, agent_status, agent_context, agent_progress, agent_validation, agent_approval, agent_finalize, agent_resource, role, spec, pr, pr_metrics, resource, llm_suggestion, output_stream, fleet, fleet_manager, supervisor, oversight, telemetry_push, merge_queue_monitor, mcp_service, awareness, dependency_health
- Old pages (dashboard, overview, control, runs, tasks, work) redirect to the current equivalent (agents)
- **State display has one source**: `work_state.py` owns every state label and color token (`state_label()`, `state_color()`; WorkItemState, ComputedPRState, and the tracker states the queue and dependency graph emit, with an explicit humanized/`gray` fallback for an unregistered state, which is also logged once per process so a state nobody registered is noticed). A PR state's label and color are read from the `WorkItemState` that names the same condition (`_PR_STATE_WORK_ITEM`), so a PR row and the task card showing that PR cannot disagree on either; only `approved` is written out, in its own shade so it stays apart from Ready to Merge (`green-strong`). The tracker-only table holds just the `TaskState` values `WorkItemState` lacks (`in_review` green, `on_qa` peach, neither sharing blue with `in_progress`). Each state-emitting API attaches `state_label`/`state_color` (work items and their `pr_details`, open PRs, queue items, `/api/tasks/issues`, dependency-graph nodes plus `pr_state_label`/`pr_state_color`, both built from the PR's raw computed state, not the graph's collapsed `pr_state`); the client only turns a token into badge classes (`stateBadgeClasses()`) or a hex (`stateHex()` via `SOVA_COLORS`) from `app.js`, and keeps no state table of its own. The supervisor graph legend is rendered from the label and token on the nodes on screen, so it cannot describe a second palette. Node colors follow those tokens, which is a deliberate visible change from the graph's old local `stateColor()` (every open state gray): `triaged` and `needs_spec` nodes draw yellow and `researched` purple. Likewise a PR row's "Draft" and "In Review" now read "Draft PR" and "Review Addressed" (PRMonitor's macOS notification too), and the task card's Ready to Merge moved from `green` to `green-strong` (badge `/20` to `/30`). `stateHex()` gives `green-strong` its own shade, so the graph tells In Review from Done, and `gray-dim` (Human Only) its own `surface2` shade, so the legend never shows Backlog, Closed and Human Only as three identical gray dots; the graph's Human, Interactive, Queued and run-status markers read `stateHex()`/`SOVA_COLORS` as well, never a hex literal.
- **Shared client helpers have one definition each**: `escapeHtml`, the `esc` alias, `timeAgo`, `setText`, `renderSectionError` and `statusBadge(status, classMap)` (returns an escaped HTML pill) live in `app.js` and nowhere else; `app.js` loads after `_head.html`, so a page's inline script may call them only from the scripts block or from a function that runs after load (`tests/test_dashboard_agents_modules.py` checks both). `timeAgo` reads a future timestamp (clock skew) as "just now", where queue.html's former private copy showed nothing.
- **Multi-agent control**: manages concurrent agent processes per project with slot limits and per-issue dedup. Both `start_agent()` and `start_command()` call `_check_issue_conflict()` which rejects duplicates via two checks: in-memory (`pa.agents`) and DB (`TaskRun` with alive PID). The DB check catches CLI-spawned agents not tracked in-memory. The `max_concurrent` slot check alone doesn't prevent same-issue duplicates. `_check_issue_conflict()` auto-recovers dead-PID DB runs by marking them "interrupted" on detection. When `force=True` (passed through from `start_agent`), both in-memory and live external conflicts are skipped so `--force` retries are not blocked by stale state.
- **Batch operations**: triage/harden multiple issues with parallel concurrency (`asyncio.Semaphore`, default 3 for triage, 2 for harden via `DEFAULT_CONCURRENCY`). `BatchJob.max_concurrency` configurable per-batch. Global progress bar in `base.html` (visible on all pages), batch ID persistence via `sessionStorage`, `GET /api/queue/batch/active` endpoint for discovering running batches after page navigation or browser refresh
- **Handoff system**: agents write `.claude/agent-control/handoff.json` to pass state between agents
  - `handoff_service.py`: read/write/archive handoff files (mtime-cached)
  - Dashboard renders handoff action buttons on the agents page (awaiting_action/completed/failed). Failed runs also show a "Re-run" button that pre-fills issue, role, and PR number.
  - `_process_auto_handoff()` in `agent_handoff.py` auto-triggers `HandoffAction` entries with `auto_execute=True` after agent exit, enabling autonomous role chaining. The `auto_execute` flag is set per `pipeline.auto_handoff` / `pipeline.auto_address_review` config
  - Enables chaining: `integrate-pr` (full pipeline: rebase, CI, merge, cleanup)
- **Claude command execution**: `agent_lifecycle.start_command()` runs Claude Code commands from handoff actions (re-exported via `control_service` facade)
- Tests: `tests/test_dashboard.py` + `tests/test_batch_service.py` (pytest + httpx ASGITransport, in-memory SQLite via `sqlite+aiosqlite://`), run via `make test-py`

### 4. Scheduler (`sova/scheduler/`)
- `watch.py` (WatchLoop): async poll with priority scan (RESEARCHED > TRIAGED > BACKLOG), veto window, asyncio.Event for shutdown. Dispatches one task at a time (`max_parallel_agents` has no effect here; `sova parallel` is the concurrent path)
- `server.py` (SOVAServer): combined FastAPI dashboard + scheduler in one process, PID file lifecycle. Scheduler-specific API endpoints (`/api/scheduler/status`, `/api/scheduler/health`, `/api/scheduler/digest`) plus the general `/api/health` (reports the package version and git SHA, and `degraded` once the watch loop's `last_scan_at` exceeds three times `watch.interval_idle` with no task in flight, or, if no scan has ever completed, once that same threshold has passed since process start, so a loop whose `scan()` fails on every attempt does not report `ok` forever) are defined here, not in dashboard routers
- Deploy: `deploy/sova-server.service` (systemd user unit; `ReadWritePaths` must list the whole project dir, not just its `.claude/`, because agents write `{project}/.git` too) + `deploy/com.sova.server.plist` (launchd) + `deploy/com.sova.server.newsyslog.conf` (reference newsyslog.d entry for the plist's unrotated stdout/stderr files; `install-service --type launchd` generates the path-substituted copy). `uvicorn.run(access_log=False)` in `SOVAServer.run()`: an access-log line per dashboard poll is what made those files grow unbounded
- CLI: `sova server start/stop/status/restart/digest/install-service`

## Model Selection System

Model routing is config-driven via three mechanisms: task-type-specific overrides (`llm.routing.{task_type}`), complexity-tier defaults (`llm.routing.{tier}`), and per-role fallbacks (`roles.{role}_model`). The system unifies LLM provider selection (Claude Code CLI, Anthropic API, LiteLLM multi-provider) behind an abstract `LLMProvider` ABC with pluggable error classification, availability checking, and fallback chaining. See [docs/model-selection-architecture.md](../../docs/model-selection-architecture.md) for the complete design, verified root causes, and migration roadmap.

**Always pin explicit model IDs in `sova.toml`, never generic aliases.** A bare alias (`opus`, `sonnet`, `haiku`) is resolved by the Claude CLI, not SOVA, so a new CLI release can silently point it at a model unavailable on Vertex (issue #619, PR #702, #828). Applies to `model`/`researcher_model`/`triage_model` in `[agent]`, `analysis_model` in `[oversight]`, and every `fallback_models` entry. Verify availability on Vertex before pinning (404 = not found, 429 = quota-limited). `route_model()` auto-pins bare aliases to `agent.model` as a defense-in-depth backstop, but that is not a substitute for pinning explicitly. See `docs/model-selection-architecture.md` section 2.3 for the pinning mechanism.

**Discovery is proactive enumeration, not an on-demand probe (#1031, A1-A5, #1039).** `GET /api/models/available?layer=llm|runtime[&refresh=1]` (`sova/dashboard/services/models_service.py:get_available_models()`) feeds every LLM- and runtime-layer model dropdown (settings page, launch modal), backed by `LLMProvider.list_available_models(*, allow_probe=True)` (`sova/llm/provider.py`), which three of the four concrete providers override to call their real catalog: `AnthropicAPIProvider` (SDK `models.list()`), `ClaudeCodeProvider` (auth-probe plus a probe per curated candidate, since the CLI has no listing endpoint), and `LiteLLMProvider` (fans out to Vertex, Ollama, and an OpenAI-compatible endpoint). Fail-open per the Q2 guarantee: an unauthenticated, misconfigured, or timed-out provider reports `source: "curated"` and is never cached, so the next poll always retries; `layer="runtime"` instead reports an unreachable runtime as `source: "unavailable"` (also uncached) and a reachable one with no catalog as `source: "reachable"` (cached, since `check_available()` already confirmed it). See `docs/model-selection-architecture.md` section 2.9 for the full design.

**Layer-1 enumeration is a separate axis from layer-2, and an "unavailable" answer expires fast (#1038).** `agent.runtime` carries its own credential, so an Anthropic API key can make layer 2 (`llm.provider`) work while the Claude Code CLI stays unauthenticated, and `codex login` is the reverse. `AgentRuntime.list_available_models()` (`sova/ipc/runtime.py`) is the layer-1 counterpart of `LLMProvider.list_available_models()`: `None` means unavailable, `[]` means reachable with no model IDs of its own; `_RuntimeModelCache` splits its TTL between the two (1800s for a real catalog, 300s for "unavailable", since a mid-session `claude login` must become visible quickly). `get_model_capabilities()` (`sova/llm/provider.py`) composes both layers, each probed independently under its own timeout; the layer-1 catalog itself reaches the UI only via `layer="runtime"`, feeding `roles.developer_runtime_model` and the launch modal's per-run Model select (#1039), applied to tool-using steps via `resolve_runtime_model_pin()`. See `docs/model-selection-architecture.md` section 2.10 for the full design.

**Alias resolution is backend-aware, not just provider-aware (#1033).** `CLAUDE_CODE_USE_VERTEX`/`CLAUDE_CODE_USE_BEDROCK` silently redirect the CLI to a deployment that rejects a bare tier name and needs a fully-qualified, dialect-specific ID. `sova/llm/backends.py` adds `detect_backend()`, `backend_can_serve()`, and `tier_candidates_for()`; `sova/llm/client.py:resolve_alias()` is the one choke point `select_model()`, `create_provider()`, and `llm_suggestion_service.py` all share, falling through to a servable candidate when the resolved one is unservable for the backend detected from the spawned CLI child's actual (scrubbed) environment. `llm.resolve_tier_aliases=false` restores byte-identical passthrough. See `docs/model-selection-architecture.md` section 2.7 for the full design.

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
- **DB URL**: `SOVA_DATABASE_URL` env var for PostgreSQL; defaults to `.claude/sova.db` (SQLite). One database per project is the contract, never a shared one: `TaskRun.project_slug` is declared but never written and `CostRecord` has no such column, so two projects on one database mix runs and costs and each liveness sweep marks the other's runs interrupted. The deploy templates deliberately ship no `SOVA_DATABASE_URL` example for this reason; see `docs/database-guidelines.md` "PostgreSQL: one database per project, never shared"
- **Budget limits**: per-run and per-issue caps prevent runaway costs. See `docs/performance-guidelines.md` for defaults and config keys.
- **Settings metadata registry**: every field in a config model (`models.py`) must also have a `SettingMeta` entry in `sova/dashboard/settings_meta.py` with key, label, description, group, and value_type. Without it, the field won't appear in the dashboard settings UI. When adding a new config group (e.g., `external_reviews`), also add it to `GROUPS` dict and `GROUP_ORDER` list.
- **Enumerable settings** (`SettingMeta.options_source`): a field whose valid values can only be discovered at runtime sets `options_source` (`"llm"` or, since #1039, `"runtime"`, each served by `GET /api/models/available?layer=<options_source>`) instead of the static `options` tuple that `value_type="select"` uses. `get_grouped_config()` passes it through, and `settings.html` renders such a field as a `<optgroup>`-grouped dropdown (built by `app.js`'s shared `appendModelOptgroups()`, vendor-labelled via its `MODEL_FAMILY_LABELS` map, e.g. `anthropic` -> "Claude", `openai-oss` -> "gpt-oss"; unmapped families fall back to their raw name, and the map lives in `app.js` rather than one page's inline script so the four grouped model dropdowns cannot label the same family two ways) plus a "Custom..." free-text escape hatch, so a valid-but-unlisted model ID is never blocked. The editor is a progressive upgrade, not a replacement: it mounts the plain text input first and swaps in the dropdown only once discovery resolves, so a failed, empty, or slow enumeration always leaves a usable free-text field (fail open). The client treats a 200 response carrying `models_service`'s own curated fallback (`source: "curated"`, returned while a provider is unauthenticated, misconfigured, or timed out) the same as a fetch failure: neither is cached, both leave the plain text editor mounted, so the dropdown is never stuck showing the wrong provider's models after the user fixes auth on the Connections page. Any new single-string setting naming an LLM-layer model should set `options_source="llm"`, and a runtime-layer one `options_source="runtime"` (`roles.developer_runtime_model`, #1039). `codex.model` stays free text regardless: `CodexRuntime`'s enumeration can only echo back that same configured value, so a dropdown sourced from it offers no alternative to pick. A list-valued model setting (`agent.fallback_models`) also sets `options_source="llm"`: it keeps the plain comma-separated list editor as the source of truth and adds a grouped `<select>` beside it with an explicit "Add" button that appends the chosen id to that text rather than replacing it (acting on the select's own `change` event instead would append every option a keyboard user's arrow-key traversal passed over, not just the one picked), and renders the entries beneath as an ordered list with Up/Down/Remove controls that rewrite that same text, so the order shown is exactly the order saved and walked by `_invoke_with_fallback` (#1036), while still making entries typo-proof. The launch modal's per-run "Internal LLM model (this run only)" override is a separate, never-persisted control: see `docs/model-selection-architecture.md` section 2.8. `conflict_resolution.models` is also list-valued and eligible for the same list-picker treatment, but was left out of scope for this restoration. `llm.routing` (object-valued) has no enumerable editor.

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
- `make lint`: shellcheck + ruff
- `make format`: ruff auto-format

## Key Design Decisions

Full narrative entries (the reasoning, the incident that motivated a fix, and the exact files touched) live in `docs/architecture-deep-dive.md` rather than always-loaded here. No historical knowledge was deleted, only relocated and indexed. Grep that file for a topic below to read its full entry:

- A network outage is a first-class condition, and "could not verify" is never "verified bad"
- An unverifiable outcome is recorded as `interrupted`, and an outage must not spend the address-review budget
- Network self-heal is narrow by construction, and resumes rather than respawns
- Cancellation must never abandon a DB transaction
- A model column with no migration is invisible until every query on that table fails
- A log line that parses as JSON is not necessarily a JSON object
- An unbounded dependency upper bound (`<1.0`) lets CI silently install a release that was never tested against
- Python for SOVA
- Role-based agents
- Role chaining
- Gate checks between steps
- Complexity-based timeout multipliers
- Partial work preservation on timeout
- A failed agent dispatch still owns the edits it already made
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
- The Connections page builds a candidate provider without an `LLMConfig`, and only persists one that re-validates
- Codex credential ownership: keyring-first, `CODEX_API_KEY` opt-in scoped to the Codex child only
- Reviewer spawns as a trusted subprocess, not through a coding-agent runtime sandbox
- `OPENAI_API_KEY` is scrubbed like `CODEX_API_KEY` and re-injected only for `spawn_direct()`'s trusted pipeline subprocess, and Codex prompt delivery moves to stdin when the CLI documents support for it
- Reviewer post failure must not trigger address-review
- GitHub review markers are the sole SOVA verdict source (supersedes the earlier DB/label/output-parsing assembly)
- An address cycle must be visible on GitHub and must supersede a verdict regardless of where that verdict was read from
- `_post_review()` must never raise; `_write_handoff()` must always run
- Pipeline variant detection must gate on `current_step`
- Roles must self-discover missing context
- `ctx.task` has two independent fail-open fetches, one bounded and one not
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
- A CI check lookup must bind to the SHA it polled for, not the PR
- A CLI's non-zero exit is a result, not automatically a fetch failure
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
- address-pr own-PR and push-access guard
- `.claude/scripts/ci-failures.sh` project hook contract
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
- Integration gates default to on, and the label source of the review-completed gate excludes explicit rejections (integration-gates half superseded; the review-completed label source still applies, see the next entry)
- `IntegrationGatesConfig` and its three independent evaluators: REMOVED. `resolve_next_action()` is the sole source of truth for whether Integrate is offered
- Unresolved-thread state is three-valued, but only an explicit unknown fails closed
- One shared next-action resolver replaces two independently-computed state machines
- Dual-evaluation PR state experiment
- A documentation push must not re-run the expensive CI suite, and the whole-PR test cannot deliver that
- SonarCloud consumes CI's coverage artifact via `workflow_run` instead of running its own copy of the test suite
- `/integrate-pr` must not push on its own account; knowledge capture belongs to `/address-pr`
- Chat-style activity cockpit
- Confidence scoring is advisory unless gated
- Step pipelines are config-overridable, with two read-only consumers still assuming built-ins
- A new `ComputedPRState` fans out across independent maps; missing one fails open to the wrong label
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
- A cross-vendor credential leak needs an allowlist, not a denylist, of routes that may receive the resolved key
- A headless server has no display to open a browser on, and the UI must detect that before offering a browser-based reconnect action
- Codex skills are mechanically rendered from `sova/commands/data/commands/*.md`, not hand-authored, and every command-derived `.agents/skills/` entry is name-prefixed rather than directory-separated; a standalone, distributed skill from `sova/commands/data/skills/` installs under its own bare name there instead
- A tool-using step must never silently degrade to a text-only LLM call
- An unassigned ticket's suitability lives in a local record, never a label its owner's account cannot speak for
- A dependency applied once at router level, self-exempting safe methods, covers a mutating route by construction; a per-route opt-in is exactly how most of them went unguarded
- A status broadcast producer that only selects non-terminal rows can never carry a terminal transition for the client to act on
- A generated PR title's scope must come from the same source as a commit message's scope, or only one of them stays valid when the format rule changes: an issue number is not a scope
- The three suitability labels are mutually exclusive, so every producer applies them through one helper, and a config pattern match is never re-litigated by the model
- One shared suitability classifier, and the two ways routing a decision through it can weaken it
- A caller-side local-assessment fallback must honour the same scoping rule everywhere it is read
- Readiness re-checking must cover entries already in the queue, not just new candidates, and the local-assessment lookup must be batched and capacity-gated like the graph build
- A node's bare `suitability.mode` is not "is this issue schedulable," because `assess_suitability()` never looks at task state or the local-assessment record
- The dependency-gate's open-dependency walk is now the same function the classifier calls, not a parallel reimplementation
- A typo'd log level must never crash the caller, and an epic's block stays deliberately unscoped by pipeline candidate
- A git-diff-level guard must run in the PR's own worktree on the PR's own branch ref, diff the base from the pre-run fork point, and confirm blob content before flagging a revert; a dependency pin diff line with no name capture must be paired by its enclosing lockfile key, not index, name, or raw adjacency; a deleted file is not a confirmed revert; and a downgrade check must diff what this run introduced, not the PR's whole history
- A full check's reuse marker must exclude itself from the worktree-state hash it is keyed to, or writing it invalidates it
- An issue type label is not a size signal, and a triage- or spec-written complexity line outranks the heuristic vote that reads it
- A safety-critical command runs only from an installed copy verified current, because a fix to its recipe reaches a project only through a sync
- A fix loop gets one cheap try and its escalation reason is step-scoped, never the run-wide one
- One shared priority registry replaces per-tracker and per-consumer rank tables, and a default label must never be sent to a call that validates labels atomically
- A PR's closing keyword is derived from acceptance-criteria verdicts, not emitted unconditionally, and a body refresh after address-review must target only the sections it generated
- A non-current spec is degraded with a caveat, not dropped, and the stamp writer and the freshness checker must agree on exactly one frontmatter span
- Every untrusted issue-body block interpolated into a review prompt is quoted through one shared nonce-delimiting helper, never some of them
- A project's own commit convention is config, not a hardcoded list, and a hook-fix must fold into the branch commit it belongs to rather than appending a new one

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
