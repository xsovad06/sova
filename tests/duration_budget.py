"""Fail a test whose own setup, call and teardown time exceeds ``SOVA_TEST_DURATION_BUDGET`` seconds.

Off unless the variable is set: CI sets it (``.github/workflows/ci.yml``), a local
run does not, since a laptop's timings swing with whatever else it runs. A few
hundred process-heavy tests took most of the suite's CPU time (issue #1191), and
the budget keeps a new one from joining them unnoticed. A test that is slow by
nature says so with ``@pytest.mark.slow(reason="...")``, which exempts it from the
budget. A ``slow`` marker without a reason fails the test everywhere, budget or not.

What counts is the test's own cost, so a failure does not depend on which test xdist
happened to schedule first:

- Setup excludes fixtures wider than ``function`` scope (a per-module app, a session DB
  template): the first test on a worker would otherwise pay for all of them.
- Teardown is counted only when the next test shares this test's parent (module or class),
  because otherwise the report also holds the wider fixtures' teardown. The last test of a
  module therefore escapes the teardown half of the budget.

Loaded from ``tests/conftest.py``'s ``pytest_plugins``. The budget is read once
at configure time, not per test: the autouse ``_isolate_sova_env`` fixture strips
every ``SOVA_*`` variable while a test runs.
"""

from __future__ import annotations

import os
import time
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

import pytest

BUDGET_ENV = "SOVA_TEST_DURATION_BUDGET"

_budget_key = pytest.StashKey[float | None]()


@dataclass
class _Timings:
    """One item's phase durations, in seconds, plus whether every phase so far passed."""

    shared_setup: float = 0.0
    setup: float = 0.0
    call: float = 0.0
    passed: bool = True
    teardown_counts: bool = False


_timings_key = pytest.StashKey[_Timings]()
_active_item: pytest.Item | None = None


def _parse_budget(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        budget = float(raw)
    except ValueError:
        raise pytest.UsageError(f"{BUDGET_ENV} must be a number of seconds, got {raw!r}") from None
    if budget <= 0:
        raise pytest.UsageError(f"{BUDGET_ENV} must be positive, got {raw!r}")
    return budget


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_budget_key] = _parse_budget(os.environ.get(BUDGET_ENV))


def _slow_reason(item: pytest.Item) -> str | None:
    """The ``slow`` marker's reason, ``""`` for a marker without one, or None when unmarked."""
    marker = item.get_closest_marker("slow")
    if marker is None:
        return None
    reason = marker.kwargs.get("reason", marker.args[0] if marker.args else "")
    return reason.strip() if isinstance(reason, str) else ""


def _timings_of(item: pytest.Item) -> _Timings:
    return item.stash.setdefault(_timings_key, _Timings())


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None, None, None]:
    global _active_item  # noqa: PLW0603 (the fixture hook receives a request, not the running item)
    _active_item = item
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef: Any, request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """Time the fixtures wider than function scope, so setup can leave them out."""
    start = time.perf_counter()
    yield
    item = _active_item
    if item is not None and fixturedef.scope != "function":
        _timings_of(item).shared_setup += time.perf_counter() - start


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, None, None]:
    global _active_item  # noqa: PLW0603 (see pytest_runtest_setup)
    _active_item = None
    # Only function-scoped fixtures are finalized when the next test stays in this item's module or class.
    _timings_of(item).teardown_counts = nextitem is not None and item.parent in nextitem.listchain()
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None, None, None]:
    outcome = yield
    report: pytest.TestReport = outcome.get_result()
    timings = _timings_of(item)
    if report.when == "setup":
        timings.setup = max(0.0, report.duration - timings.shared_setup)
        timings.passed = report.passed
        return
    if report.when == "call":
        timings.call = report.duration
        timings.passed = timings.passed and report.passed
        if report.passed and _slow_reason(item) == "":
            report.outcome = "failed"
            report.longrepr = f"{item.nodeid}: @pytest.mark.slow needs a reason, e.g. @pytest.mark.slow(reason='...')"
        return
    budget = item.config.stash.get(_budget_key, None)
    if budget is None or not report.passed or not timings.passed or _slow_reason(item) is not None:
        return
    teardown = report.duration if timings.teardown_counts else 0.0
    total = timings.setup + timings.call + teardown
    if total > budget:
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid} took {total:.2f}s (setup {timings.setup:.2f}s + call {timings.call:.2f}s + "
            f"teardown {teardown:.2f}s), over {BUDGET_ENV}={budget:g}s. Setup leaves out module and session "
            "fixtures. Make it faster, or mark it @pytest.mark.slow(reason='...') "
            'if it is slow by nature (docs/testing-guidelines.md, "Duration Budget").'
        )
