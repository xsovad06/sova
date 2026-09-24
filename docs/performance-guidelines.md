# Performance Guidelines

Timeout values, cache patterns, concurrency limits, and async conventions in the SOVA codebase.

## Timeout Hierarchy

All configurable timeouts cascade from SOVA config or env vars (`SOVA_` prefix).

| Layer | Default | Config Key | File |
|-------|---------|------------|------|
| Shell commands | 300s | hardcoded | `sova/utils/shell.py` |
| LLM invoke | 600s | hardcoded | `sova/llm/client.py` |
| Pipeline step hard timeout (normal tier) | 1800s | `agent.step_timeout_normal` | `sova/config/models.py` |
| Pipeline step hard timeout (complex tier) | 2700s | `agent.step_timeout_complex` | `sova/config/models.py` |
| Direct LLM invoke inside a step | 1800s | `agent.step_timeout` (legacy) | `sova/config/models.py` |
| Pipeline steps (git/API) | 15-120s | hardcoded per step | `create_pr.py` |
| Develop step (normal tier) | 2400s | `develop.step_timeout_normal` | `sova/config/models.py` |
| Develop step (complex tier) | 3600s | `develop.step_timeout_complex` | `sova/config/models.py` |
| Develop check phase | 600s | `develop.check_timeout` | `sova/config/models.py` |
| Develop LLM fix | 600s | `develop.fix_timeout` | `sova/config/models.py` |
| Develop max fix time | 1800s | `develop.max_fix_time` | `sova/config/models.py` |
| Validate hook execution | 120s | `validation.hook_timeout` | `sova/config/models.py` |
| Validate LLM fix | 600s | `validation.fix_timeout` | `sova/config/models.py` |
| CI polling max wait | 900s | `ci.max_wait` | `sova/config/models.py` |
| CI poll interval | 60s | `ci.poll_interval` | `sova/config/models.py` |
| CI no-checks grace | 120s | `ci.no_checks_grace_period` | `sova/config/models.py` |
| External review poll | 30s | `external_reviews.poll_interval` | `sova/config/models.py` |
| External review timeout | 15s | `external_reviews.timeout` | `sova/config/models.py` |
| Agent graceful stop | 10s | hardcoded | `sova/ipc/control.py` |
| Watch veto window | 30s | `watch.veto_seconds` | `sova/config/models.py` |

### Per-tier step timeouts

