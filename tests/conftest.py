"""Shared pytest fixtures for the SOVA test suite.

The key fixture here is ``seed_config``, which seeds DB-backed project settings
for a test project directory. Production SOVA stores configuration in
``.claude/sova.db`` (the ``project_settings`` table), not in ``sova.toml``, so
tests that need configured values should seed the DB rather than writing a TOML
file. Writing ``sova.toml`` only works today because ``load_config()`` still has
an auto-migration fallback that epic #550 intends to remove. See issue #557.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
from sqlalchemy.pool import StaticPool

import sova.db.session as _dbs
from sova.db.models import Base
from sova.db.session import get_session, init_db

# Enables the ``pytester`` fixture: ``tests/test_shared_db_fixture.py`` uses it to
# drive a deterministic, isolated two-test pytest run in a subprocess.
pytest_plugins = ["pytester"]

# ``SOVA_DATABASE_URL`` is set deliberately by several test modules (at import
# time) to route ORM access to an in-memory SQLite DB, so it must survive the
# env-isolation fixture below.
_PRESERVED_ENV = frozenset({"SOVA_DATABASE_URL"})

# Paths a handful of tests still default to instead of ``tmp_path`` (see
# docs/testing-guidelines.md). Nothing should ever write through one of these
# literals: every exercised code path reaching a default is mocked. This
# records each path's mtime (or its absence) at session start and fails the
# run at session end if one changed or newly appeared, so a future test that
# stops mocking its collaborator and starts touching the shared path for real
# is caught immediately instead of silently corrupting another test's run.
#
# The snapshot covers each root *and every descendant*, because a write inside
# an already-existing directory (``/tmp/test/existing.txt`` when ``/tmp/test``
# is a pre-existing directory) leaves the root's own mtime untouched: comparing
# only the root would let exactly the write this guard exists to catch pass
# unnoticed.
_SHARED_TEST_PATHS = (Path("/tmp/test"), Path("/tmp/test-project"), Path("/tmp/worktree"))
_shared_path_snapshots: dict[Path, dict[str, float | str]] = {}

# Sentinel recorded for a path that does not exist, so "absent -> present" and
# "present -> absent" are both ordinary value changes rather than key churn.
_ABSENT = "absent"

# Cap on how many differing paths the session-end message names, so a root that
# unexpectedly holds a large tree cannot bury the failure in output.
_MAX_REPORTED_CHANGES = 20


def _snapshot_tree(root: Path) -> dict[str, float | str]:
    """Record ``root``'s mtime plus the mtime of every path beneath it."""
    snapshot: dict[str, float | str] = {}
    try:
        snapshot[str(root)] = root.stat().st_mtime
    except OSError:
        snapshot[str(root)] = _ABSENT
        return snapshot
    if not root.is_dir():
        return snapshot
    try:
        descendants = sorted(root.rglob("*"))
    except OSError:
        return snapshot
    for child in descendants:
        try:
            snapshot[str(child)] = child.stat().st_mtime
        except OSError:
            snapshot[str(child)] = _ABSENT
    return snapshot


def pytest_sessionstart(session: pytest.Session) -> None:
    for path in _SHARED_TEST_PATHS:
        _shared_path_snapshots[path] = _snapshot_tree(path)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    changed = []
    for root, before in _shared_path_snapshots.items():
        after = _snapshot_tree(root)
        for key in sorted(set(before) | set(after)):
            prev = before.get(key, _ABSENT)
            now = after.get(key, _ABSENT)
            if prev != now:
                changed.append(f"{key} (mtime {prev!r} -> {now!r})")
    if changed:
        reported = ", ".join(changed[:_MAX_REPORTED_CHANGES])
        if len(changed) > _MAX_REPORTED_CHANGES:
            reported += f", and {len(changed) - _MAX_REPORTED_CHANGES} more"
        print(
            "Shared /tmp test-default path(s) changed during this session, meaning "
            "some test wrote through a literal default instead of tmp_path: " + reported
        )
        if exitstatus == 0:
            session.exitstatus = 1


