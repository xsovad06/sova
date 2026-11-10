"""Tests for tests/duration_budget.py, run in-process through ``pytester``."""

from __future__ import annotations

import pytest

from tests.duration_budget import BUDGET_ENV

_MODULE = """
import time

import pytest


def test_fast():
    pass


def test_over_budget():
    time.sleep(0.15)


@pytest.fixture
def slow_fixture():
    time.sleep(0.08)


def test_setup_counts(slow_fixture):
    time.sleep(0.05)


@pytest.mark.slow(reason="waits on a real timer")
def test_marked_slow():
    time.sleep(0.15)


@pytest.mark.slow
def test_marked_without_reason():
    pass


@pytest.fixture
def slow_teardown():
    yield
    time.sleep(0.15)


def test_teardown_counts(slow_teardown):
    pass


@pytest.fixture(scope="module")
def shared_fixture():
    time.sleep(0.3)


def test_shared_setup_is_not_counted(shared_fixture):
    pass
"""


def _run(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, budget: str | None) -> pytest.RunResult:
    if budget is None:
        monkeypatch.delenv(BUDGET_ENV, raising=False)
    else:
        monkeypatch.setenv(BUDGET_ENV, budget)
    pytester.makepyfile(test_budget=_MODULE)
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\nmarkers =\n    slow: slow by nature\n")
    return pytester.runpytest_inprocess("-p", "tests.duration_budget", "-p", "no:cacheprovider", "-p", "no:xdist")


class TestDurationBudget:
    def test_unset_budget_fails_no_slow_test_but_still_a_reasonless_marker(
        self, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _run(pytester, monkeypatch, None)
        result.assert_outcomes(passed=6, failed=1)
        result.stdout.fnmatch_lines(["*test_marked_without_reason: @pytest.mark.slow needs a reason*"])

    def test_budget_fails_slow_tests_with_their_duration(
        self, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _run(pytester, monkeypatch, "0.1")

        # An over-budget test is reported as an error at teardown, the only point its whole cost is known.
        # test_setup_counts fails on setup (0.08) plus call (0.05) though neither alone is over 0.1.
        # test_teardown_counts is followed by a test in the same module, so its teardown is measured.
        # The three over-budget calls pass (hence passed=6); only their teardown errors.
        result.assert_outcomes(passed=6, failed=1, errors=3)
        result.stdout.fnmatch_lines(
            [
                "*test_over_budget took *s (setup *s + call *s + teardown *s), over SOVA_TEST_DURATION_BUDGET=0.1s*",
                "*test_setup_counts took *s (setup *s + call *s + teardown *s)*",
                "*test_teardown_counts took *s (setup *s + call *s + teardown *s)*",
                "*test_marked_without_reason: @pytest.mark.slow needs a reason*",
            ]
        )
        out = result.stdout.str()
        assert "test_marked_slow took" not in out
        assert "test_shared_setup_is_not_counted took" not in out

    @pytest.mark.parametrize("value", ["soon", "0", "-1"])
    def test_invalid_budget_is_a_usage_error(
        self, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        result = _run(pytester, monkeypatch, value)
        assert result.ret == pytest.ExitCode.USAGE_ERROR
        result.stderr.fnmatch_lines([f"*{BUDGET_ENV} must be*"])


def test_the_suite_loads_the_budget_plugin(request: pytest.FixtureRequest) -> None:
    assert request.config.pluginmanager.has_plugin("tests.duration_budget")
