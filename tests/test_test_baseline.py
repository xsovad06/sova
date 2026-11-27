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
from sova.utils.shell import run

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


def _mock_adapter() -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_state.return_value = TaskState.RESEARCHED
    adapter.get_task.return_value = Task(id="1", title="Test issue")
    return adapter


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


async def _git_project_with_worktree(tmp_path: Path, ignore_line: str) -> tuple[Path, Path]:
    """A one-commit repository ignoring *ignore_line*, plus a linked worktree of it."""
    project = tmp_path / "project"
    project.mkdir()
    for argv in (
        ("git", "init"),
        ("git", "config", "user.email", "test@example.com"),
        ("git", "config", "user.name", "Test User"),
        ("git", "config", "commit.gpgsign", "false"),
    ):
        assert (await run(*argv, cwd=project)).success, argv
    (project / ".gitignore").write_text(f"{ignore_line}\n", encoding="utf-8")
    assert (await run("git", "add", ".gitignore", cwd=project)).success
    assert (await run("git", "commit", "-m", "initial", cwd=project)).success
    worktree = tmp_path / "wt"
    assert (await run("git", "worktree", "add", str(worktree), "-b", "feat/x", cwd=project)).success
    return project, worktree


def _common_exclude_lines(project: Path) -> list[str]:
    """Lines of the repository-wide exclude file every worktree of *project* shares."""
    exclude = project / ".git" / "info" / "exclude"
    return exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []


# BaselineSnapshot serialization


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


# save / load


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

    def test_load_non_utf8(self, tmp_path: Path) -> None:
        path = baseline_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"mode": "exit_code\xff"}')
        assert load_baseline(tmp_path) is None

    def test_baseline_path(self, tmp_path: Path) -> None:
        p = baseline_path(tmp_path)
        assert p == tmp_path / ".sova" / "test-baseline.json"


# diff_results


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


# run_test_suite