def configure_test_logging() -> None:
    """Render exceptions as plain tracebacks instead of rich-with-locals.

    ``tests/conftest.py`` never called ``structlog.configure()`` before this
    helper, so every ``log.debug(..., exc_info=True)`` call rendered through
    structlog's default chain, which leaves ``exc_info`` live for
    ``ConsoleRenderer`` to format with a ``RichTracebackFormatter`` (locals
    included) whenever ``rich`` is importable. That render costs about 0.43s
    per call on an M4 and more under coverage, and the suite hits it hundreds
    of times (see ``drain_feed_persist`` below for the most common trigger),
    accounting for roughly a third of CI test time.

    The chain below is production ``setup_logging()``'s own (see
    ``sova/utils/logging.py``) with colors forced off: ``format_exc_info``
    flattens the exception into a plain string before the renderer sees it,
    which is what makes the render cheap, so passing ``ConsoleRenderer`` an
    ``exception_formatter`` here would be dead config. Filtering stays at
    DEBUG (the unconfigured default lets every level through), so
    ``structlog.testing.capture_logs()`` still captures debug events: it swaps
    the configured processor list in place for its own ``LogCapture``,
    independent of whatever renderer sits ahead of it.

    ``cache_logger_on_first_use`` stays off, unlike production, which sets it:
    production configures once, whereas this helper is re-applied whenever a
    test perturbs the process-global config. Caching turns a lazy proxy into a
    bound logger on first use, freezing the wrapper class and the *identity* of
    the processor list it holds, and a later ``structlog.configure()`` installs
    a new list. Both the "swaps the list in place" guarantee above and the
    re-application below then silently stop applying to every logger already
    used: measured with structlog 26.1.0, ``capture_logs()`` sees 0 of 1 event
    from such a logger with caching on and 1 of 1 with it off. Caching buys
    nothing here either way (9 us per call measured with it both on and off).

    Exposed as a plain function, not only as the fixture below, because
    structlog's configuration is process-global and a session fixture cannot
    reinstate it: a test that calls production ``setup_logging()`` unpatched
    has to call this again afterwards (see ``tests/test_logging_rotation.py``).

    ``logger_factory`` is set explicitly, to a ``PrintLoggerFactory`` bound to
    whatever ``sys.stdout`` is live at call time, rather than left unset.
    ``sys.stdout`` (not ``stderr``, unlike production ``setup_logging()``)
    because ``tests/test_jira_adapter.py::test_unmapped_status_logs_warning``
    already asserts a structlog warning lands on ``capsys``'s ``.out``, which
    only holds with structlog's own unconfigured default
    (``PrintLoggerFactory()`` with no ``file``, which resolves to ``stdout``);
    switching the test suite's default to ``stderr`` to mirror production
    would silently break that assertion. Binding fresh at call time is what
    matters here: production binds its own ``PrintLoggerFactory`` to the
    ``sys.stderr`` object live when *it* runs, and pytest's output capture
    swaps in a fresh stream per test and closes the old one, so a test that
    calls production ``setup_logging()`` and then re-applies this helper
    (per the docstring above) would otherwise inherit that now-closed stream
    rather than falling back to a working default, and the next structlog
    write raises ``ValueError: I/O operation on closed file``.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )


@pytest.fixture(scope="session", autouse=True)
def _configure_test_logging() -> None:
    """Apply ``configure_test_logging()`` once, before the first test runs."""
    configure_test_logging()


async def drain_feed_persist() -> None:
    """Await any in-flight feed-persistence tasks before a DB teardown fixture closes it.

    ``FeedService.emit()`` (used via ``emit_safe()`` by ``start_agent``,
    ``start_batch``, and auto-handoff) schedules ``_persist()`` as a
    fire-and-forget background task. If a test's ``setup_db`` fixture calls
    ``close_db()`` while that task is still pending, the task's commit throws
    ("Cannot operate on a closed database") and logs ``feed.persist_failed``
    with ``exc_info=True``. Call this before ``close_db()`` in any DB teardown
    fixture whose test exercises feed events; a no-op when nothing is pending.
    """
    from sova.dashboard.services.feed_service import get_feed_service

    await get_feed_service().drain_persist()


@pytest.fixture(scope="session")
def _shared_test_db_engine():
    """Initialize one in-memory DB per xdist worker, reused across every migrated test module.

    ``create_async_engine()`` doesn't need a running loop, and empirically
    aiosqlite (current version) resolves the event loop per call rather than
    binding a connection to the loop that opened it, so a connection pooled
    here under ``asyncio.run()``'s throwaway loop is reused safely by each
    test's own per-test loop later. ``asyncio.run()`` is used (not an async
    fixture) specifically so this fixture can be session-scoped without
    requiring every consuming test to opt into a session-scoped event loop.

    ``init_db()``'s own pool (the default ``AsyncAdaptedQueuePool``, sized for
    production) opens a brand-new, separately-empty ``sqlite+aiosqlite://``
    connection on every checkout beyond the first: fine for the old per-test
    pattern (one engine per test, effectively one connection ever checked
    out), but this engine is now shared and some suites
    (``test_supervisor_api.py::test_concurrent_adds_no_duplicates``, which
    fires three concurrent requests via ``asyncio.gather`` against the same
    app) legitimately hold several concurrent sessions per test, each
    demanding its own checkout from the pool, exhausting the single
    already-schema'd connection and handing out "no such table" connections
    instead. Patching ``create_async_engine`` to force ``StaticPool`` only for
    this call keeps one physical connection (and therefore one schema) for
    every checkout, without touching ``sova/db/session.py``'s production pool
    choice.

    Consequence: every session opened against this engine within a migrated
    test shares that one physical connection, and therefore one transaction,
    with every other session opened in the same test (confirmed: a write made
    by one uncommitted session is visible to a second session opened in the
    same test, and that second session's ``commit()`` persists the first
    session's uncommitted write; a later ``rollback()`` on the first session
    cannot undo it). Production never shares a connection this way (it uses
    the default queue pool over a real file). A test for commit/rollback
    semantics specifically, or for "the failure path leaves no row behind",
    cannot be migrated onto this shared engine and still assert what it
    means to assert; give it its own engine (a ``tmp_path`` file DB via
    ``init_db()``/``close_db()``, as ``tests/test_db.py`` does) instead.

    Session-scoped per worker process, not global: in-memory SQLite is
    process-local, so pytest-xdist's default per-worker-process session scope
    already gives each worker its own isolated DB with no extra configuration.

    Not autouse: only the test modules that migrated off a local
    ``init_db``/``close_db`` fixture depend on this (see ``reset_db_rows``
    below), since forcing ``SOVA_DATABASE_URL`` onto every test in the suite
    would break tests that specifically assert default DB path fallback
    behavior with that variable unset.

    Returns the ``(engine, session_factory, url)`` this fixture created, not
    ``None``: ``reset_db_rows`` re-pins ``sova.db.session``'s globals to these
    exact objects before every test, because an unmigrated file elsewhere in
    the same xdist worker runs its own private ``init_db()``/``close_db()``
    per test (about 50 files still do, out of scope for this migration) and
    those calls overwrite the same module-level ``_engine``/``_session_factory``
    globals this fixture set up. Holding the objects here, rather than trusting
    the globals to still point at them later, is what survives that
    interleaving.

    One of those unmigrated files' own ``close_db()`` can still dispose this
    engine's actual connection outright, in the rare case its own entry in
    ``_engines`` still happens to be this engine when that call fires (e.g. a
    migrated test's ``reset_db_rows`` pinned it as current, then a background
    task or nested app lifespan calls ``close_db()`` before the next
    migrated test re-pins it): ``close_db()`` disposes *every* engine in that
    dict, not just the caller's own. For true in-memory SQLite that is not a
    "lost connection", it is a lost *database*: the next checkout silently
    opens a brand-new, schema-less one. ``reset_db_rows`` below treats a
    missing-table error on its own delete sweep as exactly that signal and
    recreates the schema rather than failing the test.
    """
    mp = pytest.MonkeyPatch()
    url = "sqlite+aiosqlite://"
    mp.setenv("SOVA_DATABASE_URL", url)
    mp.setattr(
        "sova.db.session.create_async_engine",
        lambda u, **kwargs: _create_async_engine(u, **{**kwargs, "poolclass": StaticPool}),
    )
    asyncio.run(init_db(run_migrations=False))

    engine, factory = _dbs._engine, _dbs._session_factory
    # Undo immediately rather than at session teardown: this monkeypatch is only
    # needed to get ``init_db()`` to build *this* engine. Left in place for the
    # rest of the session it would pin ``SOVA_DATABASE_URL`` in ``os.environ`` for
    # every other test in the worker too, since ``mp.undo()`` only runs at the
    # very end. ``_get_database_url()`` treats that env var as taking precedence
    # over an explicit ``project_dir``, so a test calling
    # ``get_session(project_dir=tmp_path)`` after this fixture first ran would
    # silently be redirected onto this shared in-memory DB instead of its own
    # project directory (observed in ``tests/test_settings_service.py``'s
    # per-project DB tests under a full-suite run). ``reset_db_rows`` below reasserts
    # the env var and the ``create_async_engine`` call is never needed again (the
    # same ``engine``/``factory`` objects are reused directly), so nothing here
    # depends on the patch staying active past this point.
    mp.undo()
    yield (engine, factory, url)
    asyncio.run(engine.dispose())


async def _delete_all_rows() -> None:
    """Empty every table on the current engine, in FK-safe order.

    Acquired through ``async with await get_session()`` rather than a bare
    ``session = await get_session()``: ``docs/database-guidelines.md`` documents
    the latter as the leaking pattern, and this runs once per test across the
    whole suite, so a session left unclosed on the error path would be the most
    frequently executed instance of it in the repository.
    """
    async with await get_session() as session, session.begin():
        for table in reversed(Base.metadata.sorted_tables):
            await session.execute(table.delete())


@pytest.fixture
async def reset_db_rows(_shared_test_db_engine: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every row from every table so each test starts with an empty, migrated schema.

    Thin wrapper around ``reset_shared_db_rows()``: the fixture form is what every
    migrated test module requests, and the plain-function form is what
    ``tests/test_shared_db_fixture.py`` calls directly to exercise this logic
    deterministically within a single test, since relying on two separate test
    items to land on the same pytest-xdist worker is not guaranteed under the
    default ``load`` distribution CI runs under (``-n auto`` with no ``--dist``
    flag): confirmed by running the row-isolation pair under ``-n 4`` with the
    delete sweep stubbed out, which still passed because the two tests landed on
    different workers, each with its own empty in-memory DB.

    Ordering hazard for file-specific seeding fixtures: this fixture is
    requested via ``pytest.mark.usefixtures("reset_db_rows")``, not
    ``autouse=True``, so pytest resolves it *after* any module- or
    class-level ``autouse=True`` fixture in the same file. A seeding fixture
    written the natural way, as ``@pytest.fixture(autouse=True)``, runs
    *before* this sweep and has its rows silently deleted before the test
    body ever sees them (confirmed: such a fixture inserting one ``TaskRun``
    leaves the test body with zero rows). The fix is to take this fixture as
    an explicit parameter even when the seeding fixture is itself autouse
    (``async def seed_rows(reset_db_rows): ...``), which forces pytest to
    resolve it first regardless of autouse ordering.
    """
    engine, factory, url = _shared_test_db_engine
    await reset_shared_db_rows(engine, factory, url, monkeypatch)


