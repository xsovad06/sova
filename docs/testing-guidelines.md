# Testing Guidelines

Conventions and patterns for writing and running tests in the SOVA codebase.

## Running Tests

| Command | Purpose |
|---------|---------|
| `make check` | Lint + `make test-bash` + `make test-ci`, run before every push |
| `make test` | Bash + Python tests (local iteration, via `make test-py`) |
| `make test-ci` | The exact pytest invocation CI runs: used by both CI's test matrix and `make check` so neither can diverge from the other |
| `make test-py` | pytest suite only, tuned for local iteration (see below) |
| `make test-bash` | ShellCheck + invariant `--help` validation |
| `make lint` | ShellCheck + Ruff (lint + format check) |

`make test-ci` runs `pytest tests/ -n auto -q --cov=sova --cov-report=xml --cov-report=term:skip-covered --durations=25 --timeout=60` with `COVERAGE_CORE=sysmon` set, and both CI and `make check` call this one target rather than each keeping their own copy of the invocation (#1209). CI runs it once per Python version in its matrix (3.12 and 3.14 on a pull request; 3.12 only on a push to main). `-n auto` shards across every runner core, `sysmon` (the sys.monitoring coverage core, valid on Python 3.12+ because no branch coverage is configured) costs about 11% against 47% for the default tracer, at a line rate within half a point of it, and `--timeout=60` raises the 30s global, which worker contention on a CI runner breaches. `coverage.xml` is uploaded as the `coverage-xml` artifact, which `.github/workflows/sonarcloud.yml` downloads from the triggering run via a `workflow_run` trigger on CI's completion, rather than re-running the suite itself (#1187). The pre-push hook in `.githooks/pre-push` mirrors CI's lint, invariant, and secrets checks; it does not run pytest, so run `make check` (or `make test-ci`) before pushing.

`make test-ci`'s `-n auto` shards across every core, so unlike `make test-py`, two or more concurrent `make check` runs (e.g. parallel pipeline steps) compete for the whole machine instead of yielding to each other: this is a deliberate tradeoff (#1209) in favor of `make check` never passing on an invocation CI wouldn't also pass on, at the cost of the isolation `make test-py` gives concurrent agents. Use `make test-py` directly for local iteration when several pipeline steps may be running checks at once: it deselects the `runtime`, `stress`, `chaos`, and `evaluation` markers CI does run (an `evaluation` test is kept out of CI only by its own env-var `skipif`, exactly as `codex_smoke` is), caps each test at pyproject's 30s rather than the 60s `make test-ci` passes, and uses the Makefile's `PYTEST_WORKERS` variable (default 2: `PYTEST_WORKERS ?= $(or $(PYTEST_XDIST_AUTO_NUM_WORKERS),2)`) instead of `-n auto`, overridable via `make test-py PYTEST_WORKERS=4` or the pytest-xdist-native `PYTEST_XDIST_AUTO_NUM_WORKERS` environment variable. Measured on a 14-core Apple Silicon machine: a single `make test-py` run at the default of 2 workers completes the full suite (11484 passed, 7 skipped) in about 378s wall time; two concurrent `make test-py` runs (4 pytest-xdist workers total) each complete in about 333s (#1156).

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
| `test_dashboard.py` | `sova/dashboard/` | ~1064 |
| `test_core.py` | `sova/core/` | ~563 |
| `test_roles.py` | `sova/roles/` | ~381 |
| `test_agent_recovery.py` | `sova/dashboard/services/agent_recovery.py` | ~92 |
| `test_git.py` | `sova/git/` | ~219 |
| `test_cli.py` | `sova/cli/` | ~137 |
| `test_ipc.py` | `sova/ipc/` | ~251 |
| `test_adapters.py` | `sova/adapters/` | ~104 |
| `test_config.py` | `sova/config/` | ~147 |
| `test_agent_handoff.py` | `sova/dashboard/services/agent_handoff.py` | ~24 |
| `test_agent_pool.py` | `sova/dashboard/services/agent_pool.py` | ~17 |

Plus: `test_batch_service.py`, `test_commands.py`, `test_dag.py`, `test_db.py`, `test_external_reviews.py`, `test_extraction.py`, `test_harden.py`, `test_jira_adapter.py`, `test_knowledge.py`, `test_knowledge_sharing.py`, `test_lifecycle.py`, `test_llm.py`, `test_mcp.py`, `test_scheduler.py`, `test_settings_meta.py`, `test_spec.py`, `test_utils.py`.

Tests use classes to group related assertions. Fixtures are defined per-file, not in conftest, except for the autouse isolation fixtures and the shared DB fixture (`reset_db_rows`) described below.

## In-Memory Database Fixture

Request the shared fixture; do not build an engine per test:

```python
# Whole module (replaces the old autouse per-file setup_db fixture)
pytestmark = pytest.mark.usefixtures("reset_db_rows")

# Or one class / one test
@pytest.mark.usefixtures("reset_db_rows")
class TestSomething: ...
```

`tests/conftest.py` builds the schema once per pytest-xdist worker
(`_shared_test_db_engine`, session-scoped) and `reset_db_rows` deletes every row
from every table before each test, in reverse `Base.metadata.sorted_tables`
order. The trade is speed: an `init_db(run_migrations=False)` plus `close_db()`
cycle measures 22.7ms against 3.2ms for the delete sweep, and the migrated files
previously spent more wall-clock in fixture setup than in their own test bodies
(issue #1189). `tests/test_shared_db_fixture.py` guards the isolation
guarantee and the self-heal path below.

What `reset_db_rows` does beyond deleting rows, and why:

- Re-pins `sova.db.session`'s `_engine`/`_session_factory`/`_engines` globals and `SOVA_DATABASE_URL` on every test, not once. Any file still running its own `init_db()`/`close_db()` per test rebinds those same module globals and pops that same env var at teardown, and once the var is gone `get_session()` resolves a different, schema-less database
- Sets the env var through the test's own `monkeypatch` fixture, never raw `os.environ`. A hand-rolled restore in a `finally` block is unsafe when the test body also calls `monkeypatch.setenv("SOVA_DATABASE_URL", ...)`, because pytest does not guarantee teardown ordering between the two
- Treats `OperationalError` on the sweep as a disposed engine (for `sqlite+aiosqlite://` with no path, disposal discards the database itself, so the next checkout silently opens a fresh schema-less one), recreates the schema, then **retries the sweep**. The retry is deliberate: an `OperationalError` from any other cause rolls the first sweep's deletes back, so swallowing it would hand the test a half-populated DB

The shared engine is forced onto `StaticPool` (one physical connection for every checkout), so every session opened against it within one test shares that single connection, and therefore one transaction, with every other session opened in the same test: an uncommitted write in one session is visible to a second session opened in the same test, that second session's `commit()` persists it, and a later `rollback()` on the first session cannot undo it. A test asserting commit/rollback semantics specifically, or "the failure path leaves no row behind", cannot be migrated onto this engine and still mean what it asserts; give it its own engine instead (see below).

### When a test may build its own engine

Keep a file-local fixture only with a documented reason in its docstring. The two current cases:

- **`init_db()`/`close_db()`/engine disposal is the thing under test** (`tests/test_db.py`): those tests need the real WAL, migration, and disposal side effects against their own `tmp_path` project directories
- **A test leaves a background task touching the DB when its event loop closes** (`tests/test_dashboard.py`): the resulting `CancelledError` invalidates the pooled connection, and SQLAlchemy's transparent reconnect replaces the shared in-memory DB with a fresh schema-less one for every later test in the worker. Disposing per test makes that harmless, because the engine is already on its way out

In both cases use `monkeypatch.setenv`, not assignment to or popping from `os.environ` directly, and prefer `run_migrations=False` (`create_all`, faster than Alembic).

If the test exercises a code path that calls `FeedService.emit()`/`emit_safe()` (e.g. `start_agent`, `start_batch`, auto-handoff) and then queries feed rows, `await drain_feed_persist()` (from `tests/conftest.py`) yourself before that query, or before `close_db()` in a file-local fixture: `emit()` schedules a background DB write that otherwise races the read or the teardown, throwing on a since-closed engine and logging `feed.persist_failed`. `reset_db_rows` does not do this for you: it runs as fixture *setup*, before the test body, so it can only ever drain tasks left over from a previous test (which pytest-asyncio's per-test runner teardown has already drained anyway), never a write the current test schedules.

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

Take `tmp_path` as the first parameter rather than defaulting `project_dir` to a
literal `/tmp/...` path: a shared literal collides across parallel test workers
and across runs, where `tmp_path` is unique per test. Callers pass their own
`tmp_path` fixture through.

```python
def _make_ctx(tmp_path: Path, **kwargs) -> ExecutionContext:
    defaults = {
        "project_dir": tmp_path,
        "config": ProjectConfig(),
        "adapter": _mock_adapter(),
        "issue_number": "42",
        "role": "developer",
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


def test_something(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, issue_number="7")
    ...
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
3. **DB fixture scope**: request the shared `reset_db_rows` fixture (per-test row reset over one session-scoped engine per xdist worker); see "In-Memory Database Fixture" above. Do not add a `scope="module"` DB fixture: module scope gives neither per-test isolation nor the shared engine's speed, and an engine built in one module's scope is still live when the next module runs.
3a. **Module-level caches and lock dicts**: a PR that adds one to `sova/` also adds an autouse fixture that `.clear()`s it before and after each test. An `asyncio.Lock` is bound to the event loop that first used it.
3b. **A regression test must fail with the fix reverted**: revert the fix locally and confirm the test fails before trusting it. A concurrency test whose mocks never suspend (no `await asyncio.sleep(0)` or `asyncio.Event` barrier) runs sequentially and proves nothing.
3c. **Seeding fixture ordering**: `reset_db_rows` is requested via `pytest.mark.usefixtures`, not `autouse=True`, so pytest resolves it *after* any autouse fixture in the same module or class. A file-specific fixture that inserts rows and is itself written as `@pytest.fixture(autouse=True)` therefore runs first and has its rows silently deleted before the test body sees them, no error, no failure, just a test that passes vacuously. Take `reset_db_rows` as an explicit parameter on that seeding fixture, even when it is autouse (`async def seed_rows(reset_db_rows): ...`), to force the correct order.
4. **No per-file-only helpers in conftest.py**: helpers used by a single file (`_make_ctx`, `_shell_result`) are defined per-file. Copy the pattern. `tests/conftest.py` is reserved for autouse isolation fixtures and helpers genuinely shared across multiple test modules.
5. **Ambient `SOVA_*` env vars**: a dev shell running SOVA agents may export `SOVA_MCP_TOKEN`, `SOVA_AGENT_RUN`, etc. `tests/conftest.py`'s autouse `_isolate_sova_env` fixture strips all `SOVA_*` vars except `SOVA_DATABASE_URL` before each test so Pydantic Settings config construction is deterministic. If a test needs a specific `SOVA_*` var, set it explicitly via `monkeypatch.setenv` inside the test.
6. **Never write to a hard-coded `/tmp/...` path**: use the `tmp_path`/`tmp_path_factory` fixtures. A fixed path is a real, shared location on disk that collides across `pytest-xdist` workers and survives between runs. `tests/conftest.py`'s autouse `_isolate_cwd` fixture already chdirs each test into a fresh temp directory (so `load_config(None)`'s `Path.cwd()` fallback can't read the developer's real `.claude/sova.db`), and the sibling autouse `_isolate_home_config` fixture monkeypatches `Path.home()` (plus `sova.config.registry._REGISTRY_DIR`/`_REGISTRY_FILE` directly, since those module constants are bound to the real `Path.home()` at import time) to a fresh temp directory, so `sova/db/session.py:_get_database_url()`'s no-project-dir fallback and the project registry can never resolve under the developer's real `~/.config/sova/`. These two fixtures cover the ambient fallback paths; they do not protect an explicit `Path("/tmp/...")` literal passed as `project_dir` or similar, which must be replaced with `tmp_path` at each call site. `tests/conftest.py`'s `pytest_sessionstart`/`pytest_sessionfinish` hooks record and compare the mtime of three specific paths known to have been hardcoded this way historically (`/tmp/test`, `/tmp/test-project`, `/tmp/worktree`), plus the mtime of every path beneath each of them, and fail the session if any of those changed, appeared, or disappeared, catching a future literal reintroduced on a path a mocked collaborator happens to share (#1209). The descendants matter as much as the roots: writing `/tmp/test/existing.txt` inside a pre-existing `/tmp/test` directory leaves that directory's own mtime untouched, so a root-only snapshot would miss it.
7. **Warnings are promoted to errors**: `pyproject.toml`'s `filterwarnings = ["error", ...]` turns any warning not explicitly allowlisted there into a test failure, so a new warning is caught immediately instead of scrolling by unnoticed. Each allowlist entry is scoped as narrowly as the warning category (and, where practical, message) allows; fix the underlying cause (e.g. an `AsyncMock` coroutine a test's own early-return path never awaits) rather than widening an entry, and never allowlist `PytestUnraisableExceptionWarning` as a shortcut around a task whose exception is genuinely never retrieved.