class TestRunTestSuite:
    async def test_invalid_cwd_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid cwd"):
            await run_test_suite("pytest", Path("/nonexistent/path"), cmd_timeout=60)

    async def test_none_cwd_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid cwd"):
            await run_test_suite("pytest", None, cmd_timeout=60)  # type: ignore[arg-type]

    async def test_non_pytest_uses_exit_code_mode(self, tmp_path: Path) -> None:
        with patch("sova.core.test_baseline.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, success=True, stdout="", stderr="")
            result = await run_test_suite("npm test", tmp_path, cmd_timeout=60)
        assert result.mode == "exit_code"
        assert result.exit_code == 0
        assert result.tests == []

    async def test_suite_does_not_inherit_orchestrator_database_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A project's tests create their own databases; the orchestrator's URL would redirect them."""
        monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite:////tmp/orchestrator.db")
        with patch("sova.core.test_baseline.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, success=True, stdout="", stderr="")
            await run_test_suite("npm test", tmp_path, cmd_timeout=60)
        assert "SOVA_DATABASE_URL" not in mock_run.call_args.kwargs["env"]

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

    async def test_worker_cap_comes_from_the_passed_config(self, tmp_path: Path) -> None:
        config = ProjectConfig()
        config.testing.check_workers = 7
        with patch("sova.core.test_baseline.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, success=True, stdout="", stderr="")
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("PYTEST_XDIST_AUTO_NUM_WORKERS", None)
                await run_test_suite("npm test", tmp_path, cmd_timeout=60, config=config)

        assert mock_run.call_args.kwargs["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "7"

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

    async def test_pytest_fallback_when_report_not_utf8(self, tmp_path: Path) -> None:
        sova_dir = tmp_path / ".sova"
        sova_dir.mkdir()
        call_count = 0

        async def mock_run_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                (sova_dir / ".test-report.json").write_bytes(b'{"tests": []\xff}')
                return MagicMock(returncode=4, success=False, stdout="", stderr="")
            return MagicMock(returncode=1, success=False, stdout="", stderr="")

        with patch("sova.core.test_baseline.run", side_effect=mock_run_side_effect):
            result = await run_test_suite("pytest", tmp_path, cmd_timeout=60)

        assert result.mode == "exit_code"
        assert call_count == 2
        assert not (sova_dir / ".test-report.json").exists()

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


# CaptureBaselineStep


class TestCaptureBaselineStep:
    async def test_execute_captures_baseline(self, tmp_path: Path) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        ctx = _make_ctx(tmp_path, worktree_dir=worktree)

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

    async def test_disabled_via_config(self, tmp_path: Path) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        config = ProjectConfig(testing={"baseline_enabled": False})
        ctx = _make_ctx(tmp_path, config=config, worktree_dir=tmp_path / "wt")
        result = await CaptureBaselineStep().execute(ctx)
        assert result.success
        assert "disabled" in result.summary

    async def test_no_test_cmd(self, tmp_path: Path) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        config = ProjectConfig(test_cmd="")
        ctx = _make_ctx(tmp_path, config=config, worktree_dir=tmp_path / "wt")
        result = await CaptureBaselineStep().execute(ctx)
        assert result.success
        assert "no test command" in result.summary.lower()

    async def test_no_worktree(self, tmp_path: Path) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        ctx = _make_ctx(tmp_path, worktree_dir=None)
        result = await CaptureBaselineStep().execute(ctx)
        assert result.success
        assert "no worktree" in result.summary.lower()

    async def test_capture_failure_non_fatal(self, tmp_path: Path) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "wt")
        with patch("sova.core.steps.capture_baseline.run_test_suite", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = RuntimeError("timeout")
            result = await CaptureBaselineStep().execute(ctx)

        assert result.success
        assert "non-fatal" in result.summary.lower()
        assert ctx.test_baseline_path is None

    async def test_validate_output_always_passes(self, tmp_path: Path) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        ctx = _make_ctx(tmp_path)
        gate = await CaptureBaselineStep().validate_output(ctx)
        assert gate.passed

    async def test_can_skip_when_baseline_exists(self, tmp_path: Path) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        worktree = tmp_path / "worktree"
        (worktree / ".sova").mkdir(parents=True)
        (worktree / ".sova" / "test-baseline.json").write_text('{"mode":"exit_code","exit_code":0,"tests":[]}')
        ctx = _make_ctx(tmp_path, worktree_dir=worktree)

        step = CaptureBaselineStep()
        assert await step.can_skip(ctx)
        assert ctx.test_baseline_path is not None

    async def test_can_skip_when_completed(self, tmp_path: Path) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        ctx = _make_ctx(tmp_path, completed_steps=frozenset({"capture_baseline"}))
        assert await CaptureBaselineStep().can_skip(ctx)

    async def test_clears_stale_check_cache_marker(self, tmp_path: Path) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        ctx = _make_ctx(tmp_path, worktree_dir=worktree)

        with (
            patch("sova.core.steps.capture_baseline.run_test_suite", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.capture_baseline.clear_marker") as mock_clear,
        ):
            mock_run.return_value = BaselineSnapshot(mode="exit_code", exit_code=0, tests=[])
            await CaptureBaselineStep().execute(ctx)

        mock_clear.assert_called_once_with(worktree)

    @pytest.mark.parametrize("prelinked", [False, True])
    @pytest.mark.parametrize("ignore_line", [".venv/", ".venv"])
    async def test_links_venv_before_the_run_and_keeps_it_untracked(
        self, tmp_path: Path, ignore_line: str, prelinked: bool
    ) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        project, worktree = await _git_project_with_worktree(tmp_path, ignore_line)
        (project / ".venv").mkdir()
        if prelinked:  # create_worktree already linked it because copy_files lists ".venv"
            (worktree / ".venv").symlink_to(project / ".venv")
        ctx = _make_ctx(tmp_path, project_dir=project, worktree_dir=worktree)

        async def fake_suite(**kwargs: object) -> BaselineSnapshot:
            if not (worktree / ".venv").is_symlink():
                return BaselineSnapshot(mode="exit_code", exit_code=4, tests=[])
            return BaselineSnapshot(mode="per_test", exit_code=0, tests=[SingleTestOutcome("t1", "passed")])

        with patch("sova.core.steps.capture_baseline.run_test_suite", side_effect=fake_suite):
            result = await CaptureBaselineStep().execute(ctx)

        assert "per-test, 1 tests" in result.summary
        status = await run("git", "status", "--porcelain", "--untracked-files=all", cwd=worktree)
        assert ".venv" not in status.stdout
        # Git has no per-worktree exclude file: the line lands in the common git dir, shared with the
        # primary checkout, and only when the project's own ignore rule does not already cover a symlink.
        assert ("/.venv" in _common_exclude_lines(project)) is (ignore_line == ".venv/")

    async def test_no_project_venv_writes_no_exclude(self, tmp_path: Path) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        project, worktree = await _git_project_with_worktree(tmp_path, ".venv/")
        ctx = _make_ctx(tmp_path, project_dir=project, worktree_dir=worktree)

        with patch("sova.core.steps.capture_baseline.run_test_suite", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = BaselineSnapshot(mode="exit_code", exit_code=0, tests=[])
            result = await CaptureBaselineStep().execute(ctx)

        assert result.success
        mock_run.assert_awaited_once()
        assert not (worktree / ".venv").exists()
        assert "/.venv" not in _common_exclude_lines(project)

    async def test_supplies_missing_copy_files_entries(self, tmp_path: Path) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        project = tmp_path / "project"
        (project / "fixtures").mkdir(parents=True)
        (project / ".env").write_text("A=1\n", encoding="utf-8")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        config = ProjectConfig()
        config.worktree.copy_files = [".env", "fixtures"]
        ctx = _make_ctx(tmp_path, project_dir=project, worktree_dir=worktree, config=config)

        with patch("sova.core.steps.capture_baseline.run_test_suite", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = BaselineSnapshot(mode="exit_code", exit_code=0, tests=[])
            result = await CaptureBaselineStep().execute(ctx)

        assert result.success
        assert (worktree / ".env").read_text(encoding="utf-8") == "A=1\n"
        assert (worktree / "fixtures").is_symlink()

    async def test_file_blocking_the_venv_link_is_non_fatal(self, tmp_path: Path) -> None:
        from sova.core.steps.capture_baseline import CaptureBaselineStep

        project = tmp_path / "project"
        (project / ".venv").mkdir(parents=True)
        (project / ".env").write_text("A=1\n", encoding="utf-8")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".venv").write_text("not a directory", encoding="utf-8")
        config = ProjectConfig()
        config.worktree.copy_files = [".venv", ".env"]
        ctx = _make_ctx(tmp_path, project_dir=project, worktree_dir=worktree, config=config)

        with patch("sova.core.steps.capture_baseline.run_test_suite", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = BaselineSnapshot(mode="exit_code", exit_code=0, tests=[])
            result = await CaptureBaselineStep().execute(ctx)

        assert result.success
        assert ctx.test_baseline_path is not None
        assert (worktree / ".venv").is_file()
        assert (worktree / ".env").read_text(encoding="utf-8") == "A=1\n"


class TestLinkWorktreeEnv:
    """``link_worktree_env()``: what the baseline step adds to a worktree, and what it leaves alone."""

    @pytest.mark.parametrize(("copy_files", "linked"), [([], False), ([".venv"], True)])
    async def test_failed_exclude_skips_an_unrequested_venv(
        self, tmp_path: Path, copy_files: list[str], linked: bool
    ) -> None:
        from sova.git.worktree import link_worktree_env

        project = tmp_path / "project"
        (project / ".venv").mkdir(parents=True)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with patch("sova.git.worktree._exclude_venv_link", new_callable=AsyncMock, return_value=False):
            await link_worktree_env(project, worktree, copy_files)

        assert (worktree / ".venv").is_symlink() is linked

    @pytest.mark.parametrize("worktree_has_env", [True, False])
    async def test_keeps_the_compose_project_name(self, tmp_path: Path, worktree_has_env: bool) -> None:
        from sova.git.worktree import link_worktree_env

        project = tmp_path / "project"
        project.mkdir()
        (project / ".env").write_text("A=1\n", encoding="utf-8")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        if worktree_has_env:  # as create_worktree() leaves it
            (worktree / ".env").write_text("A=1\nCOMPOSE_PROJECT_NAME=project\n", encoding="utf-8")

        await link_worktree_env(project, worktree, [".env"])

        assert (worktree / ".env").read_text(encoding="utf-8") == "A=1\nCOMPOSE_PROJECT_NAME=project\n"

    async def test_rejects_a_path_outside_the_project(self, tmp_path: Path) -> None:
        from sova.git.worktree import link_worktree_env

        project = tmp_path / "project"
        project.mkdir()
        (tmp_path / "escape.txt").write_text("secret", encoding="utf-8")
        worktree = tmp_path / "nest" / "wt"
        worktree.mkdir(parents=True)

        await link_worktree_env(project, worktree, ["../escape.txt"])

        assert not (tmp_path / "nest" / "escape.txt").exists()


# ValidateStep regression checking


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

        ctx = _make_ctx(tmp_path, worktree_dir=worktree, test_baseline_path=baseline_path(worktree))

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

        ctx = _make_ctx(tmp_path, worktree_dir=worktree, test_baseline_path=baseline_path(worktree))

        with patch("sova.core.steps.validate.run_test_suite", new_callable=AsyncMock) as mock_test:
            mock_test.return_value = BaselineSnapshot(
                mode="per_test",
                exit_code=0,
                tests=[SingleTestOutcome(nodeid="test_a", outcome="passed")],
            )
            gate = await ValidateStep().verify_output(ctx)

        assert gate.passed

    async def test_no_baseline_skips_regression_check(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import ValidateStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "wt", test_baseline_path=None)
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

        # project_dir != worktree_dir: baseline should still be found
        ctx = _make_ctx(
            tmp_path,
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

    @pytest.mark.parametrize(("mode", "runs_suite"), [("exit_code", False), ("per_test", True)])
    async def test_only_a_per_test_baseline_runs_the_suite(self, tmp_path: Path, mode: str, runs_suite: bool) -> None:
        from sova.core.steps.validate import ValidateStep

        worktree = tmp_path / "worktree"
        save_baseline(BaselineSnapshot(mode=mode, exit_code=0, tests=[]), worktree)
        ctx = _make_ctx(tmp_path, worktree_dir=worktree, test_baseline_path=baseline_path(worktree))

        with (
            patch("sova.core.steps.validate.run_test_suite", new_callable=AsyncMock) as mock_test,
            patch("sova.core.steps.validate.log") as mock_log,
        ):
            mock_test.return_value = BaselineSnapshot(mode="per_test", exit_code=0, tests=[])
            gate = await ValidateStep().verify_output(ctx)

        assert gate.passed
        assert mock_test.await_count == (1 if runs_suite else 0)
        logged = [c.args[0] for c in mock_log.info.call_args_list]
        skip_logged = "step.validate.regression_check_skipped" in logged
        assert skip_logged is not runs_suite

    async def test_regression_check_failure_non_fatal(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import ValidateStep

        worktree = tmp_path / "worktree"
        (worktree / ".sova").mkdir(parents=True)
        baseline = BaselineSnapshot(mode="per_test", exit_code=0, tests=[])
        save_baseline(baseline, worktree)

        ctx = _make_ctx(tmp_path, worktree_dir=worktree, test_baseline_path=baseline_path(worktree))

        with patch("sova.core.steps.validate.run_test_suite", new_callable=AsyncMock) as mock_test:
            mock_test.side_effect = RuntimeError("boom")
            gate = await ValidateStep().verify_output(ctx)

        # Non-fatal: passes despite exception
        assert gate.passed

    async def test_no_commits_ahead_fails_gate(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import ValidateStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "wt", test_baseline_path=None)

        with patch("sova.core.steps.validate.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="")
            gate = await ValidateStep().validate_output(ctx)

        assert not gate.passed
        assert "No commits ahead of base" in gate.reason

    async def test_git_log_failure_fails_gate(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import ValidateStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "wt", test_baseline_path=None)

        with patch("sova.core.steps.validate.run") as mock_run:
            mock_run.return_value = MagicMock(success=False, stdout="")
            gate = await ValidateStep().validate_output(ctx)

        assert not gate.passed
        assert "No commits ahead of base" in gate.reason