async def reset_shared_db_rows(engine: Any, factory: Any, url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every row from every table so each test starts with an empty, migrated schema.

    Row deletion, not engine disposal: the shared engine above is reused for
    the whole worker process, and an ``init_db(run_migrations=False)`` plus
    ``close_db()`` cycle per test measured 22.7ms against 3.2ms for a delete
    sweep over all 26 tables (40 iterations each, matching the 23.5ms/3.5ms
    figures in issue #1189). Runs before
    the test body, not after, so a test that errors before its own cleanup
    never leaves rows for the next test to trip over. Reverse
    ``Base.metadata.sorted_tables`` order matches the dependency order
    ``create_all`` uses, so a delete never hits a row another table's FK still
    points at (SQLite does not enforce FK constraints by default here, but the
    order is kept correct regardless).

    Re-pins ``sova.db.session``'s ``_engine``/``_session_factory``/``_engines``
    and ``os.environ["SOVA_DATABASE_URL"]`` before touching the DB: an
    unmigrated file's own ``init_db()``/``close_db()`` fixture, run earlier in
    the same worker, binds those same module globals to its own private engine
    and, at its teardown, both nulls them out and unconditionally
    ``os.environ.pop("SOVA_DATABASE_URL", None)``s the exact env var
    ``_shared_test_db_engine`` set once for the whole worker session. Once
    that var is gone, the next call to ``get_session()`` resolves a completely
    different (file-backed or home-fallback) database with no schema at all,
    which is what actually produced this fixture's "no such table" failures
    under full-suite xdist runs, not a lost connection. So both the globals
    and the env var are reasserted here, every test, rather than once.

    Sets the env var through the test's own ``monkeypatch`` fixture (the same
    instance injected into the test function, since ``monkeypatch`` is
    function-scoped and every consumer within one test shares it), not a raw
    ``os.environ`` assignment: a raw assignment plus a hand-rolled "restore
    the prior value" in a ``finally`` block is not safe when the test body
    *also* calls ``monkeypatch.setenv("SOVA_DATABASE_URL", ...)`` (e.g.
    ``tests/test_roles.py``'s env-scrubbing tests), because pytest does not
    guarantee that our fixture's teardown runs after that monkeypatch
    instance's own undo. When it runs first, the test's later undo restores
    to whatever this fixture had just set, permanently pinning the shared
    in-memory URL in ``os.environ`` for every later test in the worker
    (observed: it broke ``tests/test_settings_service.py``'s per-project DB
    tests under a full-suite run, since ``_get_database_url()`` treats that
    env var as taking precedence over an explicit ``project_dir``). Routing
    both mutations through the one shared ``monkeypatch`` instance's own undo
    stack makes the two sets unwind in the correct order regardless of which
    fixture's teardown pytest happens to run first.

    Re-pinning the globals and the env var is not sufficient on its own: an
    ``OperationalError: no such table`` on the delete sweep below, confirmed
    under full-suite xdist runs, means some unmigrated file's ``close_db()``
    disposed this *exact* engine object's one physical connection (not just
    reassigned the globals away from it) before this fixture re-pinned them.
    Re-pinning the Python objects cannot undo that: for ``sqlite+aiosqlite://``
    (true in-memory, no file), a disposed engine's next checkout silently
    opens a brand-new, schema-less in-memory database rather than erroring.
    Recreating the schema on that same engine, once, self-heals it: the next
    checkout lands on the same fresh connection the disposal already produced,
    and the delete sweep (which found nothing to delete anyway, since that
    connection starts empty) is retried against it. The retry is a real second
    attempt, not a bare ``except`` that assumes the first failure was harmless:
    an ``OperationalError`` from any other cause (a locked database, say) rolls
    the first sweep's deletes back, so swallowing it would hand the test a
    half-populated DB and a silently broken isolation guarantee. Letting the
    second attempt raise fails the test loudly instead.
    """
    monkeypatch.setenv("SOVA_DATABASE_URL", url)
    _dbs._engine = engine
    _dbs._session_factory = factory
    _dbs._engines[url] = (engine, factory)

    # No ``await drain_feed_persist()`` here: this runs as fixture *setup*, before
    # the test body, so it could only ever drain tasks left over from the
    # *previous* test, and pytest-asyncio 1.4.0's per-test ``asyncio.Runner.close()``
    # already cancels and drains any pending task at that test's teardown, so
    # there is nothing left to drain by the time this runs. A migrated test that
    # itself calls ``emit()``/``emit_safe()`` and then queries feed rows must
    # ``await drain_feed_persist()`` inside the test body, before the query.
    try:
        await _delete_all_rows()
    except OperationalError:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _delete_all_rows()


@pytest.fixture(autouse=True)
def _isolate_sova_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient ``SOVA_*`` config env vars so tests are deterministic.

    Pydantic Settings models (``TaskSourceConfig`` and friends) read
    ``SOVA_*`` env vars with higher precedence than DB/TOML/defaults. A
    developer shell that exports, e.g., ``SOVA_TASK_JIRA_API_TOKEN`` (Jira
    credentials loaded from the keychain) would otherwise leak into config
    construction and change assertions. Tests that need a specific env var set
    it explicitly via ``monkeypatch.setenv`` after this fixture runs.
    """
    for key in list(os.environ):
        if key.startswith("SOVA_") and key not in _PRESERVED_ENV:
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolate_provider_routing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient third-party provider-routing env vars and parent-session markers.

    A developer machine configured to route the Claude Code CLI through
    Vertex/Bedrock (``CLAUDE_CODE_USE_VERTEX``, ``ANTHROPIC_VERTEX_PROJECT_ID``,
    ``CLOUD_ML_REGION``, ...) exports these in every shell, including the one
    the test runner inherits. ``sova.llm.backends.detect_backend`` and
    ``sova.dashboard.services.setup_service``'s routing-warning check both read
    ``os.environ`` directly (by design: see their docstrings), so without this
    fixture a test asserting "no routing vars present" passes in CI but fails
    on exactly that kind of machine. ``PARENT_SESSION_VARS`` (``CLAUDECODE``,
    ``CLAUDE_CODE_SESSION_ID``, ``CLAUDE_CODE_MESSAGING_TOKEN``, ...) is scrubbed
    alongside it so a suite run from inside a Claude Code session matches CI.
    This is the sibling of ``_isolate_sova_env`` above, for env vars rather than
    ``SOVA_*`` config. Deliberately narrower than ``sova.utils.env.SCRUBBED_VARS``:
    that production constant also folds in ``CODEX_CREDENTIAL_VARS`` and
    ``OPENAI_CREDENTIAL_VARS``, which are real credentials a test may need to
    set and assert against, not routing/session noise to isolate from. Tests
    that need a specific var set it explicitly via ``monkeypatch.setenv`` after
    this fixture runs.
    """
    from sova.utils.env import PARENT_SESSION_VARS, PROVIDER_ROUTING_VARS

    for key in PROVIDER_ROUTING_VARS | PARENT_SESSION_VARS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolate_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Run each test from an empty directory so the repo's own config cannot leak.

    ``load_config(None)`` falls back to ``Path.cwd()``, which under pytest is the
    repository root, so any unscoped config load reads the developer's real
    ``.claude/sova.db``. A machine with, e.g., ``llm.routing.triage`` configured
    then fails assertions that assume an unrouted task type, while CI (which has
    no such file) stays green: the same tree passes in one place and fails in the
    other, and CI cannot catch it.

    This is the sibling of ``_isolate_sova_env`` above, one layer down: that
    fixture strips ambient ``SOVA_*`` env vars for the same reason. Chdir rather
    than patching ``load_config`` keeps production semantics intact, since a
    directory with no config source legitimately yields defaults.

    Tests that need a real project directory already pass ``tmp_path`` or an
    explicit path; none depend on the process cwd being the checkout (verified by
    running the full suite from an unrelated directory: identical collection,
    zero failures).
    """
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))


