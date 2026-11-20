"""MonitorCI step: polling, fix loop, SonarCloud and CI config.

Split from tests/test_core.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.config.models import ProjectConfig
from tests.core_helpers import _make_ctx

# Every test in this module runs against the shared per-worker in-memory DB
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


# ---------------------------------------------------------------------------
# MonitorCIStep: no-checks grace period
# ---------------------------------------------------------------------------


class TestMonitorCIStep:
    @pytest.fixture(autouse=True)
    def _stub_pr_head_verification(self):
        """Most tests here resolve a local head sha of "abc123def456" via
        _get_local_head_sha; default the PR-head verification to agree with
        it so these tests don't shell out to the real `gh` CLI. Tests that
        exercise a head mismatch override this patch explicitly."""
        with patch(
            "sova.core.steps.monitor_ci.get_pr_head_sha",
            AsyncMock(return_value="abc123def456"),
        ):
            yield

    async def test_passes_when_all_checks_pass(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
        ):
            mock_checks.return_value = [check]
            result = await step.execute(ctx)

        assert result.success
        assert "1 CI checks passed" in result.summary
        mock_checks.assert_awaited_once_with("abc123def456", repo=ctx.repo, github_user=ctx.config.github_user)

    async def test_fails_when_check_fails_and_fix_disabled(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.max_fix_attempts = 0
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_passed=False, is_failed=True, details_url="")
        check.name = "lint"
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
        ):
            mock_checks.return_value = [check]
            result = await step.execute(ctx)

        assert not result.success
        assert "lint" in result.summary

    async def test_no_checks_passes_after_grace_period(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_pr_checks,
            patch("sova.core.steps.monitor_ci.sha_has_workflow_runs", new_callable=AsyncMock) as mock_runs,
        ):
            mock_checks.return_value = []
            mock_pr_checks.return_value = []
            mock_runs.return_value = False
            result = await step.execute(ctx)

        assert result.success
        assert "no ci checks" in result.summary.lower()
        mock_pr_checks.assert_awaited_once_with(ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user)
        mock_runs.assert_awaited_once_with("abc123def456", repo=ctx.repo, github_user=ctx.config.github_user)

    async def test_sha_checks_not_registered_keeps_polling_when_pr_has_checks(self, tmp_path: Path) -> None:
        """An empty SHA-scoped result past the grace period must not pass the
        step while the PR-level lookup reports checks exist: GitHub may simply
        not have registered the SHA-scoped checks yet (#1175 CodeRabbit review:
        finding 1). The step must keep polling until the PR-level lookup also
        confirms no checks, with ``max_wait`` as the fail-closed terminal
        outcome rather than a silent pass."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        config.ci.poll_interval = 10
        config.ci.max_wait = 20
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_pr_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
            # elapsed is now measured via time.monotonic() (#1175 finding 6), so a
            # loop that never completes on its own needs a fake clock that actually
            # advances past max_wait, or mocking out asyncio.sleep leaves real wall
            # time nearly frozen and the test would spin until real 20s pass.
            patch("sova.core.steps.monitor_ci.time.monotonic", side_effect=[0, 10, 20]),
        ):
            mock_checks.return_value = []
            failing_check = MagicMock(is_completed=True, is_passed=False, is_failed=True, details_url="")
            failing_check.name = "lint"
            mock_pr_checks.return_value = [failing_check]
            result = await step.execute(ctx)

        assert not result.success
        assert "timed out" in result.summary.lower()

    async def test_no_checks_reported_exit_passes_after_grace_period(self, tmp_path: Path) -> None:
        """A PR whose workflows are all path-filtered away reports no checks
        at all, which makes `gh pr checks` exit non-zero with "no checks
        reported" on stderr. The PR-level cross-check must read that as a
        confirmed empty list rather than a fetch failure, so the step
        finishes right after the grace period instead of polling until
        max_wait. Patches the shell call, not get_ci_checks, so the real
        stderr mapping is exercised end to end (#1314)."""
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.utils.shell import ShellResult

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        config.ci.poll_interval = 10
        # Small enough that a regression (polling to max_wait) fails on the
        # assertions below rather than on pytest's per-test timeout.
        config.ci.max_wait = 5
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        no_checks = ShellResult(returncode=1, stdout="", stderr="no checks reported on the 'feat/x' branch\n")
        no_runs = ShellResult(returncode=0, stdout="[]", stderr="")

        async def fake_run(*args: str, **kwargs: object) -> ShellResult:
            # The step makes two distinct gh calls here: the PR-level check
            # lookup, and the workflow-run corroboration the empty result now
            # requires. Dispatch on argv so each gets its real-world answer.
            if args[:3] == ("gh", "run", "list"):
                return no_runs
            return no_checks

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_sha_checks,
            patch("sova.git.pr.run", new_callable=AsyncMock, side_effect=fake_run) as mock_run,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_sha_checks.return_value = []
            result = await step.execute(ctx)

        assert result.success
        assert "no ci checks" in result.summary.lower()
        # One PR-level lookup plus one workflow-run lookup, no second poll: the
        # step never waited out max_wait.
        assert mock_run.await_count == 2
        mock_sleep.assert_not_awaited()

    async def test_workflow_run_without_checks_keeps_polling(self, tmp_path: Path) -> None:
        """An empty check list is only "no CI" when GitHub also has no
        workflow run for the commit. Both check lookups read the same head
        commit, so during a registration race both are empty and the PR-level
        half corroborates nothing; the run record, created when the workflow
        is triggered, is what distinguishes "never triggered" from "not
        registered yet". A run that exists with no check is an anomaly, not a
        pass, so the step keeps polling (#1314 CodeRabbit review)."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        config.ci.poll_interval = 10
        config.ci.max_wait = 20
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_pr_checks,
            patch("sova.core.steps.monitor_ci.sha_has_workflow_runs", new_callable=AsyncMock) as mock_runs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
            patch("sova.core.steps.monitor_ci.time.monotonic", side_effect=[0, 10, 20]),
        ):
            mock_checks.return_value = []
            mock_pr_checks.return_value = []
            mock_runs.return_value = True
            result = await step.execute(ctx)

        assert not result.success
        assert "timed out" in result.summary.lower()

    async def test_workflow_run_lookup_failure_does_not_false_pass(self, tmp_path: Path) -> None:
        """An unreadable workflow-run lookup is no evidence either way, so it
        must fail closed exactly like the PR-level fetch failure does: reading
        it as "CI never triggered" would reintroduce the silent pass the
        corroboration exists to prevent (#1314 CodeRabbit review)."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        config.ci.poll_interval = 10
        config.ci.max_wait = 20
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_pr_checks,
            patch("sova.core.steps.monitor_ci.sha_has_workflow_runs", new_callable=AsyncMock) as mock_runs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
            patch("sova.core.steps.monitor_ci.time.monotonic", side_effect=[0, 10, 20]),
        ):
            mock_checks.return_value = []
            mock_pr_checks.return_value = []
            mock_runs.return_value = None
            result = await step.execute(ctx)

        assert not result.success
        assert "timed out" in result.summary.lower()

    async def test_exclude_checks_matching_everything_passes_after_grace_period(self, tmp_path: Path) -> None:
        """When every check on both the SHA-scoped and PR-level lookups
        matches ci.exclude_checks, the no-checks cross-check must apply the
        same exclude patterns to the PR-level result, or a project whose
        only checks are excluded polls until max_wait instead of passing
        after the grace period (#1175 CodeRabbit review: finding 1). The
        all-excluded short-circuit must still wait out the grace period
        itself before passing, since an early poll cannot tell "every check
        is excluded" from "only the excluded check has registered so far"
        (#1314 CodeRabbit review on PR #1317, finding 1): a non-zero grace
        period and an advancing clock confirm the pass is not returned on
        the first poll."""
        import itertools

        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.exclude_checks = ["Konflux"]
        config.ci.no_checks_grace_period = 120
        config.ci.poll_interval = 30
        config.ci.max_wait = 600
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        excluded = MagicMock(is_completed=False, is_passed=False, is_failed=False)
        excluded.name = "Red Hat Konflux / build"
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_pr_checks,
            patch("sova.core.steps.monitor_ci.sha_has_workflow_runs", new_callable=AsyncMock) as mock_runs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("sova.core.steps.monitor_ci.time.monotonic", side_effect=itertools.count(0, 30)),
        ):
            mock_checks.return_value = [excluded]
            mock_pr_checks.return_value = [excluded]
            mock_runs.return_value = True
            result = await step.execute(ctx)

        assert result.success
        assert "no ci checks" in result.summary.lower()
        mock_runs.assert_not_called()
        assert mock_sleep.await_count >= 1

    async def test_exclude_checks_pre_grace_poll_still_sees_later_failure(self, tmp_path: Path) -> None:
        """A poll that sees only the excluded check must not short-circuit
        to a pass before a later poll's real check has had a chance to
        register: all_excluded is computed from a single poll, and the
        first poll runs at elapsed=0, squarely inside the check-registration
        window the grace period exists to cover. Regression for the
        probe in #1314 CodeRabbit review on PR #1317, finding 1: poll 1
        returns only the excluded check, poll 2 returns the excluded check
        plus a failing one, and the step must still fail."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.exclude_checks = ["CodeRabbit"]
        config.ci.poll_interval = 10
        config.ci.max_wait = 300
        config.ci.max_fix_attempts = 0
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        excluded = MagicMock(is_completed=False, is_passed=False, is_failed=False)
        excluded.name = "CodeRabbit"
        failing = MagicMock(is_completed=True, is_passed=False, is_failed=True)
        failing.name = "Python Tests"

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.sha_has_workflow_runs", new_callable=AsyncMock) as mock_runs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [[excluded], [excluded, failing]]
            result = await step.execute(ctx)

        assert not result.success
        assert "python tests" in result.summary.lower()
        mock_runs.assert_not_called()

    async def test_pr_confirm_fetch_failure_does_not_false_pass(self, tmp_path: Path) -> None:
        """A transient failure of the PR-level cross-check lookup must not be
        read as 'confirmed no CI': that is the same silent pass the
        cross-check exists to prevent, just moved behind one failed API call.
        The step must keep polling to max_wait rather than pass (#1175
        CodeRabbit review: finding 3)."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        config.ci.poll_interval = 10
        config.ci.max_wait = 20
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_pr_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
            patch("sova.core.steps.monitor_ci.time.monotonic", side_effect=[0, 10, 20]),
        ):
            mock_checks.return_value = []
            mock_pr_checks.return_value = None
            result = await step.execute(ctx)

        assert not result.success
        assert "timed out" in result.summary.lower()

    async def test_pr_level_checks_excluded_still_requires_run_corroboration(self, tmp_path: Path) -> None:
        """When the SHA-scoped lookup is genuinely empty but the PR-level
        lookup still reports a check that ci.exclude_checks filters away,
        _pr_confirms_no_checks() must not treat the PR-level exclusion alone
        as confirmation: that combination is the registration-race
        signature (the SHA-scoped endpoint hasn't caught up with the
        PR-level one), not the legitimate all-excluded case, which short-
        circuits earlier in _poll_ci()'s own all_excluded branch (#1314
        CodeRabbit review on PR #1317, Round 4 finding 1). A workflow run
        existing for the commit must keep the step polling instead of
        passing."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.exclude_checks = ["CodeRabbit"]
        config.ci.no_checks_grace_period = 0
        config.ci.poll_interval = 10
        config.ci.max_wait = 20
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        excluded = MagicMock(is_completed=False, is_passed=False, is_failed=False)
        excluded.name = "CodeRabbit"

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock, return_value=[]),
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock, return_value=[excluded]),
            patch(
                "sova.core.steps.monitor_ci.sha_has_workflow_runs",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_runs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
            patch("sova.core.steps.monitor_ci.time.monotonic", side_effect=[0, 0, 10, 10, 20]),
        ):
            result = await step.execute(ctx)

        assert not result.success
        assert "timed out" in result.summary.lower()
        mock_runs.assert_awaited()

    async def test_no_checks_pr_level_fallback_reuses_fetch_for_cross_check(self, tmp_path: Path) -> None:
        """On the PR-level fallback path (local HEAD unresolved), _poll_ci()
        already fetched the PR-level check list to find it empty;
        _pr_confirms_no_checks() must reuse that result instead of issuing
        an identical get_ci_checks() call again, which would be a pure
        redundant round trip with no new information (#1314 CodeRabbit
        review on PR #1317, finding 4). The run lookup remains the only
        independent half of the cross-check on this path."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value=""),
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_pr_checks,
            patch("sova.core.steps.monitor_ci.get_pr_head_sha", new_callable=AsyncMock) as mock_head_sha,
            patch("sova.core.steps.monitor_ci.sha_has_workflow_runs", new_callable=AsyncMock) as mock_runs,
        ):
            mock_pr_checks.return_value = []
            mock_head_sha.return_value = "abc123def456"
            mock_runs.return_value = False
            result, _ = await step._poll_ci(ctx)

        assert result.success
        mock_pr_checks.assert_awaited_once_with(ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user)
        mock_runs.assert_awaited_once()

    async def test_no_checks_waits_during_grace_period(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 120
        config.ci.poll_interval = 60
        config.ci.max_wait = 180
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            # No checks on first poll, then checks appear
            mock_checks.side_effect = [[], [check]]
            result = await step.execute(ctx)

        assert result.success
        assert "1 CI checks passed" in result.summary

    async def test_fetch_failure_does_not_false_pass(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        config.ci.poll_interval = 10
        config.ci.max_wait = 30
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        passed_check = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [None, None, [passed_check]]
            result = await step.execute(ctx)

        assert result.success
        assert "1 CI checks passed" in result.summary
        assert mock_checks.call_count == 3

    async def test_fetch_failure_times_out(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.poll_interval = 10
        config.ci.max_wait = 20
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
            patch("sova.core.steps.monitor_ci.time.monotonic", side_effect=[0, 10, 20]),
        ):
            mock_checks.return_value = None
            result = await step.execute(ctx)

        assert not result.success
        assert "timed out" in result.summary.lower()

    async def test_fails_when_no_pr(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=None)
        step = MonitorCIStep()

        result = await step.execute(ctx)

        assert not result.success
        assert "no pr" in result.summary.lower()

    async def test_poll_ci_binds_to_resolved_head_sha(self, tmp_path: Path) -> None:
        """The initial poll resolves the local HEAD SHA and scopes the lookup to it.

        This is the core fix for issue #1175: a PR-level check lookup can
        report a prior commit's stale, already-green checks right after a
        push. Binding to the resolved HEAD SHA makes that impossible.
        """
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        step = MonitorCIStep()

        passed_check = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_sha_checks,
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_pr_checks,
        ):
            mock_sha_checks.return_value = [passed_check]
            result, _ = await step._poll_ci(ctx)

        assert result.success
        mock_sha_checks.assert_awaited_once_with("abc123def456", repo=ctx.repo, github_user=ctx.config.github_user)
        mock_pr_checks.assert_not_awaited()

    async def test_poll_ci_rebinds_when_pr_head_no_longer_matches(self, tmp_path: Path) -> None:
        """If another actor updates the PR after this pipeline's push (a human
        pushing a review fix, GitHub's 'Update branch' button, a concurrent
        SOVA run), that is a benign event, not a failure of this pipeline's
        own work. The step rebinds to the new head and keeps polling so it
        reports CI for the commit that actually matters now, rather than
        failing the whole run and feeding the repeated-failure gate over
        someone else's push (#1175 CodeRabbit review: finding 5)."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.poll_interval = 10
        config.ci.max_wait = 30
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        stale_passed = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        new_passed = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch(
                "sova.core.steps.monitor_ci.get_ci_checks_for_sha",
                new_callable=AsyncMock,
                side_effect=[[stale_passed], [new_passed]],
            ) as mock_checks,
            patch(
                "sova.core.steps.monitor_ci.get_pr_head_sha",
                new_callable=AsyncMock,
                return_value="differentsha789",
            ),
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            result = await step.execute(ctx)

        assert result.success
        assert mock_checks.call_count == 2
        mock_checks.assert_any_call("differentsha789", repo=ctx.repo, github_user=ctx.config.github_user)
        mock_sleep.assert_not_awaited()

    async def test_rebind_resets_no_checks_grace_clock(self, tmp_path: Path) -> None:
        """Rebinding to a new PR head must reset the no-checks grace clock,
        not just the SHA: the overall ``elapsed`` clock can already exceed
        ``no_checks_grace_period`` by the time a mismatch is confirmed (the
        old SHA's checks took a while to complete), so without its own
        baseline the very first empty, not-yet-registered result for the
        freshly bound head would be immediately eligible for a false
        'No CI checks found' pass (#1175 CodeRabbit review round 4, finding
        2). The PR-level cross-check must never even be consulted here: with
        the clock correctly reset, the empty result falls into the ordinary
        'not yet registered, keep polling' branch instead."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.poll_interval = 10
        config.ci.max_wait = 1000
        config.ci.no_checks_grace_period = 50
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        stale_passed = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        new_passed = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch(
                "sova.core.steps.monitor_ci.get_ci_checks_for_sha",
                new_callable=AsyncMock,
                side_effect=[[stale_passed], [], [new_passed]],
            ) as mock_checks,
            patch(
                "sova.core.steps.monitor_ci.get_pr_head_sha",
                new_callable=AsyncMock,
                return_value="newsha789",
            ),
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_pr_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
            # start_time=0; rebind resets grace_start=60 and elapsed=60; the
            # next empty-checks grace check reads 65-60=5s since reset (well
            # under the 50s grace period) even though overall elapsed is 60s
            # past start_time; final elapsed read is 70.
            patch("sova.core.steps.monitor_ci.time.monotonic", side_effect=[0, 60, 60, 65, 70]),
        ):
            mock_pr_checks.return_value = []
            result = await step.execute(ctx)

        assert result.success
        assert "All 1 CI checks passed" in result.summary
        assert mock_checks.call_count == 3
        mock_pr_checks.assert_not_awaited()

    async def test_poll_ci_falls_back_to_pr_level_when_sha_unresolved(self, tmp_path: Path) -> None:
        """When the local HEAD SHA can't be resolved, fall back to the PR-level lookup
        instead of failing the whole step outright (design decision 4)."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        step = MonitorCIStep()

        passed_check = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value=""),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_sha_checks,
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_pr_checks,
        ):
            mock_pr_checks.return_value = [passed_check]
            result, _ = await step._poll_ci(ctx)

        assert result.success
        mock_sha_checks.assert_not_awaited()
        mock_pr_checks.assert_awaited_once_with(ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user)

    async def test_skipped_checks_do_not_block_or_fail(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        step = MonitorCIStep()

        passed = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="Tests")
        skipped = MagicMock(is_completed=True, is_passed=False, is_failed=False, name="Fork Gate")
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
        ):
            mock_checks.return_value = [passed, skipped]
            result = await step.execute(ctx)

        assert result.success
        assert "2 CI checks passed" in result.summary

    async def test_exclude_checks_filters_matching(self, tmp_path: Path) -> None:
        """Checks matching exclude_checks patterns are ignored entirely."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.exclude_checks = ["bonfire-tekton"]
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        passed = MagicMock(is_completed=True, is_passed=True, is_failed=False)
        passed.name = "Tests"
        excluded = MagicMock(is_completed=False, is_passed=False, is_failed=False)
        excluded.name = "Red Hat Konflux / rbac-bonfire-tekton / insights-rbac"
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
        ):
            mock_checks.return_value = [passed, excluded]
            result = await step.execute(ctx)

        assert result.success
        assert "1 CI checks passed" in result.summary

    async def test_exclude_checks_does_not_filter_non_matching(self, tmp_path: Path) -> None:
        """Checks not matching exclude_checks patterns are still evaluated."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.exclude_checks = ["bonfire-tekton"]
        config.ci.max_fix_attempts = 0
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        failed = MagicMock(is_completed=True, is_passed=False, is_failed=True, details_url="")
        failed.name = "lint"
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
        ):
            mock_checks.return_value = [failed]
            result = await step.execute(ctx)

        assert not result.success
        assert "lint" in result.summary

    async def test_resume_skips_poll_when_ci_passed(self, tmp_path: Path) -> None:
        """On resume, if CI already passed, skip polling entirely."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10, resume_run_id=5)
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
        ):
            mock_checks.return_value = [check]
            result = await step.execute(ctx)

        assert result.success
        assert "resumed after interruption" in result.summary

    async def test_resume_skips_poll_when_ci_failed(self, tmp_path: Path) -> None:
        """On resume, if CI already failed, return failure immediately."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.max_fix_attempts = 0
        ctx = _make_ctx(tmp_path, pr_number=10, config=config, resume_run_id=5)
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_passed=False, is_failed=True, details_url="")
        check.name = "lint"
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
        ):
            mock_checks.return_value = [check]
            result = await step.execute(ctx)

        assert not result.success
        assert "lint" in result.summary

    async def test_resume_polls_when_ci_incomplete(self, tmp_path: Path) -> None:
        """On resume, if CI is still running, fall through to normal polling."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10, resume_run_id=5)
        step = MonitorCIStep()

        incomplete = MagicMock(is_completed=False, is_passed=False, is_failed=False, name="CI")
        passed = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            # Resume check returns incomplete, then normal poll returns passed
            mock_checks.side_effect = [[incomplete], [passed]]
            result = await step.execute(ctx)

        assert result.success
        assert "1 CI checks passed" in result.summary

    async def test_resume_respects_exclude_checks(self, tmp_path: Path) -> None:
        """On resume, excluded checks should not cause false failures."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.exclude_checks = ["sonar"]
        ctx = _make_ctx(tmp_path, pr_number=10, config=config, resume_run_id=5)
        step = MonitorCIStep()

        passed = MagicMock(is_completed=True, is_passed=True, is_failed=False)
        passed.name = "Tests"
        excluded_fail = MagicMock(is_completed=True, is_passed=False, is_failed=True)
        excluded_fail.name = "sonar-quality-gate"
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
        ):
            mock_checks.return_value = [passed, excluded_fail]
            result = await step.execute(ctx)

        assert result.success
        assert "resumed after interruption" in result.summary

    async def test_heartbeat_writes_during_poll(self, tmp_path: Path) -> None:
        """CI polling should write heartbeat messages to the output writer."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.poll_interval = 10
        config.ci.max_wait = 30
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        mock_writer = MagicMock()
        ctx.output_writer = mock_writer
        step = MonitorCIStep()

        incomplete = MagicMock(is_completed=False, is_passed=False, is_failed=False, name="CI")
        passed = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [[incomplete], [passed]]
            result = await step.execute(ctx)

        assert result.success
        assert mock_writer.write_line.called
        heartbeat_msg = mock_writer.write_line.call_args_list[0][0][0]
        assert "CI poll:" in heartbeat_msg

    async def test_get_local_head_sha_returns_empty_on_missing_git(self, tmp_path: Path) -> None:
        """A missing ``git`` executable (or pruned working_dir) must not raise
        out of ``_get_local_head_sha``; it should fall back to an empty string
        so callers degrade to the PR-level lookup (#1175 CodeRabbit review:
        finding 7)."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        step = MonitorCIStep()

        with patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=FileNotFoundError("no git")):
            result = await step._get_local_head_sha(ctx)

        assert result == ""

    async def test_unknown_commit_fails_with_named_diagnosis_not_bare_timeout(self, tmp_path: Path) -> None:
        """A SHA GitHub has no record of at all (e.g. a push that silently
        failed) can never resolve on retry. The step must fail immediately
        with a diagnosis naming the unresolvable commit, rather than polling
        until max_wait and reporting a bare, misleading 'timed out'
        (#1175 CodeRabbit review: finding 7, covering finding 3's fix)."""
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.git.operations import UnknownCommitError

        config = ProjectConfig()
        config.ci.poll_interval = 10
        config.ci.max_wait = 9999
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="deadbeef0000"),
            patch(
                "sova.core.steps.monitor_ci.get_ci_checks_for_sha",
                new_callable=AsyncMock,
                side_effect=UnknownCommitError("GitHub has no record of commit deadbeef0000"),
            ) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            result = await step.execute(ctx)

        assert not result.success
        assert "deadbeef" in result.summary.lower()
        assert "timed out" not in result.summary.lower()
        mock_checks.assert_awaited_once()
        mock_sleep.assert_not_awaited()

    async def test_unknown_commit_retries_when_pr_head_still_matches(self, tmp_path: Path) -> None:
        """A corroborated UnknownCommitError (both check-runs and commit-status
        404/422) is still not conclusive if the PR's own head SHA confirms the
        commit exists: that is strong evidence of a transient GitHub-side
        inconsistency rather than a failed push, so the step keeps polling
        instead of failing outright (#1175 CodeRabbit review: finding 2)."""
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.git.operations import UnknownCommitError

        config = ProjectConfig()
        config.ci.poll_interval = 10
        config.ci.max_wait = 30
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        passed_check = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch(
                "sova.core.steps.monitor_ci.get_ci_checks_for_sha",
                new_callable=AsyncMock,
                side_effect=[UnknownCommitError("transient"), [passed_check]],
            ) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            # Autouse fixture stubs get_pr_head_sha to "abc123def456", matching
            # expected_sha, so the cross-check confirms the commit exists.
            result = await step.execute(ctx)

        assert result.success
        mock_sleep.assert_awaited_once()
        assert mock_checks.call_count == 2

    async def test_unknown_commit_retries_when_pr_head_unverifiable(self, tmp_path: Path) -> None:
        """An unreadable PR head (get_pr_head_sha returning "" on a transient
        gh/network failure) is 'no corroboration either way', not 'confirmed
        bad push': the step must keep polling rather than failing terminally
        on the very first poll (#1175 CodeRabbit review: finding 2)."""
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.git.operations import UnknownCommitError

        config = ProjectConfig()
        config.ci.poll_interval = 10
        config.ci.max_wait = 30
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        passed_check = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch(
                "sova.core.steps.monitor_ci.get_ci_checks_for_sha",
                new_callable=AsyncMock,
                side_effect=[UnknownCommitError("transient"), [passed_check]],
            ) as mock_checks,
            patch(
                "sova.core.steps.monitor_ci.get_pr_head_sha",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            result = await step.execute(ctx)

        assert result.success
        mock_sleep.assert_awaited_once()
        assert mock_checks.call_count == 2

    async def test_all_cancelled_checks_do_not_report_success(self, tmp_path: Path) -> None:
        """A fully cancelled check set on the bound SHA (the normal consequence
        of a superseding push cancelling the polled commit's runs) must not
        read as 'CI passed': CANCELLED is completed but neither passed nor
        failed, so a naive 'all completed and nothing failed' check reports a
        false green (#1175 CodeRabbit review: finding 3)."""
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.git.operations import CheckConclusion

        config = ProjectConfig()
        config.ci.poll_interval = 10
        config.ci.max_wait = 30
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        cancelled_check = MagicMock(
            is_completed=True, is_passed=False, is_failed=False, conclusion=CheckConclusion.CANCELLED
        )
        cancelled_check.name = "CI"
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch(
                "sova.core.steps.monitor_ci.get_ci_checks_for_sha",
                new_callable=AsyncMock,
                return_value=[cancelled_check],
            ),
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await step.execute(ctx)

        assert not result.success

    async def test_all_skipped_checks_with_no_passing_check_still_pass(self, tmp_path: Path) -> None:
        """An all-SKIPPED (or all-NEUTRAL) set is a deliberate non-blocking
        outcome, not a failure: the 'nothing passed' guard added for the
        CANCELLED case must be scoped to CANCELLED specifically, or it
        regresses every project whose CI skips every job for e.g. a docs-only
        PR via path filters (#1175 CodeRabbit review round 4, finding 1)."""
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.git.operations import CheckConclusion

        ctx = _make_ctx(tmp_path, pr_number=10)
        step = MonitorCIStep()

        skipped_a = MagicMock(is_completed=True, is_passed=False, is_failed=False, conclusion=CheckConclusion.SKIPPED)
        skipped_a.name = "Tests"
        skipped_b = MagicMock(is_completed=True, is_passed=False, is_failed=False, conclusion=CheckConclusion.SKIPPED)
        skipped_b.name = "Lint"
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch(
                "sova.core.steps.monitor_ci.get_ci_checks_for_sha",
                new_callable=AsyncMock,
                return_value=[skipped_a, skipped_b],
            ),
        ):
            result = await step.execute(ctx)

        assert result.success
        assert "2 CI checks passed" in result.summary


