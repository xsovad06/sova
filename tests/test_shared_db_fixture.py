"""Guards for ``tests/conftest.py``'s shared per-worker in-memory DB fixture.

Issue #1189 replaced the per-test ``init_db()``/``close_db()`` pattern with one
session-scoped engine plus a per-test row-delete sweep (``reset_db_rows``). The
whole point of that trade is that a test still sees empty tables, so the
isolation guarantee needs its own test: a bug in the sweep would otherwise
surface only as a confusing, order-dependent failure in some unrelated module.

``TestRowIsolationBetweenTests`` below drives a real, separate two-test pytest
run via the ``pytester`` fixture, in a subprocess, rather than splitting
"seed" and "assert" across two test items in *this* file: two items in this
file are not guaranteed to land on the same pytest-xdist worker under the
``load`` distribution CI runs with (``-n auto``, no ``--dist`` flag), and a
previous version of this guard that called the sweep inline inside one test
body passed even with the delete sweep stubbed into a no-op (confirmed by
patching it out and running `pytest tests/test_shared_db_fixture.py -q`: still
4 passed), because it never actually observed the sweep running *between*
two tests. The subprocess run sidesteps worker assignment by construction
(the generated two-test module always runs as a single, unparallelized
pytest invocation) while still asserting the real cross-test behavior.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

import sova.db.session as _dbs
from sova.db.models import Base, TaskRun
from sova.db.session import get_session
from tests.conftest import reset_shared_db_rows

pytestmark = pytest.mark.usefixtures("reset_db_rows")

_ISSUE = "991189"


async def _task_run_count() -> int:
    async with await get_session() as session:
        result = await session.execute(select(func.count()).select_from(TaskRun))
        return result.scalar_one()


class TestRowIsolationBetweenTests:
    def test_fixture_is_actually_in_the_dependency_closure(self, request: pytest.FixtureRequest) -> None:
        """Catches the trivial regression: someone deletes the ``pytestmark`` line above.

        That mistake would make every other test in this module run against an
        unswept, globally-shared DB instead of failing loudly, so it gets its own
        explicit assertion rather than relying on the subprocess run to notice.
        """
        assert "reset_db_rows" in request.fixturenames

    def test_sweep_removes_a_row_inserted_between_tests(
        self, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive a real two-test module through pytest, in a subprocess, to prove the sweep runs.

        ``test_a`` inserts a row and leaves it uncommitted to the next test item;
        ``test_b`` asserts the table is empty again. Both import ``tests.conftest``
        as a plugin (via ``pytest_plugins``) so they get the real
        ``_shared_test_db_engine``/``reset_db_rows`` fixtures, not a reimplementation
        of them. If ``reset_db_rows``'s delete sweep were a no-op, ``test_b`` would
        see ``test_a``'s row and fail.

        ``pytester`` points ``HOME`` at its own throwaway directory for isolation,
        which breaks discovery of anything installed into user site-packages
        (this project's own dev install included) for the subprocess it spawns:
        confirmed by probing ``sys.path`` inside that subprocess, which was
        missing the user site-packages directory present in this process's own
        ``sys.path``. Explicitly forwarding this process's own ``sys.path`` (which
        already has it, since this test module itself imports project code) as
        the subprocess's ``PYTHONPATH`` sidesteps that rather than depending on
        ``HOME``.
        """
        repo_root = Path(__file__).resolve().parent.parent
        pytester.syspathinsert(repo_root)
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in sys.path if p))
        pytester.makeini(
            """
            [pytest]
            asyncio_mode = auto
            """
        )
        pytester.makepyfile(
            test_two_test_isolation="""
            from __future__ import annotations

            import pytest
            from sqlalchemy import func, select

            from sova.db.models import TaskRun
            from sova.db.session import get_session

            pytest_plugins = ["tests.conftest"]
            pytestmark = pytest.mark.usefixtures("reset_db_rows")


            async def test_a_inserts_a_row() -> None:
                async with await get_session() as session, session.begin():
                    session.add(TaskRun(issue_number="991189", role="developer", status="running"))


            async def test_b_sees_an_empty_table() -> None:
                async with await get_session() as session:
                    result = await session.execute(select(func.count()).select_from(TaskRun))
                    assert result.scalar_one() == 0
            """
        )
        result = pytester.runpytest_subprocess()
        result.assert_outcomes(passed=2)


class TestSchemaIsUsable:
    async def test_every_orm_table_exists(self) -> None:
        """The sweep itself would mask a missing table only if that table were never queried."""
        async with await get_session() as session:
            for table in Base.metadata.sorted_tables:
                result = await session.execute(select(func.count()).select_from(table))
                assert result.scalar_one() == 0, f"{table.name} is not empty at test start"

    async def test_session_globals_point_at_the_shared_engine(self) -> None:
        """``reset_db_rows`` re-pins these every test; an unmigrated module's teardown unsets them."""
        assert _dbs._engine is not None
        assert _dbs._session_factory is not None
        assert _dbs._engines["sqlite+aiosqlite://"] == (_dbs._engine, _dbs._session_factory)


class TestSelfHealAfterEngineDisposal:
    async def test_disposal_mid_test_is_recovered_by_the_next_sweep(
        self, _shared_test_db_engine: tuple, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dispose the shared engine mid-test, the way an unmigrated module's ``close_db()`` does.

        For ``sqlite+aiosqlite://`` that discards the database itself, so the next
        checkout opens a fresh, schema-less one. ``reset_shared_db_rows()``'s
        ``OperationalError`` branch must recreate the schema and retry the sweep
        rather than raising.
        """
        engine, factory, url = _shared_test_db_engine
        await engine.dispose()

        await reset_shared_db_rows(engine, factory, url, monkeypatch)

        assert await _task_run_count() == 0