@pytest.fixture(autouse=True)
def _isolate_home_config(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Redirect ``~/.config/sova`` so no test can reach the developer's real global state.

    ``_get_database_url()`` (``sova/db/session.py``) falls back to
    ``Path.home() / ".config" / "sova" / "sova.db"`` whenever a test calls into DB
    code with no ``project_dir`` and no ``SOVA_DATABASE_URL``; a test that hits
    this path writes real ``task_runs``/``step_executions`` rows into the
    developer's actual database (observed: 655 polluting rows removed by hand on
    2026-10-13). ``sova.config.registry`` and ``sova.awareness.auth.google_oauth``
    have the same problem one layer worse: their ``_REGISTRY_DIR``/``_REGISTRY_FILE``
    and ``_CONFIG_DIR``/``_ALLOWED_ROOTS`` module constants are bound to the real
    ``Path.home()`` at import time, so patching ``Path.home`` after those modules
    have already been imported would not retroactively change them; they are
    monkeypatched directly here as well.

    This is the sibling of ``_isolate_cwd`` above, one layer up: that fixture
    redirects ``Path.cwd()`` fallbacks, this one redirects ``Path.home()``
    fallbacks. Function-scoped (one fake home per test, like ``_isolate_cwd``)
    rather than session-scoped, so one test's registry writes can't leak into
    another's.
    """
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    fake_config_dir = fake_home / ".config" / "sova"

    from sova.config import registry

    monkeypatch.setattr(registry, "_REGISTRY_DIR", fake_config_dir)
    monkeypatch.setattr(registry, "_REGISTRY_FILE", fake_config_dir / "projects.json")

    # ``_CONFIG_DIR``, ``_ALLOWED_ROOTS``, and the ``DEFAULT_TOKEN_PATH``/
    # ``DEFAULT_CREDENTIALS_PATH`` constants derived from it are all bound to
    # the real ``Path.home()`` at import time, so a test that forgets to pass
    # an explicit ``token_path``/``credentials_path`` would otherwise read or
    # write the developer's real Google OAuth token and client secrets under
    # ``~/.config/sova`` instead of hitting the ``_ALLOWED_ROOTS`` guard.
    from sova.awareness.auth import google_oauth

    monkeypatch.setattr(google_oauth, "_CONFIG_DIR", fake_config_dir)
    monkeypatch.setattr(google_oauth, "_ALLOWED_ROOTS", frozenset({fake_config_dir}))
    monkeypatch.setattr(google_oauth, "DEFAULT_TOKEN_PATH", fake_config_dir / "google_token.pickle")
    monkeypatch.setattr(google_oauth, "DEFAULT_CREDENTIALS_PATH", fake_config_dir / "google_credentials.json")


@pytest.fixture(autouse=True)
def _reset_connectivity_tracker() -> None:
    """Start every test with the network considered healthy.

    The tracker is a process-wide singleton that gates agent spawning, and it is
    fed by ``track_rate_limit`` from every ``gh`` call site. A test that
    simulates a transport failure twice would otherwise declare an outage for
    the rest of the session and block spawns in unrelated tests. Resetting also
    decouples the suite from the developer's actual connection, which the probe
    would otherwise record.
    """
    from sova.supervisor.network_health import reset

    reset()


@pytest.fixture(autouse=True)
def _reset_log_dedup() -> None:
    """Clear sova.utils.log_dedup state so one test's warning can't suppress another's.

    Production relies on process lifetime + TTL to bound repeated-warning
    noise; tests asserting on warning calls need a clean slate regardless of
    what an earlier test already logged with the same (event, key) pair.
    """
    from sova.utils.log_dedup import reset

    reset()


def _seed_project_settings(project_dir: Path, values: dict[str, Any]) -> None:
    """Upsert flattened, JSON-encoded config values into ``project_settings``.

    Creates the SQLite DB and schema under ``{project_dir}/.claude/sova.db`` if
    absent. Nested dicts are flattened to dot-notation keys; flat dotted keys
    pass through unchanged. Purely synchronous so it works with or without a
    running event loop.
    """
    from sqlalchemy import create_engine

    from sova.config.db_loader import _flatten_config_dict, _write_flat_config
    from sova.db.models import Base

    flat = _flatten_config_dict(values)

    db_path = project_dir / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            _write_flat_config(conn, values, flat)
    finally:
        engine.dispose()


@pytest.fixture
def seed_config():
    """Seed DB-backed project settings so ``load_config`` reads them.

    Returns a callable ``seed(project_dir, values=None, **kwargs)`` that writes
    ``ProjectSetting`` rows into ``{project_dir}/.claude/sova.db``, mirroring
    production (config lives in the DB, not sova.toml). This replaces the legacy
    test pattern of writing a ``sova.toml`` file.

    Values may be provided as a mapping and/or keyword arguments. Nested dicts
    are flattened to dot-notation (``{"pipeline": {"auto_handoff": False}}`` ->
    ``pipeline.auto_handoff``); flat dotted keys (``{"pipeline.auto_handoff":
    False}``) are accepted as-is. Values are JSON-encoded to match production
    storage, so native Python types (str, int, float, bool, list) are passed
    naturally at call sites.

    Example::

        def test_something(tmp_path, seed_config):
            seed_config(tmp_path, github_repo="user/repo", pipeline={"auto_handoff": False})
            cfg = load_config(tmp_path)
            assert cfg.github_repo == "user/repo"

    Calling it with no values still creates the DB and ``project_settings``
    table, matching an empty (installed but unconfigured) project.

    Note: ``SOVA_*`` environment variables still take precedence over seeded
    values (env > DB > TOML > defaults), matching production config priority.
    """

    def _seed(project_dir: Path | str, values: dict[str, Any] | None = None, **kwargs: Any) -> None:
        merged: dict[str, Any] = {}
        if values:
            merged.update(values)
        merged.update(kwargs)
        _seed_project_settings(Path(project_dir), merged)

    return _seed


def csrf_request_headers(client: Any) -> dict[str, str]:
    """Build a legitimate double-submit CSRF pair for a require_same_origin_csrf route.

    ``create_app()`` builds ``app.state.allowed_origins`` from the default
    host/port (127.0.0.1:8111), not from the test transport's base_url
    ("http://test"), so the Origin header must match that default bind rather
    than the client's own base_url. Sets the cookie on *client* (httpx persists
    cookies across requests on the same client) and returns the matching
    Origin + X-CSRF-Token headers.

    Shared here rather than copied per test module: three dashboard test files
    need the same pair, and SonarCloud's new-code duplication gate counts
    near-identical helper bodies.
    """
    from sova.dashboard.security import CSRF_COOKIE_NAME, CSRF_HEADER_NAME

    token = "test-csrf-token"
    client.cookies.set(CSRF_COOKIE_NAME, token)
    return {"origin": "http://localhost:8111", CSRF_HEADER_NAME: token}


INVARIANTS_DIR = Path(__file__).parent.parent / "invariants"

DASHBOARD_DIR = Path(__file__).parent.parent / "sova" / "dashboard"
AGENTS_PAGE_JS_DIR = DASHBOARD_DIR / "static" / "js" / "pages" / "agents"


@functools.cache
def agents_page_source() -> str:
    """The Agents page markup followed by every ES module it loads, as one string.

    The page's script lives in `static/js/pages/agents/*.js`, not inline in `agents.html`,
    so a source-level assertion about its JS (there is no JS test harness) searches this.
    Cached: the files do not change during a run and a test may call it many times. Assert on
    markup (an element id) against the rendered response instead, which this string is not.
    """
    parts = [(DASHBOARD_DIR / "templates" / "agents.html").read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in sorted(AGENTS_PAGE_JS_DIR.glob("*.js"))]
    return "\n".join(parts)


# Default date for commits that stand for settled history, well outside any
# recency window an invariant applies.
INVARIANT_REPO_EPOCH = "2020-01-01T12:00:00+00:00"


class InvariantRepo:
    """A throwaway clone of a bare ``origin``, with ``main`` pushed so ``origin/main`` exists.

    The scripts under ``invariants/`` judge a branch against ``origin/main``, so
    their tests need a real remote-tracking ref rather than a single local repo.
    Global and system git config are ignored, so a developer's signing or hook
    settings cannot change the outcome. Shared here rather than copied per test
    module: every invariant test needs the same setup, and SonarCloud's new-code
    duplication gate counts near-identical helper bodies.
    """

    def __init__(self, root: Path) -> None:
        self.origin = root / "origin.git"
        self.dir = root / "work"
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        self.git("init", "-q", "--bare", "--initial-branch=main", str(self.origin), cwd=root)
        self.git("clone", "-q", str(self.origin), str(self.dir), cwd=root)
        self.git("checkout", "-q", "-b", "main")
        self.write("README.md", "# project\n")
        self.land_on_main("chore(docs): init")

    def git(self, *args: str, cwd: Path | None = None, when: str | None = None) -> str:
        env = dict(self.env)
        if when:
            env["GIT_AUTHOR_DATE"] = when
            env["GIT_COMMITTER_DATE"] = when
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.dir, env=env, capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    def write(self, path: str, content: str) -> None:
        target = self.dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self, message: str, when: str | None = None) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, when=when)
        return self.git("rev-parse", "HEAD")

    def land_on_main(self, message: str, when: str | None = INVARIANT_REPO_EPOCH) -> str:
        """Commit on main and push it, as if another PR had just merged."""
        sha = self.commit(message, when=when)
        self.git("push", "-q", "origin", "main")
        return sha

    def start_branch(self, name: str = "feat/work") -> None:
        self.git("checkout", "-q", "-b", name)

    def run(self, script: str, **env: str) -> subprocess.CompletedProcess[str]:
        """Run ``invariants/<script>`` against this repo, as the pre-push hook does."""
        return subprocess.run(
            ["bash", str(INVARIANTS_DIR / script), str(self.dir), "main"],
            env={**self.env, **env},
            capture_output=True,
            text=True,
        )


@pytest.fixture
def invariant_repo(tmp_path: Path) -> InvariantRepo:
    return InvariantRepo(tmp_path)
