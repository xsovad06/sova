# Performance Guidelines

Timeout values, cache patterns, concurrency limits, and async conventions in the SOVA codebase.

## Timeout Hierarchy

All configurable timeouts cascade from `sova.toml` or env vars (`SOVA_` prefix).

| Layer | Default | Config Key | File |
|-------|---------|------------|------|
| Shell commands | 300s | hardcoded | `sova/utils/shell.py` |
| LLM invoke | 600s | hardcoded | `sova/llm/client.py` |
| Pipeline steps (LLM) | 1800s | `agent.step_timeout` | `sova/config/models.py` |
| Pipeline steps (git/API) | 15-120s | hardcoded per step | `create_pr.py` |
| Develop step | 1200s | `develop.step_timeout` | `sova/config/models.py` |
| Validate hook execution | 120s | `validation.hook_timeout` | `sova/config/models.py` |
| Validate LLM fix | 180s | `validation.fix_timeout` | `sova/config/models.py` |
| CI polling max wait | 900s | `ci.max_wait` | `sova/config/models.py` |
| CI poll interval | 60s | `ci.poll_interval` | `sova/config/models.py` |
| CI no-checks grace | 120s | `ci.no_checks_grace_period` | `sova/config/models.py` |
| External review poll | 30s | `external_reviews.poll_interval` | `sova/config/models.py` |
| External review timeout | 15s | `external_reviews.timeout` | `sova/config/models.py` |
| Agent graceful stop | 10s | hardcoded | `sova/ipc/control.py` |
| Watch veto window | 30s | `watch.veto_seconds` | `sova/config/models.py` |

### Complexity multiplier

All pipeline step timeouts are multiplied by a complexity factor based on the issue's complexity tier:

| Complexity Tier | Multiplier | Applied To |
|----------------|------------|------------|
| TRIVIAL | 1.0x | Base timeout unchanged |
| SIMPLE | 1.0x | Base timeout unchanged |
| COMPLEX | 1.5x | All step timeouts |
| EPIC | 2.0x | All step timeouts |

The multiplier is capped at 3.0x for future extensibility. Applied in `WorkflowEngine._step_timeout()` to all steps (develop, monitor_ci, validate, etc.).

### Partial work preservation

When a step times out, `WorkflowEngine._preserve_partial_work_on_timeout()` commits any staged changes with message `"wip: partial work from {step_name} (timeout)"`. Only tracked files modified during the step are preserved (via `git add -u`); new untracked files are not committed. The `StepResult.partial_work` flag is set to `True` so the dashboard can surface this to the user.

### Timeout conventions

- Use `asyncio.timeout()` (Python 3.11+) for new code, not `asyncio.wait_for()`
- Always kill the subprocess on timeout: call `proc.kill()` then `await proc.wait()`
- Agent stop escalates SIGTERM to SIGKILL after timeout (`sova/ipc/control.py`)
- Steps that invoke Claude CLI pass `timeout=ctx.config.agent.step_timeout` (before multiplier)
- Steps with their own config keys (develop, validation, CI) use those specific timeouts
- All timeouts are subject to the complexity multiplier when `ctx.complexity` is set

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
| Max CI fix attempts | 3 | `ci.max_fix_attempts` (0=disable) |
| Max address-review cycles | 2 | `pipeline.max_address_review_cycles` (0=unlimited) |
