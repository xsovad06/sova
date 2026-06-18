# Performance Guidelines

Timeout patterns, caching strategies, concurrency control, and async conventions for {{ project_name }}.

## Timeout Conventions

- Use `asyncio.timeout()` (Python 3.11+) for new code, not `asyncio.wait_for()`
- Always kill the subprocess on timeout -- call `proc.kill()` then `await proc.wait()`
- Define a timeout hierarchy: shell commands < API calls < LLM invocations < full pipeline steps
- Make timeouts configurable where possible (config file or env vars)

## Caching Strategies

### Mtime-based file caches

For services that read project files, check `stat().st_mtime` before re-parsing:

```python
if path.stat().st_mtime <= cached_mtime:
    return cached_result
```

Tests must monkeypatch the project directory and clear caches between test cases.

### TTL caches

Use `time.monotonic()` (not `time.time()`) for TTL checks -- immune to clock skew:

```python
if time.monotonic() - cached_at > TTL_SECONDS:
    cached_result = None
```

### LRU caches

Use `@lru_cache(maxsize=1)` for static discovery results that never change at runtime.

## Concurrency Control

### Semaphore-based slot limits

```python
async with semaphore:
    await spawn_work()
```

Per-item dedup runs independently of slot checks -- semaphore alone does not prevent duplicate work on the same item.

### Background task lifecycle

All `asyncio.create_task()` calls must be tracked to prevent GC collection:

```python
# Pattern 1: Named attribute (long-lived loops)
self._watch_task = asyncio.create_task(self._run_loop())

# Pattern 2: Set with discard callback (fire-and-forget)
_background_tasks: set[asyncio.Task] = set()
task = asyncio.create_task(coro)
_background_tasks.add(task)
task.add_done_callback(_background_tasks.discard)
```

Always pass `return_exceptions=True` to `asyncio.gather()` during cancellation.

## Bounded Buffers

Use `collections.deque(maxlen=N)` for streaming data to cap memory usage:

```python
output_lines: deque[str] = deque(maxlen=5000)
```

## Blocking I/O in Async Context

File reads in async endpoint handlers must be offloaded:

```python
result = await asyncio.to_thread(parse_file, file_path)
```

Small JSON reads (config, handoff files) are fast enough to run inline.

## Subprocess Streaming

For streaming LLM output, read stdout line-by-line without buffering. No active timeout on streaming reads -- process lifetime and budget checks govern total duration.

## Database Session Management

- `expire_on_commit=False` -- objects remain accessible after commit without re-query
- SQLite: `check_same_thread=False` for async multi-threaded access
- Always use context manager: `async with await get_session() as session:`
