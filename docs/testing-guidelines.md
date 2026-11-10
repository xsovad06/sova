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
| `make lint` | ShellCheck + Ruff (lint + format check) + `make lint-js` (`node --check` on every dashboard script and ES module; `tests/test_dashboard_agents_modules.py` runs the same check under pytest, where a missing `node` skips locally but fails when `CI` is set; CI's Static Checks job also runs `make lint-js` directly). Node is a dev requirement for `make lint` |

`make test-ci` runs `pytest tests/ -n auto -q --cov=sova --cov-report=xml --cov-report=term:skip-covered --durations=25 --timeout=60` with `COVERAGE_CORE=sysmon` set, and both CI and `make check` call this one target rather than each keeping their own copy of the invocation (#1209). CI runs it once per Python version in its matrix (3.12 and 3.14 on a pull request; 3.12 only on a push to main). `-n auto` shards across every runner core, `sysmon` (the sys.monitoring coverage core, valid on Python 3.12+ because no branch coverage is configured) costs about 11% against 47% for the default tracer, at a line rate within half a point of it, and `--timeout=60` restates the global `timeout = 60`, so `make test-py` and `make test-ci` time a test out at the same point. `coverage.xml` is uploaded as the `coverage-xml` artifact, which `.github/workflows/sonarcloud.yml` downloads from the triggering run via a `workflow_run` trigger on CI's completion, rather than re-running the suite itself (#1187). The pre-push hook in `.githooks/pre-push` mirrors CI's lint, invariant, and secrets checks; it does not run pytest, so run `make check` (or `make test-ci`) before pushing.

`make test-ci`'s `-n auto` shards across every core unless `PYTEST_XDIST_AUTO_NUM_WORKERS` says otherwise, and `make check` keeps it so it never passes on an invocation CI wouldn't also pass on (#1209). SOVA bounds concurrent checks through that variable instead of the command (#1191): `scrub_check_env()` (every check command a pipeline step runs, including the baseline runner, which receives the step's config; the git `commit()`, `push()` and `preserve_partial_work()` helpers take the caller's `config`, the supervisor's rebase push and pre-push hook pass the project directory, and a call with neither reads the cap from the current working directory's config, which belongs to whoever runs it; a git-only call that runs no suite uses `scrub_orchestrator_env()`, a pure scrub with no cap) and `agent_dispatch._run_agent_process()` (every agent it dispatches, whose own `make check` inherits it) set it to `testing.check_workers` when that is non-zero, else to `max(2, cpu_count // max_parallel_agents)`, and leave an operator's own value alone. CI never sets it, so its runners keep every core. Measured on a 14-core Apple Silicon machine on 2026-11-10, alongside another session's suite run (#1159): one `make check` took 133 s at `-n auto` and 345 s at 4 workers; three concurrent runs at 4 workers each (SOVA's split for three agents) took 481 to 509 s at a peak 1-minute load of 29, against 384 s and a peak load of 51 for three at `-n auto`. Every run passed, so there is no shared full-check lock: the cap halves the peak load at about 30% longer per check, and the slowest capped run still finished 91 s inside `develop.check_timeout` (600 s). A `make check` you start by hand outside SOVA still uses every core: set the variable yourself, or use `make test-py` directly for local iteration when several pipeline steps may be running checks at once: it deselects the `runtime`, `stress`, `chaos`, and `evaluation` markers CI does run (an `evaluation` test is kept out of CI only by its own env-var `skipif`, exactly as `codex_smoke` is), caps each test at the same 60s as `make test-ci`, and uses the Makefile's `PYTEST_WORKERS` variable (default 2: `PYTEST_WORKERS ?= $(or $(PYTEST_XDIST_AUTO_NUM_WORKERS),2)`) instead of `-n auto`, overridable via `make test-py PYTEST_WORKERS=4` or the pytest-xdist-native `PYTEST_XDIST_AUTO_NUM_WORKERS` environment variable. Measured on a 14-core Apple Silicon machine: a single `make test-py` run at the default of 2 workers completes the full suite (11484 passed, 7 skipped) in about 378s wall time; two concurrent `make test-py` runs (4 pytest-xdist workers total) each complete in about 333s (#1156).

## CI Matrix and Opt-in Targets

- **Required-check name**: GitHub suffixes the matrix job's own check name per leg, so a separate unmatrixed `python-test` aggregator job owns the "Python Tests" required-check name that the main-protection ruleset matches by exact string. The matrix, `-n auto` and the `coverage-xml` artifact are described above; the artifact is uploaded from the 3.12 leg only.
- **Opt-in, real-money targets**: `make test-codex-smoke` (shells out to a real `codex` CLI) and `make test-evaluation` (runs the model-evaluation fixture matrix against a live LLM provider; see `docs/model-selection-architecture.md` section 2.11). Neither is kept out of CI by its marker: CI collects both and each is skipped only by its own env-var `skipif` (`SOVA_CODEX_SMOKE`, `SOVA_EVAL_LIVE`). `make test-py` deselects `evaluation` but not `codex_smoke`, which its own `skipif` handles.

## Framework Configuration

```toml
# pyproject.toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
timeout = 60
```

`asyncio_mode = "auto"` means all `async def test_*` functions are async tests automatically: no `@pytest.mark.asyncio` needed. `asyncio_default_fixture_loop_scope` must stay set: left unset, pytest-asyncio warns while pytest configures, and from pytest 9.2 on the `filterwarnings = ["error"]` entry turns that warning into a session abort before any test runs (`tests/test_pytest_config.py` guards it). `timeout = 60` caps every test; a test whose runtime scales with the repository rather than with one unit of work needs an explicit `@pytest.mark.timeout(N)`, which overrides both this value and any `--timeout` passed on the command line, and a `slow(reason=...)` marker (see "Duration Budget"). A whole-tree scan belongs in `make lint-tree` instead. Dependencies: `pytest`, `pytest-asyncio`, `pytest-cov`, `pytest-timeout` (the `timeout` setting and `--timeout`), `pytest-xdist` (`-n auto`), `httpx` (ASGI testing), `aiosqlite`.

## Duration Budget

`tests/duration_budget.py` (loaded through `tests/conftest.py`'s `pytest_plugins`) fails any test whose own setup, call and teardown time exceeds `SOVA_TEST_DURATION_BUDGET` seconds, with the measured split in the failure. CI sets it to 8 on the "Run tests with coverage" step; it is unset locally, where timings swing with whatever else the machine runs, so a local run never fails on duration. Reproduce a CI failure with `SOVA_TEST_DURATION_BUDGET=8 make test-ci`. An over-budget test is reported as an error at teardown, the point where its whole cost is known. The count is the test's own cost, so it does not depend on which test xdist schedules first: setup leaves out fixtures wider than `function` scope (a per-module app, a session DB template), and teardown is counted only when the next test is in the same module or class (otherwise the report also holds the wider fixtures' teardown, so the last test of a module escapes the teardown half). A test that is slow by nature carries `@pytest.mark.slow(reason="...")`, which exempts it; a `slow` marker without a reason fails the test everywhere, with or without the budget, so `make check` matches CI. Prefer making the test fast: a test that waits out a real timeout patches the constant down instead (`tests/test_shell_timeout.py` patches `sova.utils.shell._KILL_TIMEOUT_SECONDS` to 0.05 s rather than waiting the real 5 s). The budget is separate from `timeout = 60`, which kills a hung test rather than flagging a slow one.

## Test File Layout

All tests live in `tests/` as flat modules, with one subdirectory: `tests/runtime/` holds the `@pytest.mark.runtime` process-lifecycle tests and the opt-in Codex smoke test, with its own `conftest.py`. The one flat-module exception is `tests/conftest.py`, which holds only autouse isolation fixtures and a small set of fixtures/helpers shared across many test modules (`reset_db_rows`, `dashboard_app`, `seed_config`, `csrf_request_headers`, `InvariantRepo`/`invariant_repo`/`invariant_history`): it does not host per-file-only fixtures or helpers, those stay in their own test modules.

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

Tests use classes to group related assertions. Fixtures are defined per-file, not in conftest, except for the autouse isolation fixtures, the shared DB fixture (`reset_db_rows`) and the shared dashboard app (`dashboard_app`) described below.

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

- Re-pins `sova.db.session`'s `_engine`/`_session_factory`/`_engines` globals and `SOVA_DATABASE_URL` on every test, not once. A test that calls `init_db()`/`close_db()` itself (`tests/test_db.py`, or any CLI command under test, which calls `init_db(project_dir)`) rebinds those same module globals
- Sets the env var through the test's own `monkeypatch` fixture, never raw `os.environ`. A hand-rolled restore in a `finally` block is unsafe when the test body also calls `monkeypatch.setenv("SOVA_DATABASE_URL", ...)`, because pytest does not guarantee teardown ordering between the two
- Treats `OperationalError` on the sweep as a disposed engine (for `sqlite+aiosqlite://` with no path, disposal discards the database itself, so the next checkout silently opens a fresh schema-less one), recreates the schema, then **retries the sweep**. The retry is deliberate: an `OperationalError` from any other cause rolls the first sweep's deletes back, so swallowing it would hand the test a half-populated DB
- Gives the shared connection a fresh `_execute_mutex`. SQLAlchemy's asyncio adapter serializes statements on a connection through an `asyncio.Lock`, which binds to the running loop the first time two coroutines contend for it; every test runs on its own loop, so a test running queries concurrently would otherwise break the next one that does ("is bound to a different event loop")
- At teardown, awaits pending feed-persistence tasks (`drain_feed_persist()`) and disposes every engine other than the shared one (a CLI command's `init_db(project_dir)`, a `get_session(project_dir)` per-project engine). A task pytest-asyncio cancels mid-query at loop close, or an engine dropped without `dispose()`, leaves its connection to the garbage collector, which cannot close an async connection and warns on whichever later test is running

The shared engine is forced onto `StaticPool` (one physical connection for every checkout), so every session opened against it within one test shares that single connection, and therefore one transaction, with every other session opened in the same test: an uncommitted write in one session is visible to a second session opened in the same test, that second session's `commit()` persists it, and a later `rollback()` on the first session cannot undo it. A test asserting commit/rollback semantics specifically, or "the failure path leaves no row behind", cannot be migrated onto this engine and still mean what it asserts; give it its own engine instead (see below).

### Tests that call `init_db()`/`close_db()` themselves

No test file keeps a per-test engine fixture. Where `init_db()`, `close_db()` or engine disposal is the thing under test (`tests/test_db.py`), the test calls it against its own `tmp_path` and clears `SOVA_DATABASE_URL` with `monkeypatch.delenv`. Its `close_db()` disposes the shared engine too, and the next test's sweep recreates the schema. A test whose background task outlives its event loop (the real `start_agent()` calls in `tests/test_dashboard.py`) can invalidate the shared connection in the same way, with the same recovery.

Never assign to, pop, or `setdefault` `os.environ["SOVA_DATABASE_URL"]` directly: use `reset_db_rows`, or `monkeypatch.setenv`/`delenv`. The old per-file fixtures popped the variable at teardown, which removed it for the next test in the worker. `pytest_runtest_setup` in `tests/conftest.py` fails every test of a module that does it.

A test whose code path reaches the DB at all, even only through `FeedService.emit()`/`emit_safe()` (e.g. `start_agent`, `start_batch`, rate-limit tracking), should request `reset_db_rows`, so the teardown drain runs. A test that emits and then queries feed rows also needs `await drain_feed_persist()` in its body, before the query: `emit()` schedules the write in the background.

## Test Logging Configuration

`tests/conftest.py` configures structlog once per session (`configure_test_logging()`, applied by the autouse `_configure_test_logging` fixture) with production `setup_logging()`'s own processor chain plus `ConsoleRenderer(colors=False)`: `format_exc_info` flattens an `exc_info=True` log into a plain string, so the renderer never reaches structlog's default rich-with-locals traceback formatter (which is why passing an `exception_formatter` there would be dead config). Debug-level filtering is unchanged, so `structlog.testing.capture_logs()` still captures debug events exactly as before. Do not copy production's `cache_logger_on_first_use=True` into this chain: production configures once, while `configure_test_logging()` is re-applied whenever a test perturbs the process-global config, and caching freezes an already-used logger against the processor list it saw first. Both `capture_logs()` (which swaps the configured list in place) and the re-application then silently stop reaching that logger, and caching saves nothing at test scale (9 us per call measured either way). `logger_factory` is set explicitly to `structlog.PrintLoggerFactory(file=sys.stdout)`, bound fresh at call time, rather than left unset: production's own `setup_logging()` binds its factory to whichever `sys.stderr` object is live when it runs, and pytest's output capture closes that stream at the end of each test, so a test that calls production `setup_logging()` unpatched and then re-applies `configure_test_logging()` would otherwise inherit a factory pointing at a closed stream, raising `ValueError: I/O operation on closed file` on the next write. `stdout`, not `stderr`, to match the test suite's existing convention: `tests/test_jira_adapter.py::test_unmapped_status_logs_warning` already asserts a structlog warning lands on `capsys`'s `.out`, relying on structlog's unconfigured default (`PrintLoggerFactory()` with no `file`, which resolves to `stdout`). `tests/test_logging_rotation.py::TestTestLoggingConfiguration` guards the setting. Patch production `setup_logging()` (`sova/utils/logging.py`) out rather than calling it unpatched, as `test_dashboard.py` and `test_core.py` do: it re-runs `structlog.configure()` with its own default filter level (INFO) and leaves it there for the rest of the process, silently dropping every later debug event. A test that must call it for real has to call `configure_test_logging()` again in teardown, as `test_logging_rotation.py` does; a session-scoped fixture cannot reinstate process-global state on its own.

Because this fixture binds structlog to `stdout`, an in-process test cannot observe the process's real unconfigured default, which `sova/utils/logging.py` points at `stderr`. A test that must prove a log line stays off the stdout a script reads (for example `TestConfigGet.test_get_keeps_a_skipped_setting_warning_off_stdout`) runs the CLI in a subprocess with `PYTHONPATH` set to the repository root and asserts on the captured `stdout` and `stderr` separately.

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
    _shell_result(),  # second call
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

Build the client on the shared `dashboard_app` fixture rather than calling `create_app()` per test:

```python
@pytest.fixture
async def client(dashboard_app):
    async with AsyncClient(transport=ASGITransport(app=dashboard_app), base_url="http://test") as ac:
        ac.headers.update(csrf_request_headers(ac))  # only if the tests post to guarded routes
        yield ac
```

`tests/conftest.py` builds one `create_app(multi_project=False)` per test module (`_module_dashboard_app`), under the same cwd, home, env and keyring isolation the autouse fixtures give a test, and `dashboard_app` resets it around every test: it clears `dependency_overrides`, restores `app.state` to its built value, empties the rate limiter's counters, and points `control_service` and `handoff_service` at the test's working directory, as `create_app()` did. Building the app mounts every router and a Jinja environment that compiles each template on first render, which the shared app does once per module instead of once per test. `tests/test_dashboard_app_fixture.py` proves an override set in one test is gone in the next.

Call `create_app()` yourself only when the test needs an app built differently: an env var or config value read at build time (`SOVA_DASHBOARD_RATE_LIMIT_PER_MINUTE`, host, port), an explicit `project_dir`, or multi-project mode. Do not mutate the shared app in a way the reset does not undo (adding routes or middleware).

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

A script's own logic is tested in pytest against throwaway repositories; a scan of this repository's whole tree is not. `invariants/no-double-dash.sh --all` and `invariants/file-encoding.sh --all` run in `make lint-tree`, which `make lint` and CI's invariants job both run (#1191; inside pytest they cost 33 s of a local run). `invariants/structured-logging.sh` has no `--all` mode, so `tests/test_structured_logging.py` keeps its whole-tree test.

Real-git tests are dominated by process starts, which a Mac runs several times slower than a CI runner, so build each history once and copy it:

- `invariant_repo` is a copy of a session-scoped template (`InvariantRepo.copy_to()` copies the bare `origin` and the clone, and relinks the clone's `origin` URL), not a fresh `git init` per test.
- `invariant_history(dest, build, *args)` (module scope) runs `build(repo, *args)` once per module for each distinct `(build, args)` and returns a copy at `dest`, so parametrized cases that start from the same commits share one history (`tests/test_squash_file_set_check.py`, the `seeded` fixture in `tests/test_silent_revert_guard.py`). `build`'s return value is discarded: read a commit back from a ref.
- A read-only check that must behave the same in several shells runs each shell against one repository and compares the results, instead of parametrizing a test that rebuilds the history per shell (`Repo.run_check()` in `tests/test_integrate_pr_dropped_file_check.py`).
- A fake CLI that a polled bash block calls many times is a bash function, not a script: `tests/ci_wait_fakes.py` writes `gh` and `sleep` functions to a file the block's `bash -c` sources through `BASH_ENV`, keeps a failing `gh` on `PATH` behind them, and the tests cut the block's 16-poll budget to `TEST_POLL_BUDGET` with `shorten_poll_budget()`.

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
8. **Mock a sync function with `MagicMock`, never `AsyncMock`**: calling an `AsyncMock` returns a coroutine instead of running its `side_effect`, so an `AsyncMock(side_effect=ValueError(...))` patched onto a sync function such as `load_config` never raises there. The code under test fails later on the coroutine instead, and a "this error stays opaque" test passes without exercising the error it names (PR #1298; earlier, never-awaited warnings in PR #345). Add `mock.assert_called_once()` to an error-path test so a mock that never fired fails it.
