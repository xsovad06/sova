# Error Handling Guidelines

Conventions for exceptions, logging, retries, fallbacks, and circuit breakers in SOVA.

## Exception Hierarchy

SOVA defines two custom exception groups. Prefer standard library exceptions elsewhere.

| Exception | Module | Purpose |
|-----------|--------|---------|
| `LLMError` and subclasses | `sova/llm/errors.py` | Typed LLM invocation failures (see below) |
| `AdapterError` | `sova/adapters/base.py` | Tracker API failure (network, rate limit, auth, unparseable list response) |
| `RuntimeError` | stdlib | Step failures, subprocess errors, suspicious file detection |
| `ValueError` | stdlib | Config/parsing errors; parent of `json.JSONDecodeError` |
| `OSError` | stdlib | File I/O errors; parent of `ProcessLookupError` |

**`AdapterError` subclasses `Exception` directly**, not `RuntimeError`. A handler narrowed to
`except (RuntimeError, OSError)` around an adapter call (`list_tasks`, `get_task`, `post_pr_review`, ...)
therefore lets every tracker API failure escape, which is exactly the case those fail-open handlers exist
to absorb. Adapter getters also parse `gh` output, so `json.JSONDecodeError` (a `ValueError`) is a second
escape route. Keep `except Exception` with a justified `# noqa: BLE001` on any fail-open path that calls
the adapter.

### LLM Error Hierarchy

`sova/llm/errors.py` is a leaf module (imports nothing from `sova`) defining `LLMError(RuntimeError)` and seven subclasses, so existing `except RuntimeError` catches keep working: `ConfigurationError`, `BillingError`, `ModelUnavailableError`, `RateLimitError`, `ProviderUnavailableError`, `LLMTimeoutError`, `LLMInvocationError` (fallback for anything unmatched). All three provider implementations (`providers/claude_code.py`, `litellm_provider.py`, `providers/anthropic_api.py`) raise these directly instead of a bare `RuntimeError`, so the failure's category is decided at the provider boundary. `ConfigurationError` is distinct from `BillingError`: it marks a static, operator-fixable configuration fact (e.g. `CodexRuntime.spawn()` refusing an unenforceable budget cap) that no retry or fallback-model hop can change, versus a transient billing/rate-limit condition that fallback or a later attempt might clear. `sova/core/workflow.py`'s `is_configuration_failure()` check runs before the retry/fallback logic and resolves straight to a non-retryable `"failed"` outcome, so a configuration fault is reported once rather than burning `step.max_retries` or walking `agent.fallback_models` re-raising itself per candidate.

- `classify_error(detail: str)` matches a failure detail string via case-insensitive substring patterns, scanned terminal-first (configuration, then billing, then model availability, rate limit, provider availability, timeout) so a detail naming both an exhausted budget and a 429 classifies as billing, and a detail naming an unenforceable budget cap classifies as configuration rather than billing.
- `classify_exception(exc)` classifies a raised SDK exception: an already-typed `LLMError` keeps its class, then the exception's class name is matched by walking the MRO (so `anthropic`/`litellm` SDK subclasses resolve without importing those optional packages), then an HTTP `status_code` attribute, then falls back to `classify_error(str(exc))`.
- `is_fallback_eligible()` and `is_billing_failure()` derive from the same category tables; `is_billing_failure()` accepts either a detail string or an exception instance. `sova/core/workflow.py`'s `_is_billing_failure` is a delegation alias to `is_billing_failure`.

**SonarCloud S5713 rule**: never catch both parent and child in the same except tuple:

```python
# Wrong: redundant
except (json.JSONDecodeError, ValueError):

# Correct: broad catch
except ValueError:

# Correct: narrow catch for JSON + separate concern
except (json.JSONDecodeError, OSError):
```

## Structured Logging

All logging uses structlog (`sova/utils/logging.py`).

```python
from sova.utils.logging import get_logger

log = get_logger(component="module.name")
```

