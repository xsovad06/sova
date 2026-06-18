# Error Handling Guidelines

Conventions for exceptions, logging, retries, fallbacks, and circuit breakers in {{ project_name }}.

## Exception Hierarchy Awareness

Never catch both parent and child in the same except tuple:

```python
# Wrong -- JSONDecodeError inherits from ValueError
except (json.JSONDecodeError, ValueError):

# Correct -- broad catch
except ValueError:

# Correct -- narrow catch for JSON + separate concern
except (json.JSONDecodeError, OSError):
```

Same applies to `ProcessLookupError` (child of `OSError`).

## Structured Logging

```python
from your_project.utils.logging import get_logger
log = get_logger(component="module.name")
```

- **Event names**: dot-delimited keys (`step.create_pr.failed`, `workflow.gate.failed`)
- **Always pass `exc_info=True`** in warning/error calls inside except blocks
- **Never use bare `print()`** for diagnostics -- use the logger

## Non-Fatal Side Effects

Optional operations (tracker updates, notifications, telemetry) must never block the primary workflow:

```python
try:
    await adapter.transition_state(issue_number, TaskState.IN_REVIEW)
except Exception:
    log.warning("step.tracker_update_failed", exc_info=True)
```

Apply this pattern to: state transitions, notification dispatches, assignment operations, telemetry extraction, handoff writes.

## Subprocess Error Handling

Two modes for subprocess execution:

| Function | Behavior | Use when |
|----------|----------|----------|
| `run()` | Returns result object, never raises | Failure is recoverable |
| `run_checked()` | Raises on non-zero exit | Failure is fatal |

- On timeout, kill the process, return a result with `returncode=-1`
- Always catch `ProcessLookupError` when calling `proc.kill()` -- the process may already be dead

## LLM Fallbacks

When LLM calls fail for user-facing outputs, always provide a structured fallback from available data. Never discard to a bare stub:

```python
try:
    result = await invoke(prompt, model="sonnet", timeout=120)
    return result.text
except RuntimeError:
    log.warning("body_generation_failed", fallback="structured")
    return build_fallback_body(context, commit_log, diff_stat)
```

### JSON Parsing with Fallback

LLM responses may include markdown fences around JSON:

1. Strip markdown fences
2. Try `json.loads()` on cleaned text
3. If that fails, extract first `{...}` substring and retry
4. If both fail, log and return empty/error state

## Idempotent Finalization

When multiple codepaths can finalize state, guard with a status check:

```python
if record.status in TERMINAL_STATUSES:
    return  # Don't overwrite status, but still update cost
record.status = new_status
```

## Summary

| Situation | Action |
|-----------|--------|
| Step cannot produce valid output | Return failure result with error message |
| Subprocess command fails | Return failure or raise, depending on mode |
| Tracker/notification fails | Log warning with `exc_info=True`, continue |
| LLM call fails | Fall back to structured output from available data |
| JSON parse fails | Try substring extraction, then log and return empty |
| DB migration fails | Fall back to `create_all`, log warning |
