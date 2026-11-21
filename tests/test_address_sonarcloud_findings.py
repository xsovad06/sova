"""Tests for the address_sonarcloud_findings step (SonarCloud issues raised after CI passed)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.adapters.external_reviews import ExternalFinding
from sova.config.models import CIConfig, ExternalReviewsConfig, ProjectConfig, SonarCloudConfig
from sova.core.context import ExecutionContext
from sova.core.steps.address_sonarcloud_findings import AddressSonarCloudFindingsStep, SonarAnalysis
from sova.core.steps.base import StepResult
from sova.db.models import TaskRun
from sova.db.session import get_session
from sova.git.operations import UnknownCommitError
from sova.git.pr import CheckConclusion, CheckStatus, CICheck

pytestmark = pytest.mark.usefixtures("reset_db_rows")

_MODULE = "sova.core.steps.address_sonarcloud_findings"
_SONAR = "SonarCloud Code Analysis"
_HEAD = "9886871b2455dee2c36ddd26516aa7e5e8686d8c"


def _ext(**kwargs: Any) -> ExternalReviewsConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "tools": ["sonarcloud", "coderabbit"],
        "poll_interval": 30,
        "timeout": 15,
        "sonarcloud": SonarCloudConfig(project_key="org_repo"),
    }
    defaults.update(kwargs)
    return ExternalReviewsConfig(**defaults)


def _ctx(ext: ExternalReviewsConfig | None = None, *, grace: int = 0, **kwargs: Any) -> ExecutionContext:
    adapter = AsyncMock()
    adapter.get_state.return_value = TaskState.RESEARCHED
    adapter.get_task.return_value = Task(id="1", title="Test issue")
    config = ProjectConfig(
        github_repo="owner/repo",
        external_reviews=ext or _ext(),
        ci=CIConfig(no_checks_grace_period=grace),
    )
    defaults: dict[str, Any] = {
        "project_dir": Path("/nonexistent/sova-test-project"),
        "config": config,
        "adapter": adapter,
        "issue_number": "42",
        "role": "developer",
        "pr_number": 94,
        "branch_name": "feat/test",
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


def _check(name: str, *, done: bool = True, conclusion: CheckConclusion = CheckConclusion.SUCCESS) -> CICheck:
    return CICheck(
        name=name,
        status=CheckStatus.COMPLETED if done else CheckStatus.IN_PROGRESS,
        conclusion=conclusion if done else None,
        details_url="",
    )


def _finding(key: str, line: int = 10) -> ExternalFinding:
    return ExternalFinding("sonarcloud", "tests/test_x.py", line, "MAJOR", "Split this composite assertion.", key)


@contextmanager
def _patched(
    *,
    checks: list[list[CICheck] | None] | None = None,
    findings: list[ExternalFinding] | None = None,
    pushed: bool = True,
    watch: StepResult | None = None,
) -> Iterator[dict[str, AsyncMock]]:
    """Patch the step's GitHub, SonarCloud, agent and CI collaborators.

    ``checks`` is the sequence of SHA-scoped check lists successive polls see
    (default: one poll with SonarCloud passed); ``findings`` is what
    SonarCloud's issue API returns (default: none).
    """
    polls = checks if checks is not None else [[_check("Python Tests"), _check(_SONAR)]]
    with (
        patch(f"{_MODULE}._head_sha", new_callable=AsyncMock, return_value=_HEAD),
        patch(f"{_MODULE}.get_ci_checks_for_sha", new_callable=AsyncMock, side_effect=polls) as get_checks,
        patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch(
            f"{_MODULE}.fetch_sonarcloud_issues",
            new_callable=AsyncMock,
            return_value=[] if findings is None else findings,
        ) as fetch,
        patch.object(
            AddressSonarCloudFindingsStep,
            "_apply_fixes",
            new_callable=AsyncMock,
            return_value=(Decimal("0.10"), pushed, pushed, pushed),
        ) as apply_fixes,
        patch(
            f"{_MODULE}.MonitorCIStep.watch",
            new_callable=AsyncMock,
            return_value=watch or StepResult(success=True, summary="All 14 CI checks passed"),
        ) as ci_watch,
    ):
        yield {"get_checks": get_checks, "sleep": sleep, "fetch": fetch, "apply_fixes": apply_fixes, "watch": ci_watch}


async def _seed_handoff(pr_number: int, findings: list[dict]) -> None:
    async with await get_session() as session:
        session.add(
            TaskRun(
                issue_number="42",
                role="developer",
                status="done",
                pr_number=pr_number,
                handoff_json={"addressed_findings": findings},
            )
        )
        await session.commit()


class TestCanSkip:
    @pytest.mark.parametrize(
        "ext",
        [
            _ext(enabled=False),
            _ext(tools=["coderabbit"]),
            _ext(sonarcloud=SonarCloudConfig(project_key="")),
            _ext(tools=[]),
        ],
        ids=["disabled", "not-listed", "no-project-key", "no-tools"],
    )
    async def test_skips_when_sonarcloud_is_not_set_up(self, ext: ExternalReviewsConfig) -> None:
        assert await AddressSonarCloudFindingsStep().can_skip(_ctx(ext)) is True

    async def test_runs_when_sonarcloud_is_set_up(self) -> None:
        assert await AddressSonarCloudFindingsStep().can_skip(_ctx()) is False

    async def test_skips_once_completed(self) -> None:
        ctx = _ctx(completed_steps=frozenset({"address_sonarcloud_findings"}))
        assert await AddressSonarCloudFindingsStep().can_skip(ctx) is True


class TestWaitForAnalysis:
    async def test_missing_check_after_the_rest_settle_reads_as_absent(self) -> None:
        """A repository that never runs SonarCloud must not hold the run for the whole timeout."""
        with _patched(checks=[[_check("Python Tests")]]) as mocks:
            result = await AddressSonarCloudFindingsStep().execute(_ctx(grace=0))

        assert result.success is True
        assert "did not report" in result.summary
        mocks["fetch"].assert_not_awaited()
        mocks["apply_fixes"].assert_not_awaited()

    async def test_an_empty_check_list_is_not_a_settled_commit(self) -> None:
        """GitHub briefly lists no checks before the suites register; that must not start the grace clock."""
        from sova.core.steps.address_sonarcloud_findings import _read_checks

        assert _read_checks([], _SONAR) == (None, False)
        with _patched(checks=[[], [_check("Python Tests")]]) as mocks:
            result = await AddressSonarCloudFindingsStep().execute(_ctx(grace=0))

        assert mocks["get_checks"].await_count == 2
        assert "did not report" in result.summary

    async def test_keeps_waiting_while_other_checks_run(self) -> None:
        """SonarCloud posts its check only after its own scan, which starts once CI is done."""
        polls = [
            [_check("Python Tests"), _check("SonarCloud Analysis", done=False)],
            [_check("Python Tests"), _check("SonarCloud Analysis"), _check(_SONAR, done=False)],
            [_check("Python Tests"), _check("SonarCloud Analysis"), _check(_SONAR)],
        ]
        with _patched(checks=polls) as mocks:
            analysis = await AddressSonarCloudFindingsStep()._wait_for_analysis(_ctx(grace=0))

        assert analysis == SonarAnalysis.PASSED
        assert mocks["get_checks"].await_count == 3

    async def test_failed_lookup_is_retried(self) -> None:
        with _patched(checks=[None, [_check(_SONAR)]]):
            analysis = await AddressSonarCloudFindingsStep()._wait_for_analysis(_ctx())

        assert analysis == SonarAnalysis.PASSED

    async def test_failed_check_reads_as_failed(self) -> None:
        with _patched(checks=[[_check(_SONAR, conclusion=CheckConclusion.FAILURE)]]):
            analysis = await AddressSonarCloudFindingsStep()._wait_for_analysis(_ctx())

        assert analysis == SonarAnalysis.FAILED

    async def test_check_still_running_at_the_deadline_times_out(self) -> None:
        clock = iter([0.0, 0.0, 10_000.0])
        with (
            _patched(checks=[[_check(_SONAR, done=False)]] * 2),
            patch(f"{_MODULE}.time.monotonic", side_effect=lambda: next(clock)),
        ):
            analysis = await AddressSonarCloudFindingsStep()._wait_for_analysis(_ctx())

        assert analysis == SonarAnalysis.TIMED_OUT

    async def test_unknown_commit(self) -> None:
        with _patched(checks=[]) as mocks:
            mocks["get_checks"].side_effect = UnknownCommitError("gone")
            analysis = await AddressSonarCloudFindingsStep()._wait_for_analysis(_ctx())

        assert analysis == SonarAnalysis.UNKNOWN

    async def test_unresolvable_head(self) -> None:
        with _patched() as mocks, patch(f"{_MODULE}._head_sha", new_callable=AsyncMock, return_value=""):
            analysis = await AddressSonarCloudFindingsStep()._wait_for_analysis(_ctx())

        assert analysis == SonarAnalysis.UNKNOWN
        mocks["get_checks"].assert_not_awaited()


class TestExecute:
    async def test_new_findings_are_fixed_and_ci_is_watched_again(self) -> None:
        ctx = _ctx()
        with _patched(findings=[_finding("AZ1"), _finding("AZ2", line=20)]) as mocks:
            result = await AddressSonarCloudFindingsStep().execute(ctx)

        assert result.success is True
        prompt = mocks["apply_fixes"].await_args.args[1]
        assert "tests/test_x.py:10" in prompt
        assert "tests/test_x.py:20" in prompt
        mocks["watch"].assert_awaited_once()
        assert "Addressed 2 SonarCloud finding(s)" in result.summary
        assert {f["tool_id"] for f in ctx.addressed_external_findings} == {"AZ1", "AZ2"}

    async def test_findings_this_run_already_handed_over_are_not_repeated(self) -> None:
        ctx = _ctx()
        ctx.addressed_external_findings = [
            {"source": "sonarcloud", "severity": "MAJOR", "file_path": "a.py", "tool_id": "AZ1", "message": "m"},
        ]
        with _patched(findings=[_finding("AZ1"), _finding("AZ2")]) as mocks:
            await AddressSonarCloudFindingsStep().execute(ctx)

        prompt = mocks["apply_fixes"].await_args.args[1]
        assert "(1 findings)" in prompt
        assert [f["tool_id"] for f in ctx.addressed_external_findings] == ["AZ1", "AZ2"]

    async def test_findings_an_earlier_run_on_this_pr_handed_over_are_not_repeated(self) -> None:
        await _seed_handoff(94, [{"source": "sonarcloud", "tool_id": "AZ1"}])
        # Another PR's handoff, and a CodeRabbit finding sharing the key, must not filter anything.
        await _seed_handoff(95, [{"source": "sonarcloud", "tool_id": "AZ2"}])
        await _seed_handoff(94, [{"source": "coderabbit", "tool_id": "AZ3"}])

        with _patched(findings=[_finding("AZ1"), _finding("AZ2"), _finding("AZ3")]) as mocks:
            await AddressSonarCloudFindingsStep().execute(_ctx())

        prompt = mocks["apply_fixes"].await_args.args[1]
        assert "(2 findings)" in prompt

    async def test_no_new_findings_after_a_passed_analysis_does_nothing(self) -> None:
        await _seed_handoff(94, [{"source": "sonarcloud", "tool_id": "AZ1"}])
        with _patched(findings=[_finding("AZ1")]) as mocks:
            result = await AddressSonarCloudFindingsStep().execute(_ctx())

        assert result.success is True
        assert result.summary == "No new SonarCloud findings after CI passed"
        mocks["apply_fixes"].assert_not_awaited()
        mocks["watch"].assert_not_awaited()

    async def test_failed_fetch_is_reported_not_read_as_clean(self) -> None:
        with _patched() as mocks:
            mocks["fetch"].return_value = None
            result = await AddressSonarCloudFindingsStep().execute(_ctx())

        assert result.success is True
        assert "Could not read SonarCloud issues" in result.summary
        mocks["apply_fixes"].assert_not_awaited()

    async def test_failed_quality_gate_with_no_issues_goes_to_the_ci_fix_loop(self) -> None:
        checks = [[_check(_SONAR, conclusion=CheckConclusion.FAILURE)]]
        with _patched(checks=checks) as mocks:
            result = await AddressSonarCloudFindingsStep().execute(_ctx())

        mocks["apply_fixes"].assert_not_awaited()
        mocks["watch"].assert_awaited_once()
        assert result.success is True

    async def test_red_ci_after_the_fix_fails_the_step(self) -> None:
        red = StepResult(success=False, summary="CI failed: Python Tests", error="Failed checks: Python Tests")
        with _patched(findings=[_finding("AZ1")], watch=red):
            result = await AddressSonarCloudFindingsStep().execute(_ctx())

        assert result.success is False
        assert result.error == "Failed checks: Python Tests"

    async def test_no_push_skips_the_ci_watch(self) -> None:
        """An agent that judged every finding a false positive changed nothing CI needs to re-check."""
        with _patched(findings=[_finding("AZ1")], pushed=False) as mocks:
            result = await AddressSonarCloudFindingsStep().execute(_ctx())

        assert result.success is True
        assert "no changes pushed" in result.summary
        mocks["watch"].assert_not_awaited()

    async def test_partial_fix_that_reached_the_pr_is_watched_and_reported_as_partial(self) -> None:
        from sova.core.steps.address_external_findings import _DispatchFailedButPreserved

        with _patched(findings=[_finding("AZ1")]) as mocks:
            mocks["apply_fixes"].side_effect = _DispatchFailedButPreserved(RuntimeError("agent crashed"))
            result = await AddressSonarCloudFindingsStep().execute(_ctx())

        assert result.success is True
        assert result.summary.startswith("Partially addressed")
        mocks["watch"].assert_awaited_once()

    async def test_dispatch_failure_fails_the_step(self) -> None:
        with _patched(findings=[_finding("AZ1")]) as mocks:
            mocks["apply_fixes"].side_effect = RuntimeError("agent crashed")
            result = await AddressSonarCloudFindingsStep().execute(_ctx())

        assert result.success is False
        assert "agent crashed" in (result.error or "")
        mocks["watch"].assert_not_awaited()


class TestRetry:
    """A workflow retry runs execute() again on the same ctx."""

    async def test_retry_after_a_dispatch_failure_hands_the_findings_over_again(self) -> None:
        ctx = _ctx()
        polls = [[_check(_SONAR)], [_check(_SONAR)]]
        with _patched(checks=polls, findings=[_finding("AZ1")]) as mocks:
            mocks["apply_fixes"].side_effect = [RuntimeError("agent crashed"), (Decimal("0.10"), True, True, True)]
            first = await AddressSonarCloudFindingsStep().execute(ctx)
            assert ctx.addressed_external_findings == []
            second = await AddressSonarCloudFindingsStep().execute(ctx)

        assert first.success is False
        assert second.success is True
        assert second.summary.startswith("Addressed 1 SonarCloud finding(s)")
        assert mocks["apply_fixes"].await_count == 2

    async def test_retry_after_a_red_watch_watches_ci_again_instead_of_waiting_for_sonarcloud(self) -> None:
        """SonarCloud never analyses a red head: a retry that waited for it would read ABSENT and pass."""
        ctx = _ctx()
        red = StepResult(success=False, summary="CI failed: Python Tests", error="Failed checks: Python Tests")
        # The retry would see no SonarCloud check at all, the shape of a red head.
        polls = [[_check(_SONAR)], [_check("Python Tests")]]
        with _patched(checks=polls, findings=[_finding("AZ1")]) as mocks:
            mocks["watch"].side_effect = [red, red]
            first = await AddressSonarCloudFindingsStep().execute(ctx)
            assert ctx.sonarcloud_unverified_head == _HEAD
            second = await AddressSonarCloudFindingsStep().execute(ctx)

        assert first.success is False
        assert second.success is False
        assert second.error == "Failed checks: Python Tests"
        assert mocks["watch"].await_count == 2
        assert mocks["get_checks"].await_count == 1
        assert mocks["apply_fixes"].await_count == 1

    async def test_green_watch_clears_the_unverified_head(self) -> None:
        ctx = _ctx()
        with _patched(findings=[_finding("AZ1")]):
            result = await AddressSonarCloudFindingsStep().execute(ctx)

        assert result.success is True
        assert ctx.sonarcloud_unverified_head == ""

    async def test_a_head_that_moved_since_the_marker_waits_for_sonarcloud_again(self) -> None:
        ctx = _ctx(sonarcloud_unverified_head="0" * 40)
        with _patched() as mocks:
            result = await AddressSonarCloudFindingsStep().execute(ctx)

        assert result.success is True
        mocks["watch"].assert_not_awaited()
        mocks["get_checks"].assert_awaited_once()


class TestFailedGateReachesTheRealCiWatch:
    async def test_the_failed_sonarcloud_check_is_handed_to_the_ci_fix_loop(self) -> None:
        """Runs MonitorCIStep.watch()'s own poll: only the fix loop and the GitHub lookups are patched."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        failed = _check(_SONAR, conclusion=CheckConclusion.FAILURE)
        fix_failed = StepResult(success=False, summary="CI still failing: " + _SONAR, error="Failed checks")
        monitor = "sova.core.steps.monitor_ci"
        with (
            patch(f"{_MODULE}._head_sha", new_callable=AsyncMock, return_value=_HEAD),
            patch(f"{_MODULE}.get_ci_checks_for_sha", new_callable=AsyncMock, return_value=[failed]),
            patch(f"{_MODULE}.fetch_sonarcloud_issues", new_callable=AsyncMock, return_value=[]),
            patch(f"{monitor}.get_ci_checks_for_sha", new_callable=AsyncMock, return_value=[failed]),
            patch.object(MonitorCIStep, "_get_local_head_sha", new_callable=AsyncMock, return_value=_HEAD),
            patch.object(MonitorCIStep, "_find_pr_head_mismatch", new_callable=AsyncMock, return_value=None),
            patch.object(MonitorCIStep, "_try_fix_ci", new_callable=AsyncMock, return_value=fix_failed) as fix,
        ):
            result = await AddressSonarCloudFindingsStep().execute(_ctx())

        assert result.success is False
        fix.assert_awaited_once()
        assert [c.name for c in fix.await_args.args[1]] == [_SONAR]


class TestPipelinePlacement:
    def test_runs_after_monitor_ci_in_both_pipelines(self) -> None:
        from sova.core.steps import get_address_review_step_names, get_developer_step_names

        for names in (get_developer_step_names(), get_address_review_step_names()):
            assert names.index("address_sonarcloud_findings") == names.index("monitor_ci") + 1

    def test_step_timeout_covers_wait_fix_and_ci_watch(self, tmp_path: Path) -> None:
        from sova.core.workflow import WorkflowEngine

        ctx = _ctx(project_dir=tmp_path)
        engine = WorkflowEngine(steps=[], ctx=ctx)
        agent = engine._step_timeout("confidence_score")
        expected = 15 * 60 + agent + ctx.config.ci.max_wait + 120
        assert engine._step_timeout("address_sonarcloud_findings") == expected
