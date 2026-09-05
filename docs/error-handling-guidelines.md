# Error Handling Guidelines

Conventions for exceptions, logging, retries, fallbacks, and circuit breakers in SOVA.

## Exception Hierarchy

SOVA defines two custom exception groups. Prefer standard library exceptions elsewhere.

| Exception | Module | Purpose |
|-----------|--------|---------|
| `InvalidTransitionError` | `sova/core/state.py` | Invalid state machine transition (`current` and `target` fields) |
| `LLMError` and subclasses | `sova/llm/errors.py` | Typed LLM invocation failures (see below) |
| `RuntimeError` | stdlib | Step failures, subprocess errors, suspicious file detection |
| `ValueError` | stdlib | Config/parsing errors; parent of `json.JSONDecodeError` |
| `OSError` | stdlib | File I/O errors; parent of `ProcessLookupError` |

### LLM Error Hierarchy

`sova/llm/errors.py` is a leaf module (imports nothing from `sova`) defining `LLMError(RuntimeError)` and six subclasses, so existing `except RuntimeError` catches keep working: `BillingError`, `ModelUnavailableError`, `RateLimitError`, `ProviderUnavailableError`, `LLMTimeoutError`, `LLMInvocationError` (fallback for anything unmatched). All three provider implementations (`providers/claude_code.py`, `litellm_provider.py`, `providers/anthropic_api.py`) raise these directly instead of a bare `RuntimeError`, so the failure's category is decided at the provider boundary.

- `classify_error(detail: str)` matches a failure detail string via case-insensitive substring patterns, scanned terminal-first (billing, then model availability, rate limit, provider availability, timeout) so a detail naming both an exhausted budget and a 429 classifies as billing.
- `classify_exception(exc)` classifies a raised SDK exception: an already-typed `LLMError` keeps its class, then the exception's class name is matched by walking the MRO (so `anthropic`/`litellm` SDK subclasses resolve without importing those optional packages), then an HTTP `status_code` attribute, then falls back to `classify_error(str(exc))`.
- `is_fallback_eligible()` and `is_billing_failure()` derive from the same category tables; `is_billing_failure()` accepts either a detail string or an exception instance. `sova/core/workflow.py`'s `_is_billing_failure` is a delegation alias to `is_billing_failure`.

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

## Typer Exit and Dashboard Endpoints

`typer.Exit` inherits from `SystemExit` (a `BaseException`), **not** `Exception`. Any dashboard endpoint that calls a CLI function raising `typer.Exit` must catch `SystemExit` explicitly -- `except Exception` will not intercept it, causing an unhandled 500 with a raw traceback.

```python
# Wrong -- typer.Exit escapes this
except Exception as exc:
    raise HTTPException(status_code=500, detail=str(exc))

# Correct -- catch SystemExit for CLI errors, generic message for others
except SystemExit:
    raise HTTPException(status_code=404, detail="Project directory not found")
except Exception:
    log.exception("operation.failed for %s", identifier)
    raise HTTPException(status_code=500, detail="Operation failed")
```

Also: never leak raw `str(exc)` in HTTP responses (OWASP information disclosure). Use a generic message and log the details server-side with `log.exception()`.

Use `from None` on re-raised exceptions when the original is already logged, to suppress confusing "During handling of the above exception..." tracebacks. Ruff B904 enforces explicit chaining.

```python
except Exception:
    log.exception("Uninstall failed for %s", req.slug)
    raise HTTPException(status_code=500, detail="Failed to uninstall") from None
```

## Per-Item Error Handling in Deletion Loops

When iterating over items to delete (files, directories, registry entries), wrap try/except **inside** the loop so one failure doesn't skip the remaining items.

```python
# Wrong -- first failure skips everything after it
try:
    for name in items:
        (path / name).unlink()
except OSError as exc:
    failed.append(f"cleanup: {exc}")

# Correct -- each item attempted independently
for name in items:
    try:
        (path / name).unlink()
    except OSError as exc:
        failed.append(f"{name}: {exc}")
```

This pattern applies to any cleanup/teardown code that operates on multiple independent resources.

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

PR body generation uses a deterministic template (`_build_pr_body()`) -- no LLM call. For steps that still use conditional LLM calls (validate, monitor_ci, rebase), the pattern is:

```python
try:
    result = await invoke(prompt, model=model, cwd=ctx.working_dir, timeout=120)
    return result.text
except RuntimeError:
    log.warning("step.operation_failed", fallback="structured")
    return build_structured_fallback(available_data)
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
