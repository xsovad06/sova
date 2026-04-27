# Review Feedback

Lessons from PR reviews, grouped by category.

## Self-Review (Phase 2, April 2026)

- **Config defaults must be headless-safe**: features that depend on platform-specific tools (e.g., desktop notifications via osascript/notify-send) should default to `False`. Servers, containers, and CI environments don't have GUI tooling. Let users opt-in explicitly.
- **Type annotations on private methods**: even private/internal methods like `_build_comment(task)` need proper type annotations. Missing annotations on parameters that have well-known types (e.g., `Task`) silently reduce type safety and IDE support.
- **Session resource leaks from wrong pattern**: DB session acquisition and cleanup must use the context manager pattern consistently. Getting a session without `async with` means it never gets properly closed, leaking connections under load.
- **`@staticmethod` for methods not using `self`**: when a method on a class doesn't reference `self`, make it a `@staticmethod`. This communicates intent (pure function), allows calling without an instance, and avoids unnecessary mock scaffolding in tests.
- **Type hints on internal helpers**: `asyncio.coroutines` is not a real type. Use `Coroutine[Any, Any, ReturnType]` from `collections.abc` for coroutine parameters. Ruff won't catch invalid type references that happen to exist as module attributes.

## Self-Review (Multi-Project Dashboard, April 2026)

- **No `assert` in background tasks**: `assert state.process is not None` in `asyncio.create_task()` coroutines crashes silently -- the task dies but nobody catches the `AssertionError`. Use `if x is None: return` guard instead. Found in `_read_output`, `_read_stderr`, `_wait_and_finalize`.
- **Context cleanup in middleware**: even though Python `contextvars` are task-scoped in asyncio, add explicit `clear_project_context()` in a `finally` block for defense-in-depth. Prevents any edge case where middleware context leaks between requests.
- **Pydantic models for API validation**: don't use raw `data = await request.json(); path = data["path"]` -- a missing key gives an unhandled `KeyError` (500 error). Use Pydantic `BaseModel` request parameters so FastAPI validates and returns 422 on bad input.
- **`dict.setdefault()` for concurrent state init**: when multiple async tasks can call `if key not in dict: dict[key] = new_value`, use `dict.setdefault(key, default)` instead. It's atomic for the check-and-set, preventing duplicate initialization.
