"""Guards for ``tests/conftest.py``'s module-scoped ``dashboard_app`` fixture.

The app is built once per test module and reused, so whatever one test changes on it
must be undone before the next. Two tests in this file are not guaranteed to share a
pytest-xdist worker (see ``tests/test_shared_db_fixture.py``), so the cross-test check
runs a generated two-test module in a subprocess, where they always run in order.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def test_an_override_set_in_one_test_is_gone_in_the_next(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    pytester.syspathinsert(repo_root)
    # pytester points HOME at a throwaway directory, which hides user site-packages from
    # the subprocess; forwarding this process's sys.path restores them.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in sys.path if p))
    pytester.makeini(
        """
        [pytest]
        asyncio_mode = auto
        """
    )
    pytester.makepyfile(
        test_two_tests_share_an_app="""
        from __future__ import annotations

        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.routers.fleet_insights import _get_fleet_service

        pytest_plugins = ["tests.conftest"]
        seen = []


        async def test_a_overrides_a_dependency_and_state(dashboard_app) -> None:
            seen.append(dashboard_app)
            dashboard_app.dependency_overrides[_get_fleet_service] = lambda: None
            dashboard_app.state.is_loopback_bind = False
            dashboard_app.state.added_by_test_a = True


        async def test_b_sees_neither(dashboard_app) -> None:
            assert dashboard_app is seen[0], "the app is rebuilt per test instead of shared per module"
            assert dashboard_app.dependency_overrides == {}
            assert dashboard_app.state.is_loopback_bind is True
            assert not hasattr(dashboard_app.state, "added_by_test_a")
            async with AsyncClient(transport=ASGITransport(app=dashboard_app), base_url="http://test") as client:
                assert (await client.get("/api/health/connectivity")).status_code == 200
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=2)
