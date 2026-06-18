# Error Handling Guidelines

Conventions for exceptions, logging, retries, fallbacks, and circuit breakers in SOVA.

## Exception Hierarchy

SOVA defines one custom exception. Prefer standard library exceptions elsewhere.

| Exception | Module | Purpose |
|-----------|--------|---------|
| `InvalidTransitionError` | `sova/core/state.py` | Invalid state machine transition (`current` and `target` fields) |
| `RuntimeError` | stdlib | Step failures, subprocess errors, suspicious file detection |
| `ValueError` | stdlib | Config/parsing errors; parent of `json.JSONDecodeError` |
| `OSError` | stdlib | File I/O errors; parent of `ProcessLookupError` |

**SonarCloud S5713 rule**: never catch both parent and child in the same except tuple:

```python
# Wrong -- redundant
except (json.JSONDecodeError, ValueError):

# Correct -- broad catch
except ValueError:

# Correct -- narrow catch for JSON + separate concern
except (json.JSONDecodeError, OSError):
```

## Structured Logging

All logging uses structlog (`sova/utils/logging.py`).

```python
from sova.utils.logging import get_logger
log = get_logger(component="module.name")
```

- **Event names**: dot-delimited keys (`step.create_pr.assign_failed`, `workflow.gate.failed`)
- **Always pass `exc_info=True`** in warning/error calls inside except blocks
- **Never use bare `print()`** for diagnostics -- use the logger

## Non-Fatal Side Effects

Optional operations (tracker updates, notifications, memory extraction) must never block the primary workflow. Wrap in try/except, log with `exc_info=True`, and continue.

```python
try:
    await ctx.adapter.transition_state(ctx.issue_number, TaskState.IN_REVIEW)
except Exception:
    log.warning("step.create_pr.tracker_update_failed", exc_info=True)
```

This pattern appears in 100+ locations including:
- Tracker state transitions (`create_pr.py`, `_handoff_helpers.py`)
- Notification wrappers (`_safe_desktop`, `_safe_slack` in `sova/ipc/notifications.py`)
- PR assignment and label operations
- Memory extraction (`extract_memory.py`)
- Handoff file/DB writes

## Subprocess Error Handling

Two modes in `sova/utils/shell.py`:

| Function | Behavior | Use when |
|----------|----------|----------|
| `run()` | Returns `ShellResult`, never raises | Failure is recoverable |
| `run_checked()` | Raises `RuntimeError` on non-zero exit | Failure is fatal |

- Timeout uses `asyncio.timeout()`; on expiry, kills process, returns `returncode=-1`
- `subprocess_error()` factory truncates stderr to 500 chars
- Always catch `ProcessLookupError` when calling `proc.kill()` -- the process may already be dead

## Step-Level Retry

`WorkflowEngine._execute_with_retries()` handles bounded retries:

1. Attempts = `step.max_retries + 1` (default `max_retries=0`, one attempt)
2. Catches broad `Exception` during `step.execute()`, converts to `StepResult(success=False)`
3. On success, runs `step.validate_output()` (gate check) -- gate failure is NOT retried
4. Records attempt count and duration in `StepExecution` DB records

**Do NOT implement retry logic inside step `execute()` methods** -- declare `max_retries` on the class.

## Gate Check Validation

Every step's `validate_output()` must verify all forms of change:

- Unstaged diff: `git diff --stat HEAD`
- Staged diff: `git diff --cached --stat`
- Commits ahead of base: `git log {base}..HEAD --oneline`

LLM agents may commit directly, leaving working-tree diffs empty. **Never return `GateCheckResult(passed=True)` unconditionally.**

## Circuit Breakers

### Address-Review Circuit Breaker

`_check_address_review_circuit_breaker()` in `sova/dashboard/services/agent_handoff.py` prevents infinite bot re-review loops:

1. Counts completed address-review runs by issue+PR (`_count_address_review_runs()`)
2. Blocks auto-execution at `pipeline.max_address_review_cycles` (default 2, 0=unlimited)
3. Dashboard shows manual action buttons instead of auto-spawning

### Budget Check

Checked at step boundaries in `WorkflowEngine`. When exceeded, sets `TaskStatus.PAUSED` and records a `FailureRecord`.

## Terminal Status Handling

Two terminal status sets in `sova/core/state.py`:

```python
_TERMINAL = frozenset({TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.REJECTED})
TASK_RUN_TERMINAL = frozenset({"done", "failed", "rejected", "interrupted"})
```

Use `TASK_RUN_TERMINAL` for DB queries, `_TERMINAL` for state machine checks. "interrupted" is set by `recover_stale_runs()` for dead-PID cleanup.

### Idempotent Finalization

Multiple codepaths can finalize a TaskRun. Always guard:

```python
if task_run.status in TASK_RUN_TERMINAL:
    return  # Don't overwrite status -- but still update cost
```

Status updates are conditional (once terminal, never overwrite); cost updates are unconditional (stream cost is authoritative).

## LLM Fallbacks

When LLM calls fail for user-facing outputs, always provide a structured fallback from available data. **Never discard to a bare stub.**

```python
try:
    result = await invoke(prompt, model="sonnet", cwd=ctx.working_dir, timeout=120)
    return result.text
except RuntimeError:
    log.warning("step.create_pr.body_generation_failed", fallback="structured")
    return self._build_fallback_body(ctx, task_title, commit_log, diff_stat)
```

### JSON Parsing with Substring Extraction

LLM responses may include markdown fences around JSON. Pattern from `sova/roles/reviewer.py`:

1. Strip markdown fences (```` ``` ````)
2. Try `json.loads()` on cleaned text
3. If that fails, extract first `{...}` substring and retry
4. If both fail, log and return empty/error state

### Database Migration Fallback

`sova/db/session.py:_run_migrations()` uses a three-tier approach:
1. Alembic upgrade/stamp (normal path)
2. `create_all` + stamp (if Alembic fails)
3. Tables created but untracked (if stamp also fails)

Always `DROP TABLE IF EXISTS alembic_version` before the fallback stamp to avoid corrupted state.

## Summary

| Situation | Action |
|-----------|--------|
| Step cannot produce valid output | Return `StepResult(success=False, error=str(exc))` |
| Subprocess command fails | `run()` returns failure; `run_checked()` raises `RuntimeError` |
| Invalid state transition | Raise `InvalidTransitionError` |
| Tracker/notification/assignment fails | Log warning with `exc_info=True`, continue |
| LLM call fails | Fall back to structured output from available data |
| JSON parse fails | Try substring extraction, then log and return empty |
| DB migration fails | Fall back to `create_all`, log warning |
| Gate check fails | Return `GateCheckResult(passed=False, reason=...)` |
