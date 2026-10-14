---
name: testing-patterns
description: "SOVA test conventions: pytest async patterns, mock strategies, fixture patterns, dashboard ASGI testing. Auto-activates when writing or modifying test files in the tests/ directory."
allowed_tools: Read, Grep, Glob, Bash, Edit, Write
---

# SOVA Testing Patterns

When writing or modifying files in `tests/`, follow these conventions. Reference: `docs/testing-guidelines.md`.

## Framework

- `asyncio_mode = "auto"`: no `@pytest.mark.asyncio` needed on async tests
- All tests flat in `tests/`: no subdirectories, no `conftest.py`
- Tests grouped with classes. Fixtures defined per-file, not shared.

## Required Fixtures

### In-memory DB (autouse in any file touching ORM):
```python
@pytest.fixture(autouse=True)
async def setup_db(monkeypatch):
    monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
    await init_db(run_migrations=False)
    yield
    await close_db()
```

### Dashboard client:
```python
@pytest.fixture
async def client(tmp_path):
    app = create_app(project_dir=tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
```

## Mock Rules

1. **Patch at the import site**, not the definition site:
   ```python
   # Correct
   patch("sova.adapters.github.run", new_callable=AsyncMock)
   # Wrong
   patch("sova.utils.shell.run", new_callable=AsyncMock)
   ```
   Exception: when the code under test imports inside the function body (`from x import y` in the function), there is no module-level binding to patch, so patch the source module (`x.y`). Leave a one-line comment at the patch site so a reviewer does not "fix" it back.

1b. **Environment variables**: always `monkeypatch.setenv`/`delenv`, never assign to or pop from `os.environ` directly. A direct mutation outlives a failing test and leaks into every later test in the process.

2. **Use `patch.object` for split modules**: patch the actual submodule, not the re-export facade:
   ```python
   patch.object(agent_lifecycle, "_get_project_agents", return_value=pa)
   ```

3. **ShellResult factory**: define per-file:
   ```python
   def _shell_result(stdout="", stderr="", returncode=0):
       return ShellResult(returncode=returncode, stdout=stdout, stderr=stderr)
   ```

4. **Context factory**: define per-file:
   ```python
   def _make_ctx(**kwargs) -> ExecutionContext:
       defaults = {"project_dir": Path("/tmp/test"), "config": ProjectConfig(),
                   "adapter": _mock_adapter(), "issue_number": "42", "role": "developer"}
       defaults.update(kwargs)
       return ExecutionContext(**defaults)
   ```

5. **Multi-call methods**: use `side_effect` list.

## Pitfalls

- **Clear caches**: file-backed services (handoff, log) cache by mtime. Clear `_handoff_caches` in tests.
- **Session pattern**: always `async with await get_session() as session:`.
- **DB fixture scope**: per-test only. Never `scope="module"`.
- **No conftest.py**: copy helpers per-file, don't share.
- **A new module-level cache or lock dict needs an autouse clear fixture in the same PR**: any `_cache: dict` or `_locks: dict[..., asyncio.Lock]` added to `sova/` gets an autouse fixture that `.clear()`s it before and after each test. An `asyncio.Lock` is bound to the event loop that first used it, so a stale one leaks across tests and can bypass active patches.
- **A regression or concurrency test must fail with the fix reverted**: before trusting it, revert the fix locally and watch it fail. A `gather()` test whose mocks never suspend (no `await asyncio.sleep(0)` or `asyncio.Event` barrier) runs sequentially and proves nothing about concurrency.
- **Module-level cache requires `setup_method` in ALL test classes**: when a service function has a module-level cache, add `setup_method: clear_cache()` to every test class in the file that calls it, not just new ones. Cache type changes (`tuple|None` -> `dict`) require updating all reset patterns (`= None` -> `.clear()`).
- **Direct unit tests for mocked functions**: if a function is always mocked in integration tests, SonarCloud flags 0% coverage. Add a dedicated test class with direct calls to cover the implementation paths.
- **`MagicMock(name="foo")` sets repr, not `.name`**: `name=` is a reserved constructor arg. Assign after construction: `mock = MagicMock(); mock.name = "foo"`.
- **Spawn mock functions must use `**kwargs`**: `async def _capture_spawn(prompt, cwd, **kwargs)` absorbs new parameters without breaking when `spawn()` gains kwargs.
- **`TaskRun` uses `started_at`, not `created_at`**: most ORM models use `created_at`, but `TaskRun` uses `started_at`/`ended_at`. Referencing `TaskRun.created_at` raises `AttributeError` silently swallowed by broad `except Exception`.

## Running

- `make check` before push (close to CI, not identical: see docs/testing-guidelines.md)
- `make test-py` for pytest only
- `make lint` for ShellCheck + Ruff