# ---------------------------------------------------------------------------
class TestCheckExistingCI:
    """Tests for MonitorCIStep._check_existing_ci fast path."""

    @pytest.fixture(autouse=True)
    def _stub_pr_head_verification(self):
        """Several tests here fall through to _poll_ci() (the empty-list and
        all-excluded branches both defer to it), which can reach
        _find_pr_head_mismatch() -> get_pr_head_sha(). Default the PR-head
        verification to agree with the "abc123def456" local head these tests
        resolve, so they don't shell out to the real `gh` CLI (#1314
        CodeRabbit review on PR #1317, Round 4 finding 2)."""
        with patch(
            "sova.core.steps.monitor_ci.get_pr_head_sha",
            AsyncMock(return_value="abc123def456"),
        ):
            yield

    async def test_no_new_commits_all_passed(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        ctx.no_new_commits = True
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_failed=False)
        check.name = "CI"
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [check]
            result = await step.execute(ctx)

        assert result.success
        assert "already passed" in result.summary

    async def test_no_new_commits_some_failed(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.max_fix_attempts = 0
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        ctx.no_new_commits = True
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_failed=True)
        check.name = "lint"
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [check]
            result = await step.execute(ctx)

        assert not result.success
        assert "already failed" in result.summary
        assert "lint" in result.summary

    async def test_no_new_commits_still_running_falls_through(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        config.ci.poll_interval = 10
        config.ci.max_wait = 20
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        ctx.no_new_commits = True
        step = MonitorCIStep()

        running = MagicMock(is_completed=False, is_failed=False)
        running.name = "CI"
        passed = MagicMock(is_completed=True, is_failed=False)
        passed.name = "CI"
        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [[running], [passed]]
            result = await step.execute(ctx)

        assert result.success

    async def test_no_new_commits_no_checks(self, tmp_path: Path) -> None:
        """An empty check list on the fast path is ambiguous between "no CI
        configured" and "not registered yet" (#1316 CodeRabbit review:
        finding 2), so the fast path defers to _poll_ci()'s grace period and
        cross-check rather than passing immediately. The local HEAD SHA is
        explicitly stubbed as unresolvable (rather than relying on tmp_path
        not being inside a git repository, #1314 CodeRabbit review: finding
        4) so _poll_ci() falls back to the PR-level lookup; that fallback
        still corroborates against the PR's head SHA (finding 3) before
        passing once the (zeroed) grace period elapses."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        ctx.no_new_commits = True
        step = MonitorCIStep()

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value=""),
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_pr_head_sha", new_callable=AsyncMock) as mock_head_sha,
            patch("sova.core.steps.monitor_ci.sha_has_workflow_runs", new_callable=AsyncMock) as mock_runs,
        ):
            mock_checks.return_value = []
            mock_head_sha.return_value = "abc123def456"
            mock_runs.return_value = False
            result = await step.execute(ctx)

        assert result.success
        assert "no ci checks" in result.summary.lower() or "No new commits" in result.summary

    async def test_no_new_commits_no_checks_does_not_false_pass_when_pr_has_checks(self, tmp_path: Path) -> None:
        """Regression for the race the fast path used to miss: no_new_commits
        is set right after a push, so an empty check list on this path can
        mean the checks simply haven't registered yet. With a resolvable
        head SHA, the fast path must defer to _poll_ci()'s cross-check
        rather than passing immediately; a PR-level lookup that still
        reports a check in flight must keep the step polling (#1316
        CodeRabbit review: finding 2)."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        config.ci.poll_interval = 10
        config.ci.max_wait = 20
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        ctx.no_new_commits = True
        step = MonitorCIStep()

        in_flight = MagicMock(is_completed=False, is_passed=False, is_failed=False)
        in_flight.name = "CI"
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_sha_checks,
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_pr_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
            patch("sova.core.steps.monitor_ci.time.monotonic", side_effect=[0, 10, 20]),
        ):
            mock_pr_checks.side_effect = [[], [in_flight]]
            mock_sha_checks.return_value = []
            result = await step.execute(ctx)

        assert not result.success
        assert "timed out" in result.summary.lower()

    async def test_no_new_commits_fetch_failure_falls_through(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        config.ci.poll_interval = 10
        config.ci.max_wait = 20
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        ctx.no_new_commits = True
        step = MonitorCIStep()

        passed = MagicMock(is_completed=True, is_failed=False)
        passed.name = "CI"
        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [None, [passed]]
            result = await step.execute(ctx)

        assert result.success
        assert mock_checks.call_count == 2

    async def test_no_new_commits_respects_exclude_checks(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.exclude_checks = ["flaky-sonar"]
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        ctx.no_new_commits = True
        step = MonitorCIStep()

        good = MagicMock(is_completed=True, is_failed=False)
        good.name = "tests"
        flaky = MagicMock(is_completed=True, is_failed=True)
        flaky.name = "flaky-sonar-check"
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [good, flaky]
            result = await step.execute(ctx)

        assert result.success
        assert "1" in result.summary

    async def test_no_new_commits_all_excluded_falls_through_to_poll(self, tmp_path: Path) -> None:
        """The fast path's all-excluded result is the same registration-
        window ambiguity as its empty-list case above: no_new_commits can be
        set only seconds after a real push (CommitStep._handle_no_changes()
        runs, then push, then this step), so it must defer to _poll_ci()
        instead of passing immediately (#1314 CodeRabbit review on PR
        #1317, finding 2). Regression: the PR-level lookup here sees only
        the excluded check, but a genuinely failing check shows up on the
        next SHA-scoped poll, and the step must still fail."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.exclude_checks = ["CodeRabbit"]
        config.ci.poll_interval = 10
        config.ci.max_wait = 300
        config.ci.max_fix_attempts = 0
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        ctx.no_new_commits = True
        step = MonitorCIStep()

        excluded = MagicMock(is_completed=False, is_passed=False, is_failed=False)
        excluded.name = "CodeRabbit"
        failing = MagicMock(is_completed=True, is_passed=False, is_failed=True)
        failing.name = "Python Tests"

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_pr_checks,
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_sha_checks,
            patch("sova.core.steps.monitor_ci.sha_has_workflow_runs", new_callable=AsyncMock) as mock_runs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_pr_checks.return_value = [excluded]
            mock_sha_checks.side_effect = [[excluded], [excluded, failing]]
            result = await step.execute(ctx)

        assert not result.success
        assert "python tests" in result.summary.lower()
        mock_runs.assert_not_called()


class TestCILogTruncation:
    """Verify _invoke_fix truncates long CI logs before building the prompt."""

    async def test_short_logs_unchanged(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import _CI_LOG_LIMIT, MonitorCIStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt")
        step = MonitorCIStep()
        short_log = "ERROR: something failed\n" * 10

        run = AsyncMock(return_value=MagicMock(success=True, stdout="abc commit\n"))
        invoke = AsyncMock(return_value=LLMResult(text="ok", model="m", cost_usd=Decimal("0")))

        assert len(short_log) <= _CI_LOG_LIMIT

        check = _make_ci_check("lint")
        with (
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock, return_value=short_log),
            patch("sova.core.steps.monitor_ci._redact_logs", side_effect=lambda t: t),
        ):
            await step._invoke_fix(ctx, [check], invoke, run, 1)

        prompt = invoke.call_args[0][0]
        assert short_log in prompt
        assert "...(truncated)" not in prompt

    async def test_long_logs_truncated_to_tail(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import _CI_LOG_LIMIT, MonitorCIStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt")
        step = MonitorCIStep()
        long_log = "X" * (_CI_LOG_LIMIT + 5000)

        run = AsyncMock(return_value=MagicMock(success=True, stdout="abc commit\n"))
        invoke = AsyncMock(return_value=LLMResult(text="ok", model="m", cost_usd=Decimal("0")))

        check = _make_ci_check("lint")
        with (
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock, return_value=long_log),
            patch("sova.core.steps.monitor_ci._redact_logs", side_effect=lambda t: t),
        ):
            await step._invoke_fix(ctx, [check], invoke, run, 1)

        prompt = invoke.call_args[0][0]
        assert "...(truncated)" in prompt
        assert long_log not in prompt
        assert long_log[-_CI_LOG_LIMIT:] in prompt

    async def test_exact_limit_not_truncated(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import _CI_LOG_LIMIT, MonitorCIStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt")
        step = MonitorCIStep()
        exact_log = "Y" * _CI_LOG_LIMIT

        run = AsyncMock(return_value=MagicMock(success=True, stdout="abc commit\n"))
        invoke = AsyncMock(return_value=LLMResult(text="ok", model="m", cost_usd=Decimal("0")))

        check = _make_ci_check("lint")
        with (
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock, return_value=exact_log),
            patch("sova.core.steps.monitor_ci._redact_logs", side_effect=lambda t: t),
        ):
            await step._invoke_fix(ctx, [check], invoke, run, 1)

        prompt = invoke.call_args[0][0]
        assert "...(truncated)" not in prompt
        assert exact_log in prompt

    async def test_truncation_happens_after_redaction(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import _CI_LOG_LIMIT, MonitorCIStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt")
        step = MonitorCIStep()
        raw_log = "A" * (_CI_LOG_LIMIT + 2000)

        redacted_tail = "REDACTED_TAIL"
        redacted_log = "B" * (_CI_LOG_LIMIT + 1000) + redacted_tail

        run = AsyncMock(return_value=MagicMock(success=True, stdout="abc commit\n"))
        invoke = AsyncMock(return_value=LLMResult(text="ok", model="m", cost_usd=Decimal("0")))

        check = _make_ci_check("lint")
        with (
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock, return_value=raw_log),
            patch("sova.core.steps.monitor_ci._redact_logs", return_value=redacted_log),
        ):
            await step._invoke_fix(ctx, [check], invoke, run, 1)

        prompt = invoke.call_args[0][0]
        assert redacted_tail in prompt
        assert "...(truncated)" in prompt

    async def test_empty_logs_not_truncated(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt")
        step = MonitorCIStep()

        run = AsyncMock(return_value=MagicMock(success=True, stdout="abc commit\n"))
        invoke = AsyncMock(return_value=LLMResult(text="ok", model="m", cost_usd=Decimal("0")))

        check = _make_ci_check("lint")
        with (
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock, return_value=""),
            patch("sova.core.steps.monitor_ci._redact_logs", side_effect=lambda t: t),
        ):
            await step._invoke_fix(ctx, [check], invoke, run, 1)

        prompt = invoke.call_args[0][0]
        assert "...(truncated)" not in prompt


class TestCIConfigMaxWaitDefault:
    """Verify CIConfig.max_wait default is 1500."""

    def test_default_max_wait(self) -> None:
        config = ProjectConfig()
        assert config.ci.max_wait == 1500


class TestCIMaxFixAttemptsInSettingsMeta:
    """Verify ci.max_fix_attempts is registered in settings_meta."""

    def test_registered(self) -> None:
        from sova.dashboard.settings_meta import get_meta

        meta = get_meta("ci.max_fix_attempts")
        assert meta is not None
        assert meta.group == "ci"
        assert meta.value_type == "number"


class TestRunawayGuardConfig:
    """Verify the cost-independent runaway guard fields ([runaway] section) and
    their settings_meta registration."""

    def test_defaults(self) -> None:
        config = ProjectConfig()
        assert config.runaway.max_run_wall_clock_seconds == 14400
        assert config.runaway.max_run_steps == 100
        assert config.runaway.max_llm_calls == 250
        assert config.runaway.max_step_attempts == 80

    def test_registered_in_settings_meta(self) -> None:
        from sova.dashboard.settings_meta import get_meta

        for key in (
            "runaway.max_run_wall_clock_seconds",
            "runaway.max_run_steps",
            "runaway.max_llm_calls",
            "runaway.max_step_attempts",
        ):
            meta = get_meta(key)
            assert meta is not None
            assert meta.group == "runaway"
            assert meta.value_type == "number"

    def test_nested_section_registered(self) -> None:
        from sova.config.loader import _NESTED_SECTIONS

        assert "runaway" in _NESTED_SECTIONS


# MonitorCIStep: CI fix loop
# ---------------------------------------------------------------------------


def _make_ci_check(name: str = "Tests", passed: bool = False, details_url: str = "") -> MagicMock:
    """Create a mock CICheck for CI fix tests."""
    check = MagicMock(is_completed=True, is_passed=passed, is_failed=not passed, details_url=details_url)
    check.name = name
    return check


def _shell_side_effect(*args: str, **kwargs: object) -> MagicMock:
    """Dispatch shell.run mock based on the command being called."""
    if "rev-list" in args:
        return MagicMock(success=True, stdout="1\n")
    if "rev-parse" in args:
        return MagicMock(success=True, stdout="abc123def456\n")
    if "ruff" in args:
        return MagicMock(success=True, stdout="")
    if args and args[0] == "git" and len(args) > 1 and args[1] == "status":
        return MagicMock(success=True, stdout="")
    return MagicMock(success=True, stdout="abc1234 fix\n")


class TestMonitorCIFixLoop:
    @pytest.fixture(autouse=True)
    def _stub_pr_head_verification(self):
        """Same rationale as TestMonitorCIStep: default the PR-head
        verification to agree with the "abc123def456" local head sha these
        tests resolve, so they don't shell out to the real `gh` CLI."""
        with patch(
            "sova.core.steps.monitor_ci.get_pr_head_sha",
            AsyncMock(return_value="abc123def456"),
        ):
            yield

    async def test_ci_fails_fix_succeeds_first_attempt(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt")
        step = MonitorCIStep()

        failed_check = _make_ci_check(
            "Tests", passed=False, details_url="https://github.com/o/r/actions/runs/123/job/456"
        )
        passed_check = _make_ci_check("Tests", passed=True)

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock) as mock_logs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [
                [failed_check],  # initial poll
                [passed_check],  # re-poll after fix
            ]
            mock_logs.return_value = "ERROR: ModuleNotFoundError: No module named 'PIL'"

            with (
                patch("sova.core.steps.validate.find_pre_push_hook", new_callable=AsyncMock, return_value=None),
                patch("sova.git.operations.push", new_callable=AsyncMock),
                patch("sova.core.steps.monitor_ci.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            ):
                mock_invoke.return_value = LLMResult(text="Fixed", model="opus", cost_usd=Decimal("1.00"))
                mock_run.side_effect = _shell_side_effect
                result = await step.execute(ctx)

        assert result.success
        assert "1 fix attempt" in result.summary
        assert ctx.cost_usd == Decimal("1.00")

    async def test_ci_fails_fix_succeeds_second_attempt(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt")
        step = MonitorCIStep()

        failed_check = _make_ci_check(
            "Tests", passed=False, details_url="https://github.com/o/r/actions/runs/123/job/456"
        )
        passed_check = _make_ci_check("Tests", passed=True)

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock) as mock_logs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [
                [failed_check],  # initial poll
                [failed_check],  # re-poll after fix 1 (still fails)
                [passed_check],  # re-poll after fix 2 (passes)
            ]
            mock_logs.return_value = "ERROR: test failure"

            with (
                patch("sova.core.steps.validate.find_pre_push_hook", new_callable=AsyncMock, return_value=None),
                patch("sova.git.operations.push", new_callable=AsyncMock),
                patch("sova.core.steps.monitor_ci.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            ):
                mock_invoke.return_value = LLMResult(text="Fixed", model="opus", cost_usd=Decimal("0.50"))
                mock_run.side_effect = _shell_side_effect
                result = await step.execute(ctx)

        assert result.success
        assert "2 fix attempt" in result.summary
        assert mock_invoke.await_count == 2
        assert ctx.cost_usd == Decimal("1.00")

    async def test_fix_loop_escalates_to_dev_model_after_first_failed_attempt(self, tmp_path: Path) -> None:
        """Issue #780: attempt 1 uses llm.routing["monitor_ci"]; a failed attempt 1
        escalates attempt 2 to the step's dev model directly, bypassing task-type
        routing, and records why on the step-scoped reason."""
        from sova.config.models import LLMConfig, ProjectConfig
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(
            tmp_path,
            pr_number=10,
            branch_name="feat/test",
            worktree_dir=tmp_path / "wt",
            config=ProjectConfig(llm=LLMConfig(routing={"monitor_ci": "haiku"})),
        )
        step = MonitorCIStep()

        failed_check = _make_ci_check(
            "Tests", passed=False, details_url="https://github.com/o/r/actions/runs/123/job/456"
        )
        passed_check = _make_ci_check("Tests", passed=True)

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock) as mock_logs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [
                [failed_check],  # initial poll
                [failed_check],  # re-poll after fix 1 (still fails)
                [passed_check],  # re-poll after fix 2 (passes)
            ]
            mock_logs.return_value = "ERROR: test failure"

            with (
                patch("sova.core.steps.validate.find_pre_push_hook", new_callable=AsyncMock, return_value=None),
                patch("sova.git.operations.push", new_callable=AsyncMock),
                patch("sova.core.steps.monitor_ci.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            ):
                # routed_task_type_model simulates what the real
                # dispatch_prompt() (mocked out here) would set on an attempt
                # whose configured route survived the compatibility check
                # (issue #780 review); attempt 2 never routes regardless.
                mock_invoke.return_value = LLMResult(
                    text="Fixed", model="opus", cost_usd=Decimal("0.50"), routed_task_type_model="haiku"
                )
                mock_run.side_effect = _shell_side_effect
                result = await step.execute(ctx)

        assert result.success
        assert mock_invoke.await_count == 2
        first_call, second_call = mock_invoke.call_args_list
        assert first_call.kwargs["task_type"] == "monitor_ci"
        assert second_call.kwargs["task_type"] is None
        assert first_call.kwargs["model"] == second_call.kwargs["model"]
        assert "escalated to dev model" in ctx.step_model_selection_reason
        assert ctx.model_selection_reason is None

    async def test_ci_fails_all_attempts_exhausted(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        config = ProjectConfig()
        config.ci.max_fix_attempts = 2
        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt", config=config)
        step = MonitorCIStep()

        failed_check = _make_ci_check(
            "Tests", passed=False, details_url="https://github.com/o/r/actions/runs/123/job/456"
        )

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock) as mock_logs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.return_value = [failed_check]
            mock_logs.return_value = "ERROR: persistent failure"

            with (
                patch("sova.core.steps.validate.find_pre_push_hook", new_callable=AsyncMock, return_value=None),
                patch("sova.git.operations.push", new_callable=AsyncMock),
                patch("sova.core.steps.monitor_ci.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            ):
                mock_invoke.return_value = LLMResult(text="Tried fix", model="opus", cost_usd=Decimal("0.50"))
                mock_run.side_effect = _shell_side_effect
                result = await step.execute(ctx)

        assert not result.success
        assert "2 fix attempt" in result.summary
        assert "Tests" in result.summary
        assert mock_invoke.await_count == 2
        # Issue #780 review: every fix attempt's cost must be carried on the
        # final StepResult, or the CostRecord split in workflow.py's
        # provider_cost = max(result.cost_usd - routed.cost_usd, 0) silently
        # floors the dev-model attempt's spend to $0 on the most common
        # failure exit (attempts exhausted).
        assert result.cost_usd == Decimal("1.00")

    async def test_llm_timeout_produces_distinct_marker(self, tmp_path: Path) -> None:
        """A CI fix-LLM timeout must be tagged distinctly from a generic
        RuntimeError and from the unrelated step_hard_timeout string (issue #977)."""
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.errors import LLMTimeoutError

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt")
        step = MonitorCIStep()

        failed_check = _make_ci_check(
            "Tests", passed=False, details_url="https://github.com/o/r/actions/runs/123/job/456"
        )

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock) as mock_logs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.return_value = [failed_check]
            mock_logs.return_value = "ERROR: persistent failure"

            with (
                patch("sova.core.steps.validate.find_pre_push_hook", new_callable=AsyncMock, return_value=None),
                patch("sova.git.operations.push", new_callable=AsyncMock),
                patch(
                    "sova.core.steps.monitor_ci.dispatch_prompt",
                    new_callable=AsyncMock,
                    side_effect=LLMTimeoutError("Command timed out after 180s"),
                ),
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            ):
                mock_run.side_effect = _shell_side_effect
                result = await step.execute(ctx)

        assert not result.success
        assert result.error.startswith("fix_llm_timeout on cycle 1:")
        assert result.error != "step_hard_timeout"

    async def test_low_budget_fraction_stops_retry_and_fails(self, tmp_path: Path) -> None:
        """Below the 20% threshold, CI fix retries stop and the step reports
        failure: CI is still red, so returning success=True here would let
        WorkflowEngine advance past a broken build."""
        from sova.config.models import AgentConfig
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig(agent=AgentConfig(max_budget=Decimal("10.00")))
        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt", config=config)
        ctx.cost_usd = Decimal("8.50")  # 15% remaining, below 20% threshold
        step = MonitorCIStep()

        failed_check = _make_ci_check(
            "Tests", passed=False, details_url="https://github.com/o/r/actions/runs/123/job/456"
        )

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock) as mock_logs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.return_value = [failed_check]
            mock_logs.return_value = "ERROR: persistent failure"

            with (
                patch("sova.core.steps.validate.find_pre_push_hook", new_callable=AsyncMock, return_value=None),
                patch("sova.git.operations.push", new_callable=AsyncMock),
                patch("sova.core.steps.monitor_ci.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            ):
                mock_run.side_effect = _shell_side_effect
                result = await step.execute(ctx)

        assert not result.success
        assert "stopping retries" in result.summary
        assert "Tests" in (result.error or "")
        mock_invoke.assert_not_awaited()

    async def test_ci_fix_disabled_when_zero(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.max_fix_attempts = 0
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        step = MonitorCIStep()

        failed_check = _make_ci_check("lint", passed=False)
        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
        ):
            mock_checks.return_value = [failed_check]
            result = await step.execute(ctx)

        assert not result.success
        assert "lint" in result.summary

    async def test_ci_fix_respects_budget(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt")
        ctx.cost_usd = Decimal("999")  # over budget
        step = MonitorCIStep()

        failed_check = _make_ci_check("Tests", passed=False, details_url="https://github.com/o/r/actions/runs/1/job/2")

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.return_value = [failed_check]
            result = await step.execute(ctx)

        assert not result.success
        assert "budget" in result.summary.lower()

    async def test_ci_fix_local_hook_fails(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        config = ProjectConfig()
        config.ci.max_fix_attempts = 1
        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt", config=config)
        step = MonitorCIStep()

        failed_check = _make_ci_check("Tests", passed=False, details_url="https://github.com/o/r/actions/runs/1/job/2")

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock, return_value="error"),
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.return_value = [failed_check]

            with (
                patch(
                    "sova.core.steps.validate.find_pre_push_hook",
                    new_callable=AsyncMock,
                    return_value="/hooks/pre-push",
                ),
                patch("sova.git.operations.push", new_callable=AsyncMock) as mock_push,
                patch("sova.core.steps.monitor_ci.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            ):
                mock_invoke.return_value = LLMResult(text="Fixed", model="opus", cost_usd=Decimal("0.50"))
                mock_run.side_effect = [
                    MagicMock(success=True, stdout="abc fix\n"),  # git log
                    MagicMock(success=True, stdout=""),  # ruff check (from _ensure_committed)
                    MagicMock(success=True, stdout=""),  # ruff format (from _ensure_committed)
                    MagicMock(success=True, stdout=""),  # git status (from _ensure_committed, clean)
                    MagicMock(success=False, stdout="FAIL\n", stderr="hook error"),  # pre-push fails
                ]
                result = await step.execute(ctx)

        assert not result.success
        assert "local validation" in result.summary.lower()
        mock_push.assert_not_awaited()

    async def test_ci_fix_push_fails(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt")
        step = MonitorCIStep()

        failed_check = _make_ci_check("Tests", passed=False, details_url="https://github.com/o/r/actions/runs/1/job/2")

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock, return_value="error"),
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.return_value = [failed_check]

            with (
                patch("sova.core.steps.validate.find_pre_push_hook", new_callable=AsyncMock, return_value=None),
                patch("sova.git.operations.push", new_callable=AsyncMock, side_effect=RuntimeError("push rejected")),
                patch("sova.core.steps.monitor_ci.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            ):
                mock_invoke.return_value = LLMResult(text="Fixed", model="opus", cost_usd=Decimal("0.50"))
                mock_run.side_effect = _shell_side_effect
                result = await step.execute(ctx)

        assert not result.success
        assert "push failed" in result.summary.lower()

    async def test_ci_log_fetch_graceful_failure(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt")
        step = MonitorCIStep()

        failed_check = _make_ci_check("Tests", passed=False, details_url="")
        passed_check = _make_ci_check("Tests", passed=True)

        with (
            patch.object(step, "_get_local_head_sha", new_callable=AsyncMock, return_value="abc123def456"),
            patch("sova.core.steps.monitor_ci.get_ci_checks_for_sha", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock) as mock_logs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [[failed_check], [passed_check]]
            mock_logs.return_value = ""

            with (
                patch("sova.core.steps.validate.find_pre_push_hook", new_callable=AsyncMock, return_value=None),
                patch("sova.git.operations.push", new_callable=AsyncMock),
                patch("sova.core.steps.monitor_ci.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            ):
                mock_invoke.return_value = LLMResult(text="Fixed", model="opus", cost_usd=Decimal("0.50"))
                mock_run.side_effect = _shell_side_effect
                result = await step.execute(ctx)

        assert result.success
        mock_invoke.assert_awaited_once()


# ---------------------------------------------------------------------------
# MonitorCIStep: SonarCloud coverage handling
# ---------------------------------------------------------------------------


class TestMonitorCISonarCloud:
    async def test_split_sonarcloud_checks(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        sonar = _make_ci_check("SonarCloud Code Analysis", passed=False)
        tests = _make_ci_check("Tests", passed=False)
        lint = _make_ci_check("Lint", passed=False)

        sonar_checks, other_checks = MonitorCIStep._split_sonarcloud_checks([sonar, tests, lint])
        assert len(sonar_checks) == 1
        assert sonar_checks[0].name == "SonarCloud Code Analysis"
        assert len(other_checks) == 2

    async def test_split_no_sonarcloud(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        tests = _make_ci_check("Tests", passed=False)
        sonar_checks, other_checks = MonitorCIStep._split_sonarcloud_checks([tests])
        assert sonar_checks == []
        assert len(other_checks) == 1

    async def test_fetch_coverage_prompt_returns_empty_without_sonar_checks(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        result = await MonitorCIStep._fetch_sonarcloud_coverage_prompt(ctx, [])
        assert result == ""

    async def test_fetch_coverage_prompt_returns_empty_without_config(self, tmp_path: Path) -> None:
        from sova.config.models import ExternalReviewsConfig
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig(external_reviews=ExternalReviewsConfig())
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        sonar = _make_ci_check("SonarCloud Code Analysis", passed=False)
        result = await MonitorCIStep._fetch_sonarcloud_coverage_prompt(ctx, [sonar])
        assert result == ""

    async def test_fetch_coverage_prompt_returns_empty_when_sonarcloud_not_in_tools(self, tmp_path: Path) -> None:
        from sova.config.models import ExternalReviewsConfig, SonarCloudConfig
        from sova.core.steps.monitor_ci import MonitorCIStep

        ext = ExternalReviewsConfig(
            enabled=True,
            tools=["coderabbit"],
            sonarcloud=SonarCloudConfig(project_key="org_repo"),
        )
        config = ProjectConfig(external_reviews=ext)
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        sonar = _make_ci_check("SonarCloud Code Analysis", passed=False)
        result = await MonitorCIStep._fetch_sonarcloud_coverage_prompt(ctx, [sonar])
        assert result == ""

    async def test_fetch_coverage_prompt_with_gap(self, tmp_path: Path) -> None:
        from sova.adapters.external_reviews import CoverageReport, ExternalFinding
        from sova.config.models import ExternalReviewsConfig, SonarCloudConfig
        from sova.core.steps.monitor_ci import MonitorCIStep

        ext = ExternalReviewsConfig(
            enabled=True,
            tools=["sonarcloud"],
            sonarcloud=SonarCloudConfig(project_key="org_repo"),
        )
        config = ProjectConfig(external_reviews=ext)
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        sonar = _make_ci_check("SonarCloud Code Analysis", passed=False)

        report = CoverageReport(
            coverage_pct=Decimal("65.0"),
            required_pct=Decimal("80.0"),
            findings=[
                ExternalFinding("sonarcloud-coverage", "sova/core/workflow.py", 235, "MAJOR", "Uncovered"),
            ],
        )
        with patch(
            "sova.adapters.external_reviews.fetch_sonarcloud_coverage_issues",
            new_callable=AsyncMock,
            return_value=report,
        ):
            result = await MonitorCIStep._fetch_sonarcloud_coverage_prompt(ctx, [sonar])

        assert "65.0%" in result
        assert "pytest" in result
        assert "sova/core/workflow.py:235" in result

    async def test_fetch_coverage_prompt_coverage_ok(self, tmp_path: Path) -> None:
        from sova.adapters.external_reviews import CoverageReport
        from sova.config.models import ExternalReviewsConfig, SonarCloudConfig
        from sova.core.steps.monitor_ci import MonitorCIStep

        ext = ExternalReviewsConfig(
            enabled=True,
            tools=["sonarcloud"],
            sonarcloud=SonarCloudConfig(project_key="org_repo"),
        )
        config = ProjectConfig(external_reviews=ext)
        ctx = _make_ctx(tmp_path, pr_number=10, config=config)
        sonar = _make_ci_check("SonarCloud Code Analysis", passed=False)

        report = CoverageReport(coverage_pct=Decimal("85.0"), required_pct=Decimal("80.0"), findings=[])
        with patch(
            "sova.adapters.external_reviews.fetch_sonarcloud_coverage_issues",
            new_callable=AsyncMock,
            return_value=report,
        ):
            result = await MonitorCIStep._fetch_sonarcloud_coverage_prompt(ctx, [sonar])

        assert result == ""

    async def test_invoke_fix_uses_coverage_prompt_for_sonarcloud(self, tmp_path: Path) -> None:
        """When only SonarCloud fails, _invoke_fix uses the coverage prompt."""
        from sova.adapters.external_reviews import CoverageReport
        from sova.config.models import ExternalReviewsConfig, SonarCloudConfig
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        ext = ExternalReviewsConfig(
            enabled=True,
            tools=["sonarcloud"],
            sonarcloud=SonarCloudConfig(project_key="org_repo"),
        )
        config = ProjectConfig(external_reviews=ext)
        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt", config=config)
        step = MonitorCIStep()

        sonar_check = _make_ci_check("SonarCloud Code Analysis", passed=False)
        report = CoverageReport(coverage_pct=Decimal("65.0"), required_pct=Decimal("80.0"), findings=[])

        mock_invoke = AsyncMock(return_value=LLMResult(text="Fixed", model="opus", cost_usd=Decimal("0.10")))
        mock_run = AsyncMock(side_effect=_shell_side_effect)

        with (
            patch(
                "sova.adapters.external_reviews.fetch_sonarcloud_coverage_issues",
                new_callable=AsyncMock,
                return_value=report,
            ),
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock, return_value=""),
        ):
            cost, error = await step._invoke_fix(ctx, [sonar_check], mock_invoke, mock_run, 1)

        assert error is None
        prompt_arg = mock_invoke.call_args[0][0]
        assert "65.0%" in prompt_arg
        assert "pytest" in prompt_arg

    async def test_invoke_fix_combined_sonar_and_other_failures(self, tmp_path: Path) -> None:
        """When both SonarCloud and other checks fail, logs are fetched for non-Sonar checks only."""
        from sova.adapters.external_reviews import CoverageReport
        from sova.config.models import ExternalReviewsConfig, SonarCloudConfig
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        ext = ExternalReviewsConfig(
            enabled=True,
            tools=["sonarcloud"],
            sonarcloud=SonarCloudConfig(project_key="org_repo"),
        )
        config = ProjectConfig(external_reviews=ext)
        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test", worktree_dir=tmp_path / "wt", config=config)
        step = MonitorCIStep()

        sonar = _make_ci_check("SonarCloud Code Analysis", passed=False)
        tests = _make_ci_check("Tests", passed=False)
        report = CoverageReport(coverage_pct=Decimal("65.0"), required_pct=Decimal("80.0"), findings=[])

        mock_invoke = AsyncMock(return_value=LLMResult(text="Fixed", model="opus", cost_usd=Decimal("0.10")))
        mock_run = AsyncMock(side_effect=_shell_side_effect)

        with (
            patch(
                "sova.adapters.external_reviews.fetch_sonarcloud_coverage_issues",
                new_callable=AsyncMock,
                return_value=report,
            ),
            patch(
                "sova.core.steps.monitor_ci.get_ci_failure_logs",
                new_callable=AsyncMock,
                return_value="test failure logs",
            ) as mock_logs,
        ):
            cost, error = await step._invoke_fix(ctx, [sonar, tests], mock_invoke, mock_run, 1)

        assert error is None
        mock_logs.assert_called_once()
        log_checks = mock_logs.call_args[0][0]
        assert len(log_checks) == 1
        assert log_checks[0].name == "Tests"
        prompt_arg = mock_invoke.call_args[0][0]
        assert "65.0%" in prompt_arg
        assert "test failure logs" in prompt_arg
