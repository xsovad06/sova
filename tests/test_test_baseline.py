"""Tests for sova.core.test_baseline and CaptureBaselineStep."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.core.test_baseline import (
    BaselineSnapshot,
    RegressionReport,
    SingleTestOutcome,
    baseline_path,
    diff_results,
    load_baseline,
    run_test_suite,
    save_baseline,
)
from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _mock_adapter() -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_state.return_value = TaskState.RESEARCHED
    adapter.get_task.return_value = Task(id="1", title="Test issue")
    return adapter


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


# -- BaselineSnapshot serialization --


class TestBaselineSnapshot:
    def test_round_trip(self) -> None:
        snapshot = BaselineSnapshot(
            mode="per_test",
            exit_code=0,
            tests=[
                SingleTestOutcome(nodeid="tests/test_foo.py::test_bar", outcome="passed"),
                SingleTestOutcome(nodeid="tests/test_foo.py::test_baz", outcome="failed"),
            ],
        )
        data = snapshot.to_dict()
        restored = BaselineSnapshot.from_dict(data)
        assert restored.mode == "per_test"
        assert restored.exit_code == 0
        assert len(restored.tests) == 2
        assert restored.tests[0].nodeid == "tests/test_foo.py::test_bar"

    def test_empty_tests(self) -> None:
        snapshot = BaselineSnapshot(mode="exit_code", exit_code=1, tests=[])
        data = snapshot.to_dict()
        restored = BaselineSnapshot.from_dict(data)
        assert restored.tests == []
        assert restored.mode == "exit_code"


# -- save / load --


class TestSaveLoad:
    def test_save_and_load(self, tmp_path: Path) -> None:
        snapshot = BaselineSnapshot(
            mode="per_test",
            exit_code=0,
            tests=[SingleTestOutcome(nodeid="test_a", outcome="passed")],
        )
        save_baseline(snapshot, tmp_path)
        loaded = load_baseline(tmp_path)
        assert loaded is not None
        assert loaded.mode == "per_test"
        assert len(loaded.tests) == 1

    def test_load_missing(self, tmp_path: Path) -> None:
        result = load_baseline(tmp_path)
        assert result is None

    def test_load_corrupt(self, tmp_path: Path) -> None:
        path = baseline_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("not json")
        result = load_baseline(tmp_path)
        assert result is None

    def test_baseline_path(self, tmp_path: Path) -> None:
        p = baseline_path(tmp_path)
        assert p == tmp_path / ".sova" / "test-baseline.json"


# -- diff_results --


class TestDiffResults:
    def test_regression_detected(self) -> None:
        baseline = BaselineSnapshot(
            mode="per_test",
            exit_code=0,
            tests=[SingleTestOutcome(nodeid="test_a", outcome="passed")],
        )
        current = BaselineSnapshot(
            mode="per_test",
            exit_code=1,
            tests=[SingleTestOutcome(nodeid="test_a", outcome="failed")],
        )
        report = diff_results(baseline, current)
        assert report.has_regressions
        assert len(report.regressions) == 1
        assert report.regressions[0].nodeid == "test_a"

    def test_fixed_test(self) -> None:
        baseline = BaselineSnapshot(
            mode="per_test",
            exit_code=1,
            tests=[SingleTestOutcome(nodeid="test_a", outcome="failed")],
        )
        current = BaselineSnapshot(
            mode="per_test",
            exit_code=0,
            tests=[SingleTestOutcome(nodeid="test_a", outcome="passed")],
        )
        report = diff_results(baseline, current)
        assert not report.has_regressions
        assert len(report.fixed) == 1

    def test_new_failure(self) -> None:
        baseline = BaselineSnapshot(mode="per_test", exit_code=0, tests=[])
        current = BaselineSnapshot(
            mode="per_test",
            exit_code=1,
            tests=[SingleTestOutcome(nodeid="test_new", outcome="failed")],
        )
        report = diff_results(baseline, current)
        assert not report.has_regressions
        assert len(report.new_failures) == 1

    def test_pre_existing_failure_not_regression(self) -> None:
        baseline = BaselineSnapshot(
            mode="per_test",
            exit_code=1,
            tests=[SingleTestOutcome(nodeid="test_a", outcome="failed")],
        )
        current = BaselineSnapshot(
            mode="per_test",
            exit_code=1,
            tests=[SingleTestOutcome(nodeid="test_a", outcome="failed")],
        )
        report = diff_results(baseline, current)
        assert not report.has_regressions
        assert len(report.fixed) == 0

    def test_exit_code_mode_returns_empty(self) -> None:
        baseline = BaselineSnapshot(mode="exit_code", exit_code=0, tests=[])
        current = BaselineSnapshot(mode="exit_code", exit_code=1, tests=[])
        report = diff_results(baseline, current)
        assert not report.has_regressions
        assert len(report.regressions) == 0

    def test_mixed_modes_returns_empty(self) -> None:
        baseline = BaselineSnapshot(
            mode="per_test",
            exit_code=0,
            tests=[SingleTestOutcome(nodeid="test_a", outcome="passed")],
        )
        current = BaselineSnapshot(mode="exit_code", exit_code=1, tests=[])
        report = diff_results(baseline, current)
        assert not report.has_regressions

    def test_no_tests_no_regressions(self) -> None:
        baseline = BaselineSnapshot(mode="per_test", exit_code=0, tests=[])
        current = BaselineSnapshot(mode="per_test", exit_code=0, tests=[])
        report = diff_results(baseline, current)
        assert not report.has_regressions
        assert report.summary() == "no regressions"


class TestRegressionReport:
    def test_summary_with_all(self) -> None:
        report = RegressionReport(
            regressions=[SingleTestOutcome(nodeid="r", outcome="failed")],
            fixed=[SingleTestOutcome(nodeid="f", outcome="passed")],
            new_failures=[SingleTestOutcome(nodeid="n", outcome="failed")],
        )
        s = report.summary()
        assert "1 regression(s)" in s
        assert "1 fixed" in s
        assert "1 new failure(s)" in s


# -- run_test_suite --


class TestRunTestSuite:
    async def test_invalid_cwd_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid cwd"):
            await run_test_suite("pytest", Path("/nonexistent/path"), cmd_timeout=60)

    async def test_none_cwd_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid cwd"):
            await run_test_suite("pytest", None, cmd_timeout=60)  # type: ignore[arg-type]

    async def test_non_pytest_uses_exit_code_mode(self) -> None:
        with patch("sova.core.test_baseline.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, success=True, stdout="", stderr="")
            result = await run_test_suite("npm test", Path("/tmp"), cmd_timeout=60)
        assert result.mode == "exit_code"
        assert result.exit_code == 0
        assert result.tests == []

    async def test_pytest_with_json_report(self, tmp_path: Path) -> None:
        sova_dir = tmp_path / ".sova"
        sova_dir.mkdir()
        report_data = {
            "tests": [
                {"nodeid": "test_a::test_1", "outcome": "passed"},
                {"nodeid": "test_a::test_2", "outcome": "failed"},
            ]
        }

        async def mock_run_side_effect(*args, **kwargs):
            # Write the report file as a side effect
            report_path = sova_dir / ".test-report.json"
            report_path.write_text(json.dumps(report_data))
            return MagicMock(returncode=1, success=False, stdout="", stderr="")

        with patch("sova.core.test_baseline.run", side_effect=mock_run_side_effect):
            result = await run_test_suite("pytest", tmp_path, cmd_timeout=60)

        assert result.mode == "per_test"
        assert len(result.tests) == 2
        assert result.exit_code == 1

    async def test_pytest_fallback_when_report_corrupt(self, tmp_path: Path) -> None:
        sova_dir = tmp_path / ".sova"
        sova_dir.mkdir()
        call_count = 0

        async def mock_run_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: enriched command writes corrupt report
                report_path = sova_dir / ".test-report.json"
                report_path.write_text("not valid json{{{")
                return MagicMock(returncode=4, success=False, stdout="", stderr="")
            # Second call: clean fallback command
            return MagicMock(returncode=1, success=False, stdout="", stderr="")

        with patch("sova.core.test_baseline.run", side_effect=mock_run_side_effect):
            result = await run_test_suite("pytest", tmp_path, cmd_timeout=60)

        assert result.mode == "exit_code"
        assert result.exit_code == 1
        assert call_count == 2  # enriched + fallback

    async def test_pytest_fallback_when_no_report(self, tmp_path: Path) -> None:
        call_count = 0

        async def mock_run_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: enriched command (json-report plugin missing)
                return MagicMock(returncode=4, success=False, stdout="", stderr="")
            # Second call: clean fallback
            return MagicMock(returncode=0, success=True, stdout="", stderr="")

        with patch("sova.core.test_baseline.run", side_effect=mock_run_side_effect):
            result = await run_test_suite("pytest", tmp_path, cmd_timeout=60)
        # Falls back to exit_code mode with clean command's exit code
        assert result.mode == "exit_code"
        assert result.exit_code == 0
        assert call_count == 2


# -- CaptureBaselineStep --


class TestCaptureBaselineStep:
    async def test_execute_captures_baseline(self, tmp_path: Path) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        ctx = _make_ctx(worktree_dir=worktree)

        with patch("sova.core.steps.capture_baseline.run_test_suite", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = BaselineSnapshot(
                mode="per_test", exit_code=0, tests=[SingleTestOutcome(nodeid="t1", outcome="passed")]
            )
            result = await CaptureBaselineStep().execute(ctx)

        assert result.success
        assert "per-test" in result.summary
        assert "1 tests" in result.summary
        assert ctx.test_baseline_path is not None
        assert ctx.test_baseline_path.exists()

    async def test_disabled_via_config(self) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        config = ProjectConfig(testing={"baseline_enabled": False})
        ctx = _make_ctx(config=config, worktree_dir=Path("/tmp/wt"))
        result = await CaptureBaselineStep().execute(ctx)
        assert result.success
        assert "disabled" in result.summary

    async def test_no_test_cmd(self) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        config = ProjectConfig(test_cmd="")
        ctx = _make_ctx(config=config, worktree_dir=Path("/tmp/wt"))
        result = await CaptureBaselineStep().execute(ctx)
        assert result.success
        assert "no test command" in result.summary.lower()

    async def test_no_worktree(self) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        ctx = _make_ctx(worktree_dir=None)
        result = await CaptureBaselineStep().execute(ctx)
        assert result.success
        assert "no worktree" in result.summary.lower()

    async def test_capture_failure_non_fatal(self) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/wt"))
        with patch("sova.core.steps.capture_baseline.run_test_suite", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = RuntimeError("timeout")
            result = await CaptureBaselineStep().execute(ctx)

        assert result.success
        assert "non-fatal" in result.summary.lower()
        assert ctx.test_baseline_path is None

    async def test_validate_output_always_passes(self) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        ctx = _make_ctx()
        gate = await CaptureBaselineStep().validate_output(ctx)
        assert gate.passed

    async def test_can_skip_when_baseline_exists(self, tmp_path: Path) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        worktree = tmp_path / "worktree"
        (worktree / ".sova").mkdir(parents=True)
        (worktree / ".sova" / "test-baseline.json").write_text('{"mode":"exit_code","exit_code":0,"tests":[]}')
        ctx = _make_ctx(worktree_dir=worktree)

        step = CaptureBaselineStep()
        assert await step.can_skip(ctx)
        assert ctx.test_baseline_path is not None

    async def test_can_skip_when_completed(self) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        ctx = _make_ctx(completed_steps=frozenset({"capture_baseline"}))
        assert await CaptureBaselineStep().can_skip(ctx)


# -- ValidateStep regression checking --


class TestValidateStepRegressions:
    async def test_regression_fails_gate(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import ValidateStep

        worktree = tmp_path / "worktree"
        (worktree / ".sova").mkdir(parents=True)
        baseline = BaselineSnapshot(
            mode="per_test",
            exit_code=0,
            tests=[SingleTestOutcome(nodeid="test_a", outcome="passed")],
        )
        save_baseline(baseline, worktree)

        ctx = _make_ctx(worktree_dir=worktree, test_baseline_path=baseline_path(worktree))

        with patch("sova.core.steps.validate.run_test_suite", new_callable=AsyncMock) as mock_test:
            mock_test.return_value = BaselineSnapshot(
                mode="per_test",
                exit_code=1,
                tests=[SingleTestOutcome(nodeid="test_a", outcome="failed")],
            )
            gate = await ValidateStep().verify_output(ctx)

        assert not gate.passed
        assert "regression" in gate.reason.lower()

    async def test_no_regression_passes_gate(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import ValidateStep

        worktree = tmp_path / "worktree"
        (worktree / ".sova").mkdir(parents=True)
        baseline = BaselineSnapshot(
            mode="per_test",
            exit_code=0,
            tests=[SingleTestOutcome(nodeid="test_a", outcome="passed")],
        )
        save_baseline(baseline, worktree)

        ctx = _make_ctx(worktree_dir=worktree, test_baseline_path=baseline_path(worktree))

        with patch("sova.core.steps.validate.run_test_suite", new_callable=AsyncMock) as mock_test:
            mock_test.return_value = BaselineSnapshot(
                mode="per_test",
                exit_code=0,
                tests=[SingleTestOutcome(nodeid="test_a", outcome="passed")],
            )
            gate = await ValidateStep().verify_output(ctx)

        assert gate.passed

    async def test_no_baseline_skips_regression_check(self) -> None:
        from sova.core.steps.validate import ValidateStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/wt"), test_baseline_path=None)
        gate = await ValidateStep().verify_output(ctx)

        assert gate.passed

    async def test_regression_loads_baseline_from_worktree_dir(self, tmp_path: Path) -> None:
        """Verify _check_regressions uses worktree_dir (where baseline is saved),
        not project_dir, when they differ."""
        from sova.core.steps.validate import ValidateStep

        worktree = tmp_path / "worktree"
        project = tmp_path / "project"
        worktree.mkdir()
        project.mkdir()

        # Save baseline in worktree_dir (where CaptureBaselineStep puts it)
        baseline = BaselineSnapshot(
            mode="per_test",
            exit_code=0,
            tests=[SingleTestOutcome(nodeid="test_a", outcome="passed")],
        )
        save_baseline(baseline, worktree)

        # project_dir != worktree_dir -- baseline should still be found
        ctx = _make_ctx(
            project_dir=project,
            worktree_dir=worktree,
            test_baseline_path=baseline_path(worktree),
        )

        with patch("sova.core.steps.validate.run_test_suite", new_callable=AsyncMock) as mock_test:
            mock_test.return_value = BaselineSnapshot(
                mode="per_test",
                exit_code=1,
                tests=[SingleTestOutcome(nodeid="test_a", outcome="failed")],
            )
            gate = await ValidateStep().verify_output(ctx)

        # Should detect regression (baseline was found via worktree_dir)
        assert not gate.passed
        assert "regression" in gate.reason.lower()
        # Verify run_test_suite was called with worktree dir, not project dir
        mock_test.assert_called_once()
        assert mock_test.call_args.kwargs["cwd"] == worktree

    async def test_regression_check_failure_non_fatal(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import ValidateStep

        worktree = tmp_path / "worktree"
        (worktree / ".sova").mkdir(parents=True)
        baseline = BaselineSnapshot(mode="per_test", exit_code=0, tests=[])
        save_baseline(baseline, worktree)

        ctx = _make_ctx(worktree_dir=worktree, test_baseline_path=baseline_path(worktree))

        with patch("sova.core.steps.validate.run_test_suite", new_callable=AsyncMock) as mock_test:
            mock_test.side_effect = RuntimeError("boom")
            gate = await ValidateStep().verify_output(ctx)

        # Non-fatal: passes despite exception
        assert gate.passed
