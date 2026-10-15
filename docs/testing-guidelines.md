# Testing Guidelines

Conventions and patterns for writing and running tests in the SOVA codebase.

## Running Tests

| Command | Purpose |
|---------|---------|
| `make check` | Lint + test, run before every push (close to CI, not identical: see below) |
| `make test` | Bash + Python tests |
| `make test-py` | pytest suite only |
| `make test-bash` | ShellCheck + invariant `--help` validation |
| `make lint` | ShellCheck + Ruff (lint + format check) |

CI runs `pytest tests/ -n auto -q --cov=sova --cov-report=xml --durations=25 --timeout=60` with `COVERAGE_CORE=sysmon` set. `-n auto` shards across every runner core, `sysmon` (the sys.monitoring coverage core, valid on Python 3.12+ because no branch coverage is configured) costs about 11% against 47% for the default tracer, at a line rate within half a point of it, and `--timeout=60` raises the 30s global, which worker contention on a CI runner breaches. `coverage.xml` is uploaded as the `coverage-xml` artifact, which `.github/workflows/sonarcloud.yml` downloads from the triggering run via a `workflow_run` trigger on CI's completion, rather than re-running the suite itself (#1187). The pre-push hook in `.githooks/pre-push` mirrors CI's lint, invariant, and secrets checks; it does not run pytest, so run `make test-py` before pushing, remembering that it deselects the `runtime`, `stress`, and `chaos` markers CI does run, and that it caps each test at pyproject's 30s rather than the 60s CI passes, so a slow test can fail locally while passing CI.

## Framework Configuration

```toml
# pyproject.toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
timeout = 30
```