`WorkflowEngine._step_timeout()` selects an explicit base timeout per complexity tier instead of multiplying a single base. TRIVIAL, SIMPLE, and MODERATE (and unset/unknown complexity) map to the "normal" tier; COMPLEX and EPIC map to the "complex" tier (both share one base: EPIC is rare enough that a separate tier isn't warranted today).

| Config Key | Normal default | Complex default | Applies To |
|------------|-----------------|------------------|------------|
| `develop.step_timeout_normal` / `develop.step_timeout_complex` | 2400s | 3600s | `develop` step only |
| `agent.step_timeout_normal` / `agent.step_timeout_complex` | 1800s | 2700s | every other step except `monitor_ci` |

`monitor_ci` is excluded from per-tier scaling entirely: its timeout is always `ci.max_wait + 120`, regardless of complexity tier.

The develop step's `/develop` invocation deliberately stays on the flat legacy `develop.step_timeout` (2400s), not the per-tier base above: making it tier-aware would let a COMPLEX run's LLM invocation consume the entire widened `develop.step_timeout_complex` hard timeout, leaving the inner check/fix loop no headroom to actually use the extra budget.

The legacy `develop.step_timeout` / `agent.step_timeout` fields still exist with unchanged defaults, and are independent config values from the per-tier fields: no value is inherited between them. `agent.step_timeout` is still read directly by steps that invoke the LLM outside `_step_timeout()` (`research.py`, `self_review.py`, `spec.py`, `simplify.py`, `rearrange_commits.py`, `confidence_score.py`, `address_review.py`, `sova/mcp/tools.py`); `develop.step_timeout` is read directly by `DevelopStep` for its own `/develop` invocation. A project that wants to raise the develop step's outer hard timeout must set `develop.step_timeout_normal` / `develop.step_timeout_complex` explicitly (or `SOVA_DEVELOP_STEP_TIMEOUT_NORMAL` / `SOVA_DEVELOP_STEP_TIMEOUT_COMPLEX`); the legacy field's value is not carried over automatically. The `agent.step_timeout_normal` / `agent.step_timeout_complex` pair is likewise settable via `SOVA_AGENT_STEP_TIMEOUT_NORMAL` / `SOVA_AGENT_STEP_TIMEOUT_COMPLEX`; all four env vars are wired in `sova/config/loader.py:_apply_nested_env_overrides()`, without which an env var is silently ignored whenever the same field is already present in the merged TOML/DB config (see "New config sections need quadruple registration" in `.claude/rules/architecture.md`). Only `gt=0` is enforced on the per-tier fields; there is no upper bound and no cross-field ordering check between `_normal` and `_complex`, and `develop.step_timeout_complex`'s default (3600s) intentionally exceeds `agent.step_timeout_complex`'s default (2700s) unclamped, per the "specific setting wins" rule documented on `WorkflowEngine._step_timeout`.

The complexity *multiplier* (`sova.llm.complexity.complexity_multiplier`: 1.5x for COMPLEX, 2.0x for EPIC, capped at 3.0x) still exists and is still used, but only by the wall-clock runaway guard (`_check_runaway_guard`, `_effective_step_timeout`) to scale `runaway.max_run_wall_clock_seconds`. It is no longer read by `_step_timeout()` itself.

### Partial work preservation

When a step times out, `WorkflowEngine._preserve_partial_work_on_timeout()` commits any staged changes with message `"wip: partial work from {step_name} (timeout)"`. Only tracked files modified during the step are preserved (via `git add -u`); new untracked files are not committed. The `StepResult.partial_work` flag is set to `True` so the dashboard can surface this to the user.

### Timeout conventions

- Use `asyncio.timeout()` (Python 3.11+) for new code, not `asyncio.wait_for()`
- Always kill the subprocess on timeout: call `proc.kill()` then `await proc.wait()`
- Agent stop escalates SIGTERM to SIGKILL after timeout (`sova/ipc/control.py`)
- Steps that invoke Claude CLI directly (outside `_step_timeout()`, e.g. `research.py`, `self_review.py`, `spec.py`) pass `timeout=ctx.config.agent.step_timeout` (the legacy, unscaled field); `develop.py` passes `timeout=ctx.config.develop.step_timeout`, its own legacy unscaled field, for the same reason
- Steps with their own config keys (develop, validation, CI) use those specific timeouts
- Pipeline step hard timeouts (`WorkflowEngine._step_timeout()`) select a per-tier base (see "Per-tier step timeouts" above) instead of applying the complexity multiplier; only the wall-clock runaway guard still uses the multiplier

## Database Session Management

- `expire_on_commit=False` -- objects remain accessible after commit without re-query
- SQLite: `check_same_thread=False` for async multi-threaded access
- After Alembic migrations on file-backed SQLite, `await engine.dispose()` clears stale schema cache
- Always use context manager: `async with await get_session() as session:`

For multi-project mode, `get_session(project_dir=...)` returns a session from a per-project engine stored in `_engines: dict[str, tuple]`.

## Caching Strategies

### Mtime-based file caches

Used by dashboard services that read project files. Check `stat().st_mtime` before re-parsing.

| Cache | File | Key Type | Invalidation |
|-------|------|----------|--------------|
| Handoff files | `handoff_service.py` | `{project_dir}:{issue}` | mtime comparison |
| Log files | `log_service.py` | file path | `os.path.getmtime()` |

Tests MUST monkeypatch `get_project_dir` to `tmp_path` and may need to clear `_handoff_caches` / `_log_cache` between test cases.

### TTL caches

PR synthesis and issue-PR lookups in `agent_recovery.py` use `time.monotonic()` with a 60-second TTL (`_SYNTHESIS_TTL_SECONDS = 60`, `_check_ttl_cache()`). Use `time.monotonic()` (not `time.time()`) for TTL checks -- immune to clock skew.

### LRU caches

`@lru_cache(maxsize=1)` on `get_builtin_roles()` and `get_available_commands()` in `role_service.py`. These cache static discovery results that never change at runtime.

## Concurrency Control

### Semaphore-based slot limits

| Scope | Default | Config | File |
|-------|---------|--------|------|
| Parallel agents (scheduler) | 2 | `max_parallel_agents` | `sova/scheduler/parallel.py` |
| Per-project agent slots | 3 | `max_concurrent` in ProjectAgents | `sova/dashboard/services/agent_pool.py` |
| Batch triage | 3 | per-batch `max_concurrency` | `sova/dashboard/services/batch_service.py` |
| Batch harden | 2 | per-batch `max_concurrency` | `sova/dashboard/services/batch_service.py` |

Pattern: `async with semaphore:` before spawning work. Per-issue dedup (`_check_issue_conflict()`) runs independently of slot checks.

### Background task lifecycle

All `asyncio.create_task()` calls must be tracked to prevent GC collection:

```python
# Pattern 1: Named attribute (scheduler, sweep loops)
self._watch_task = asyncio.create_task(self._run_watch_loop())

# Pattern 2: Set with discard callback (fire-and-forget)
_background_tasks: set[asyncio.Task] = set()
task = asyncio.create_task(coro)
_background_tasks.add(task)
task.add_done_callback(_background_tasks.discard)
```

Always pass `return_exceptions=True` to `asyncio.gather()` during cancellation.

## Bounded Buffers

| Buffer | Max Size | TTL | File |
|--------|----------|-----|------|
| Agent output lines | 5000 | -- | `agent_pool.py` (`deque(maxlen=5000)`) |
| Recently completed agents | 5 | 30s | `agent_pool.py` (`RECENTLY_COMPLETED_TTL`) |
| Completed batches | 50 | -- | `batch_service.py` (`_MAX_COMPLETED_BATCHES`) |

## Dashboard Polling Intervals

Frontend JS polls these endpoints on fixed intervals (in `sova/dashboard/static/app.js`):

| Poll Target | Interval | Endpoint |
|-------------|----------|----------|
| Agent activity | 3s | `/api/agents/activity` |
| Handoff state | 5s | `/api/handoff` |
| Batch progress | 2s | `/api/queue/batch/active` |

When polled state disappears, the JS handler must actively clear `innerHTML` or set `hidden`. Orphaned panels persist until page reload otherwise.

## Blocking I/O in Async Context

File reads in async endpoint handlers must be offloaded via `asyncio.to_thread()`:

```python
all_entries = await asyncio.to_thread(_parse_log_file, log_path)
```

Current offloading sites: `log_service.py` (log file parsing). Small JSON reads (`handoff_service.py`, `settings_service.py`) are fast enough to run inline.

## Subprocess Streaming

LLM output uses JSONL streaming (`--output-format stream-json`). Reads stdout line-by-line without buffering. No active timeout on streaming reads -- process lifetime and budget checks govern total duration. Dashboard agents capture output into a bounded `deque(maxlen=5000)`.

## Scheduler Polling

The watch loop (`sova/scheduler/watch.py`) uses interruptible waits:

```python
await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
```

Active interval: 300s (`watch.interval_active`), idle interval: 1800s (`watch.interval_idle`). Allows immediate shutdown via `_stop_event.set()`.

## Budget Limits

| Limit | Default | Config Key |
|-------|---------|------------|
| Per-run budget | $10.00 | `agent.max_budget` |
| Per-issue budget | $50.00 | `agent.max_issue_budget` |
| Max run wall clock | 14400s | `runaway.max_run_wall_clock_seconds` (0=disable, scaled by task complexity) |
| Max run steps | 100 | `runaway.max_run_steps` (0=disable) |
| Max LLM calls | 250 | `runaway.max_llm_calls` (0=disable) |
| Max step attempts | 80 | `runaway.max_step_attempts` (0=disable) |
| Max CI fix attempts | 3 | `ci.max_fix_attempts` (0=disable) |
| Max address-review cycles | 2 | `pipeline.max_address_review_cycles` (0=unlimited) |

**`max_budget_usd` caps per attempt, not per call, when `agent.fallback_models` is
non-empty.** `_invoke_with_fallback()` in `sova/llm/client.py` passes the caller's
full `max_budget_usd` to every candidate in the chain rather than dividing it
up front, so a fast-failing primary does not starve a healthy fallback of
budget. A failed attempt reports no cost back to SOVA, so an exhausted budget
(`BillingError`) cannot repeat: it is not fallback-eligible and re-raises
before the next candidate runs. A fallback-eligible failure (rate limit,
timeout, provider unavailable) that occurs after the CLI subprocess has
already billed partial output within that attempt's own window is not
tracked, since the exception carries no cost data. The practical worst case
for spend across one call is therefore `chain_length x max_budget_usd`, not
`max_budget_usd`, when fallback models are configured. Operators who need a
hard aggregate ceiling should size `agent.max_budget` with this in mind rather
than assuming per-candidate deduction.