**Enforced by `invariants/structured-logging.sh`**: a new `logging.getLogger(...)` under `sova/` fails the pre-push hook. A stdlib logger accepts `exc_info=True` but raises `TypeError` on context keywords such as `branch=name`, and only when that line runs. End the line with `# stdlib-logging: <reason>` when a stdlib logger is genuinely needed (for example to quiet a third-party library).

- **Event names**: dot-delimited keys (`step.create_pr.assign_failed`, `workflow.gate.failed`)
- **Always pass `exc_info=True`** in warning/error calls inside except blocks, except where the frames hold secrets: code that reads or writes `.claude/settings.json` (`sova/utils/permissions.py`, `sova/utils/rtk.py`) or `.mcp.json` (`sova/utils/mcp_config.py`) logs `error=str(exc)` instead, because those files can carry an `env` block with credentials, a renderer that shows frame locals would print them, and a `UnicodeDecodeError` holds the whole undecodable file in `.object`. A recurring route that fails the same way on every request (`dashboard/routers/dependency_health.py`'s unreadable manifest, the handoff reader) also drops the traceback and warns once per file version
- **Never use bare `print()`** for diagnostics: use the logger

## Non-Fatal Side Effects

Optional operations (tracker updates, notifications, memory extraction) must never block the primary workflow. Wrap in try/except, log with `exc_info=True`, and continue.

```python
try:
    await ctx.adapter.transition_state(ctx.issue_number, TaskState.IN_REVIEW)
except Exception:
    log.warning("step.create_pr.tracker_update_failed", exc_info=True)
```

**Enforced by CI**: `tests/test_exception_handlers.py` walks `sova/` with `ast` and fails if a broad
handler (`except Exception`/`except BaseException`/bare `except`) neither logs (with `exc_info=True`
on warning/error/critical calls), reports, nor re-raises. A broad catch that is intentionally silent
must carry `# noqa: BLE001 (<reason>)` on the `except` line.

This pattern appears in 100+ locations including:
- Tracker state transitions (`create_pr.py`, `_handoff_helpers.py`)
- Notification wrappers (`_safe_desktop`, `_safe_slack` in `sova/ipc/notifications.py`)
- PR assignment and label operations
- Handoff file/DB writes

## Failure Must Not Look Like Success

A helper that swallows an error must never return a value inside its own success range. `0`, `[]`, `False` or `None`-as-"not found" returned on failure is indistinguishable from a real answer, so the caller acts on "nothing to do" when the truth is "could not tell". Seen in at least seven separate PRs (counts, label lookups, verdict caches, PR-state checks).

- Return a sentinel or a multi-state result (`CHANGED`/`UNCHANGED`/`ERROR`, or `None` for unknown next to a real `0`) and let each caller choose its own fail-open or fail-closed policy.
- Never cache a failure as a negative result: the next call must retry.
- When several independent checks feed one negative ("no PR", "no conflict"), require every check to succeed before trusting it (`and`, not `or`).
- A function documented to fail open must guard every call that can raise, not just the one nearest the return: `asyncio.create_subprocess_exec` raises `FileNotFoundError` (an `OSError`) before any `ShellResult` exists, so a credential lookup placed above the `try` breaks the contract.

## Reading Text Files

Moved from `AGENTS.md` (issue #1338).

- **Reading text files: always specify `encoding="utf-8"` and catch `(OSError, UnicodeDecodeError)`**: bare `path.read_text()` uses the platform default encoding, and `UnicodeDecodeError` is not a subclass of `OSError`, so an `except OSError` guard alone still crashes on a non-UTF-8 file. This applies to any "read a file, return `None` on failure" helper, and equally to any read inside a loop over many files where one bad file must not abort the whole batch. Confirmed three times: `sova/core/steps/spec.py` (PR #680), `sova/dashboard/services/settings_service.py` (PR #1070), and `sova/commands/distribution.py:_reverse_diff_files()` (issue #695, a per-file loop where the guard `continue`s past the unreadable file instead of returning `None`). By the third confirmation the pattern was extracted into a single shared helper, `read_text_or_none()` in `sova/utils/files.py`, used by `sova/commands/distribution.py` (`_diff_files`, `_reverse_diff_files`, `_update_files`) and `sova/dashboard/services/settings_service.py`, rather than adding a fourth and fifth hand-rolled copy in the same PR that documented the convention (issue #695 review).

## Dashboard Route Failures

One catch-all handler in `create_app()` (`_unhandled_exception_handler`, `sova/dashboard/app.py`) logs any exception a route did not handle (`dashboard.unhandled_exception`, with `exc_info=True`, the method and the path; the handler is `async` so the traceback is captured, a sync handler runs in a threadpool where it is empty) and answers with the fixed body `{"detail": "Internal error"}` and status 500. The more specific handlers (`HTTPException`, request validation, `RateLimitExceeded`, `RegistryUnreadableError`, `ImportError`) keep running first, so the catch-all only sees genuinely unexpected failures. Exception text never reaches a response.

- **Do not write a catch, log and raise-500 block in a route.** A route that does only that adds nothing; let the exception propagate. A `try`/`except` stays only when it degrades one sub-step to partial data (carries a justified `# noqa: BLE001 (reason)`) or translates a specific exception into a deliberate 4xx or 502.
- **`{"detail": ...}` is the only error shape.** Never return an error inside a 200 body (`{"ok": false, "error": ...}`, `{"error": ...}`). A failed call into something the dashboard depends on has a fixed detail that names the component, and the real exception goes to the log. A request that is itself wrong is a 4xx. **502 versus 503 is one rule**: 502 when the route was asked to *act on* an external system and that call failed or was rejected (a tracker write, a `gh` command, a provider probe or component build, a label creation); 503 when the route *serves data* from a source it could not read or write, so a retry may succeed (task source and fleet scan reads, provider enumeration, `/auth/status`, `/connections`, the project config or database, the hidden-issues write). A component reporting itself unavailable is a normal 200 answer (`ok: false` from a connection test), only a failure to run the check is an error, by category: a component the config cannot build (`ComponentMisconfiguredError`, a `ValueError` subclass the provider and runtime factories raise, whose `user_message` is author-written) is a 400 carrying that message so the operator sees what to fix, any other build failure is a 502 ("... check failed"), and a probe that raises (as opposed to one that returns "unavailable", the 200 above) is a 502 ("... unreachable").
- **An unloadable project config has one mapping.** `sova/dashboard/config_errors.py` (`load_config_or_http()`) answers it on every route that loads the config: a config failing validation (a Pydantic `ValidationError`, checked first because it subclasses `ValueError`, or an error caused by one) is a 422 naming the first fields, a missing or unparsable `sova.toml` (`FileNotFoundError`, `ValueError`, `KeyError`) is a 400 ("Failed to load project config"), an `OSError` or `SQLAlchemyError` is a 503 ("Failed to load configuration"). Every translated branch is logged with its traceback (`exc_info=True`) before it is translated. Only those expected failures are caught: an `AttributeError` or `TypeError` from a bug in the loader reaches the catch-all handler and is a 500, never a transient 503. Do not hand-roll another mapping in a route.
- **Keep a 503 for a data source that is unavailable, translated at the route.** A route whose data comes from something the dashboard cannot control (the task source, fleet scan, provider enumeration, `/auth/status`, `/connections`, the hidden-issues write) maps the failure to a 503 with a fixed detail, so a client can tell an outage from a server bug; the catch-all's 500 is only for the unexpected. Catch the expected failures by type (for example `(AdapterError, RuntimeError, OSError)`); a broad catch carries a `# noqa: BLE001 (reason)` naming why it is broad.
- **A service raises a domain error, never `HTTPException`, and a client-facing message is an attribute, never `str(exc)`.** `EditorUnavailableError` (`editor_service`) means no editor can be launched (no opener for this OS, or the opener is not installed) and is mapped to a 400 by one handler in `create_app()` that builds the body from its `user_message` (the reason and the file to edit by hand). Any other `OSError` from the spawn (permission, resources) is the host's fault: it is logged and propagates to the catch-all as a 500. `ComponentMisconfiguredError.user_message` and `PriorityUnavailableError.user_message` are the same kind of attribute; the routes return those. `tests/test_router_error_handling.py::TestNoExceptionTextInErrorBodies` walks every module under `sova/dashboard/` and fails on a `detail` built from `str()`, `repr()` or an exception variable, with no exemption marker. `github_labels_service` returns fixed-message dicts (`gh` stderr is only logged), and `ModelOverrideError` / `QueuePersistError` / `PlanSetupError` are mapped by their routers.
- **A partial success keeps its results in a structured detail.** `POST /agents/planner/create-issues` answers 200 when every issue was created, and 502 with `detail: {message, results}` when any failed (`results` holds the created numbers and a per-item `ok` flag, no exception text). JS callers read `detail` as a string, a dict (`message`, optionally `results`) or a list (FastAPI validation errors); `getErrorDetail()` in `app.js` already handles all three (an object detail with no `message` shows the generic text, never `[object Object]`). That 502 may carry side effects: some issues were already created, so `results` lists every task in request order, the created ones with their `number` and `url`, and a client must reconcile from it instead of re-submitting the batch (the planner card drops the created tasks from its cached proposals by position, since `results[i]` answers the i-th submitted task; a page reload refetches the unchanged handoff, so the server does not yet remember what was created: a reload or a second tab can still resubmit the batch and open duplicates, a known limitation that needs the created issue numbers recorded on the handoff). A result's `title` is always the title the task was submitted with. `POST /supervisor/plan/approve` follows the same shape (502 with `detail: {message, results}` when any decision failed; the plan is already consumed). `execute_plan_decisions()` normalizes every result to `{ok, issue_number, ...}`: a failure the engine reports (an `error` key or `status: "failed"`) or raises is reduced to the fixed error "Decision failed" and the engine's own text, which is not all author-written (an auto-rebase failure carries exception text), is only logged. The router tests `ok is False`, the supervisor page names each failed issue and reloads the plan and decisions whatever the outcome. If the engine itself cannot be built (config, adapter or database), `PlanSetupError` makes the route answer 503 and put the plan back whole (decisions, reasoning and deferred list), since nothing ran. `POST /settings/labels/create` is the same shape for label creation (`detail: {message, created, errors, errors_truncated}`).
- **Testing a 500.** Starlette's `ServerErrorMiddleware` sends the handler's response and then re-raises, so a test client must be built with `ASGITransport(app=app, raise_app_exceptions=False)` to see the 500. Use that flag only in a test that asserts a 500, so an unexpected exception elsewhere keeps its traceback. The handler's response sits outside the user middlewares, so it carries no security headers (`X-Content-Type-Options: nosniff` included) and no CORS headers. That is acceptable because the dashboard JavaScript is same-origin with the API (it is served by the same app, so no CORS header is needed to read the 500 body, which is JSON with the fixed `{"detail": "Internal error"}`), and the CSRF guard only gates state-changing requests before the handler runs. A dev server on another origin would not see the 500 body. WebSocket routes are not covered by the HTTP handler.

## Typer Exit and Dashboard Endpoints

`typer.Exit` inherits from `SystemExit` (a `BaseException`), **not** `Exception`. Any dashboard endpoint that calls a CLI function raising `typer.Exit` must catch `SystemExit` explicitly: `except Exception` will not intercept it, causing an unhandled exception that only the catch-all handler (see Dashboard Route Failures) turns into a 500.

```python
# Wrong: typer.Exit escapes this, and str(exc) leaks internal text to the client
except Exception as exc:
    raise HTTPException(status_code=500, detail=str(exc))

# Correct: translate SystemExit to its deliberate status; let anything else reach the catch-all handler
except SystemExit:
    raise HTTPException(status_code=404, detail="Project directory not found")
```

Also: never leak raw `str(exc)` in HTTP responses (OWASP information disclosure). Use a fixed message and log the details server-side with `exc_info=True`.

When a deliberate translation re-raises, chain it (`from exc`, or `from None` when the original is already logged, to suppress the "During handling of the above exception..." traceback). Ruff B904 enforces explicit chaining.

## Per-Item Error Handling in Deletion Loops

When iterating over items to delete (files, directories, registry entries), wrap try/except **inside** the loop so one failure doesn't skip the remaining items.

```python
# Wrong: first failure skips everything after it
try:
    for name in items:
        (path / name).unlink()
except OSError as exc:
    failed.append(f"cleanup: {exc}")

# Correct: each item attempted independently
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

- Timeout uses `asyncio.timeout()`; on expiry, sends SIGTERM to the child's whole process group (`run()` starts each child with `start_new_session=True`), waits up to `_TERM_GRACE_SECONDS` for the leader to exit, then SIGKILLs the group, so `make`, `pytest` and its xdist workers die with the `sh` that started them; returns `returncode=-1`. A cancelled `run()` SIGKILLs the group at once. The child has no controlling terminal, so a `/dev/tty` prompt (an SSH passphrase) fails instead of asking
- `subprocess_error()` factory truncates stderr to 500 chars
- Always catch `ProcessLookupError` when calling `proc.kill()`: the process may already be dead; for `os.killpg` also catch `PermissionError` (macOS, zombie leader) and log it at debug

## Step-Level Retry

`WorkflowEngine._execute_with_retries()` handles bounded retries:

1. Attempts = `step.max_retries + 1` (default `max_retries=0`, one attempt)
2. Catches broad `Exception` during `step.execute()`, converts to `StepResult(success=False)`
3. On success, runs `step.validate_output()` (gate check): gate failure is NOT retried
4. Records attempt count and duration in `StepExecution` DB records

**Do NOT implement retry logic inside step `execute()` methods**: declare `max_retries` on the class.

## Gate Check Validation

Every step's `validate_output()` must verify all forms of change:

- Unstaged diff: `git diff --stat HEAD`
- Staged diff: `git diff --cached --stat`
- Commits ahead of base: `git log {base}..HEAD --oneline`

LLM agents may commit directly, leaving working-tree diffs empty. **Never return `GateCheckResult(passed=True)` unconditionally.**

## Circuit Breakers

### Address-Review Budget

The cap lives in `resolve_next_action()` (`sova/dashboard/services/work_state.py`), the one resolver both the dashboard and the supervisor read, so neither can propose an extra cycle:

1. `resolve_sova_verdict()` (`sova/dashboard/services/work_verdict.py`) derives `address_cycles` from `parse_review_history()`'s count of `sova-addressed` GitHub review markers and carries it on the verdict
2. At `pipeline.max_address_review_cycles` (default 3, 0=unlimited) the resolver's `review_budget_exhausted` rule returns `PR_REVIEW_EXHAUSTED` with an Integrate action, ahead of every verdict- and thread-based rule. An unreadable review history (the count is unknown, not exhausted) is the separate `review_history_unresolved` rule just before it: `PR_AWAITING_REVIEW` with no action, a hold for the user, never Integrate (the supervisor auto-integrates on that action)
3. `_check_address_review_circuit_breaker()` (`sova/dashboard/services/agent_handoff.py`) still runs at agent exit to stop the handoff chain auto-spawning one more cycle before the next poll, and invalidates the PR's verdict cache so the new count is visible immediately

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

PR body generation uses a deterministic template (`_build_pr_body()`): no LLM call. For steps that still use conditional LLM calls (validate, monitor_ci, rebase), the pattern is:

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
| Tracker/notification/assignment fails | Log warning with `exc_info=True`, continue |
| LLM call fails | Fall back to structured output from available data |
| JSON parse fails | Try substring extraction, then log and return empty |
| DB migration fails | Fall back to `create_all`, log warning |
| Gate check fails | Return `GateCheckResult(passed=False, reason=...)` |