`asyncio_mode = "auto"` means all `async def test_*` functions are async tests automatically: no `@pytest.mark.asyncio` needed. `timeout = 30` caps every test; a test whose runtime scales with the repository rather than with one unit of work (for example `tests/test_no_double_dash.py`'s whole-tree scan) needs an explicit `@pytest.mark.timeout(N)`, which overrides both this value and any `--timeout` passed on the command line. Dependencies: `pytest`, `pytest-asyncio`, `pytest-cov`, `pytest-timeout` (the `timeout` setting and `--timeout`), `pytest-xdist` (`-n auto`), `httpx` (ASGI testing), `aiosqlite`.

## Test File Layout

All tests live in `tests/` as flat modules, with one subdirectory: `tests/runtime/` holds the `@pytest.mark.runtime` process-lifecycle tests and the opt-in Codex smoke test, with its own `conftest.py`. The one flat-module exception is `tests/conftest.py`, which holds only autouse isolation fixtures and a small set of fixtures/helpers shared across many test modules (`seed_config`, `csrf_request_headers`, `InvariantRepo`/`invariant_repo`): it does not host per-file-only fixtures or helpers, those stay in their own test modules.

| Test file | Source package | Approx tests |
|-----------|---------------|--------------|
| `test_dashboard.py` | `sova/dashboard/` | ~775 |
| `test_core.py` | `sova/core/` | ~444 |
| `test_roles.py` | `sova/roles/` | ~322 |
| `test_agent_recovery.py` | `sova/dashboard/services/agent_recovery.py` | ~131 |
| `test_git.py` | `sova/git/` | ~150 |
| `test_cli.py` | `sova/cli/` | ~127 |
| `test_ipc.py` | `sova/ipc/` | ~119 |
| `test_adapters.py` | `sova/adapters/` | ~90 |
| `test_config.py` | `sova/config/` | ~109 |
| `test_agent_handoff.py` | `sova/dashboard/services/agent_handoff.py` | ~23 |
| `test_agent_pool.py` | `sova/dashboard/services/agent_pool.py` | ~17 |

Plus: `test_batch_service.py`, `test_commands.py`, `test_dag.py`, `test_db.py`, `test_external_reviews.py`, `test_extraction.py`, `test_harden.py`, `test_jira_adapter.py`, `test_knowledge.py`, `test_knowledge_sharing.py`, `test_lifecycle.py`, `test_llm.py`, `test_mcp.py`, `test_scheduler.py`, `test_settings_meta.py`, `test_spec.py`, `test_utils.py`.

Tests use classes to group related assertions. Fixtures are defined per-file, not in conftest, except for the env-isolation fixture noted above.

## In-Memory Database Fixture

```python
@pytest.fixture(autouse=True)
async def setup_db(monkeypatch):
    monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
    await init_db(run_migrations=False)
    yield
    await close_db()
```

- `run_migrations=False` uses `create_all` (faster than Alembic for tests)
- `close_db()` disposes the engine so each test gets a fresh DB
- `monkeypatch.setenv` scopes the env var to the test, so a failing test cannot leak it into later ones. Never assign to or pop from `os.environ` directly
- If the test exercises a code path that calls `FeedService.emit()`/`emit_safe()` (e.g. `start_agent`, `start_batch`, auto-handoff), `await drain_feed_persist()` (from `tests/conftest.py`) before `close_db()`: `emit()` schedules a background DB write that otherwise races the teardown, throwing on the now-closed engine and logging `feed.persist_failed`

## Test Logging Configuration

`tests/conftest.py` configures structlog once per session (`configure_test_logging()`, applied by the autouse `_configure_test_logging` fixture) with production `setup_logging()`'s own processor chain plus `ConsoleRenderer(colors=False)`: `format_exc_info` flattens an `exc_info=True` log into a plain string, so the renderer never reaches structlog's default rich-with-locals traceback formatter (which is why passing an `exception_formatter` there would be dead config). Debug-level filtering is unchanged, so `structlog.testing.capture_logs()` still captures debug events exactly as before. Do not copy production's `cache_logger_on_first_use=True` into this chain: production configures once, while `configure_test_logging()` is re-applied whenever a test perturbs the process-global config, and caching freezes an already-used logger against the processor list it saw first. Both `capture_logs()` (which swaps the configured list in place) and the re-application then silently stop reaching that logger, and caching saves nothing at test scale (9 us per call measured either way). `logger_factory` is set explicitly to `structlog.PrintLoggerFactory(file=sys.stdout)`, bound fresh at call time, rather than left unset: production's own `setup_logging()` binds its factory to whichever `sys.stderr` object is live when it runs, and pytest's output capture closes that stream at the end of each test, so a test that calls production `setup_logging()` unpatched and then re-applies `configure_test_logging()` would otherwise inherit a factory pointing at a closed stream, raising `ValueError: I/O operation on closed file` on the next write. `stdout`, not `stderr`, to match the test suite's existing convention: `tests/test_jira_adapter.py::test_unmapped_status_logs_warning` already asserts a structlog warning lands on `capsys`'s `.out`, relying on structlog's unconfigured default (`PrintLoggerFactory()` with no `file`, which resolves to `stdout`). `tests/test_logging_rotation.py::TestTestLoggingConfiguration` guards the setting. Patch production `setup_logging()` (`sova/utils/logging.py`) out rather than calling it unpatched, as `test_dashboard.py` and `test_core.py` do: it re-runs `structlog.configure()` with its own default filter level (INFO) and leaves it there for the rest of the process, silently dropping every later debug event. A test that must call it for real has to call `configure_test_logging()` again in teardown, as `test_logging_rotation.py` does; a session-scoped fixture cannot reinstate process-global state on its own.

## Mock Patterns

### Patch at the import site, not the definition site

```python
# Correct: patch where it's imported
with patch("sova.adapters.github.run", new_callable=AsyncMock) as mock_run:

# Wrong: patching the definition module has no effect on the importer
with patch("sova.utils.shell.run", new_callable=AsyncMock):
```

Exception: when the code under test imports inside the function body (`from x import y` in the function), there is no module-level binding to patch, so patch the source module (`x.y`) and leave a one-line comment at the patch site explaining why.

### ShellResult helpers

Each test file defines its own factory:

```python
def _shell_result(stdout="", stderr="", returncode=0):
    return ShellResult(returncode=returncode, stdout=stdout, stderr=stderr)
```

### Multi-call methods with `side_effect`

```python
mock_run.side_effect = [
    _shell_result(stdout='{"labels": []}'),  # first call
    _shell_result(),                          # second call
]
```

### Context factory: `_make_ctx`

```python
def _make_ctx(**kwargs) -> ExecutionContext:
    defaults = {
        "project_dir": Path("/tmp/test"),
        "config": ProjectConfig(),
        "adapter": _mock_adapter(),
        "issue_number": "42",
        "role": "developer",
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)
```

### `patch.object` for split modules

When a module was split with a re-export facade, patch the actual submodule:

```python
# Correct
patch.object(agent_lifecycle, "_get_project_agents", return_value=pa)

# Wrong: only patches the facade's attribute
patch.object(control_service, "_get_project_agents", return_value=pa)
```

## Dashboard / FastAPI Testing

```python
@pytest.fixture
async def client(tmp_path):
    app = create_app(project_dir=tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
```

### File-backed service isolation

Services with mtime caches (`handoff_service`, `log_service`) need monkeypatching:

```python
monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
handoff_service._handoff_caches.clear()
```

## CLI Testing

```python
from typer.testing import CliRunner
runner = CliRunner()

result = runner.invoke(app, ["status"])
assert result.exit_code == 0
```

## Bash / Invariant Testing

Each invariant script must:
1. Pass `shellcheck` (enforced by `make lint-bash`)
2. Handle `--help` gracefully (tested by `make test-bash`)

## Parametrized Tests

```python
@pytest.mark.parametrize(
    "model_cls, field, invalid_value",
    [
        (AgentConfig, "max_budget", Decimal("-1")),
        (AgentConfig, "step_timeout", 0),
    ],
)
def test_rejects_invalid_values(model_cls, field, invalid_value):
    with pytest.raises(ValueError):
        model_cls(**{field: invalid_value})
```

## Common Pitfalls

1. **Missing cache clears**: file-backed services cache by mtime. Clear `_handoff_caches` in tests.
2. **Async context managers**: always `async with await get_session() as session:`.
3. **autouse DB fixture scope**: per-test (default). Never use `scope="module"` for DB fixtures.
3a. **Module-level caches and lock dicts**: a PR that adds one to `sova/` also adds an autouse fixture that `.clear()`s it before and after each test. An `asyncio.Lock` is bound to the event loop that first used it.
3b. **A regression test must fail with the fix reverted**: revert the fix locally and confirm the test fails before trusting it. A concurrency test whose mocks never suspend (no `await asyncio.sleep(0)` or `asyncio.Event` barrier) runs sequentially and proves nothing.
4. **No per-file-only helpers in conftest.py**: helpers used by a single file (`_make_ctx`, `_shell_result`) are defined per-file. Copy the pattern. `tests/conftest.py` is reserved for autouse isolation fixtures and helpers genuinely shared across multiple test modules.
5. **Ambient `SOVA_*` env vars**: a dev shell running SOVA agents may export `SOVA_MCP_TOKEN`, `SOVA_AGENT_RUN`, etc. `tests/conftest.py`'s autouse `_isolate_sova_env` fixture strips all `SOVA_*` vars except `SOVA_DATABASE_URL` before each test so Pydantic Settings config construction is deterministic. If a test needs a specific `SOVA_*` var, set it explicitly via `monkeypatch.setenv` inside the test.
6. **Never write to a hard-coded `/tmp/...` path**: use the `tmp_path`/`tmp_path_factory` fixtures. A fixed path is a real, shared location on disk that collides across `pytest-xdist` workers and survives between runs. `tests/conftest.py`'s autouse `_isolate_cwd` fixture already chdirs each test into a fresh temp directory (so `load_config(None)`'s `Path.cwd()` fallback can't read the developer's real `.claude/sova.db`), and the sibling autouse `_isolate_home_config` fixture monkeypatches `Path.home()` (plus `sova.config.registry._REGISTRY_DIR`/`_REGISTRY_FILE` directly, since those module constants are bound to the real `Path.home()` at import time) to a fresh temp directory, so `sova/db/session.py:_get_database_url()`'s no-project-dir fallback and the project registry can never resolve under the developer's real `~/.config/sova/`. These two fixtures cover the ambient fallback paths; they do not protect an explicit `Path("/tmp/...")` literal passed as `project_dir` or similar, which must be replaced with `tmp_path` at each call site.
