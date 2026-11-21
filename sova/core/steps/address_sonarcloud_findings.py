"""Step: address SonarCloud findings once CI has passed.

SonarCloud analyses a pull request only after its CI passes, since its scan
reuses the coverage report CI produces. ``address_external_findings`` runs
before ``monitor_ci``, so on a PR whose first push is red it finds no
SonarCloud issues at all, and the issues SonarCloud raises once CI is fixed
reached no agent (PR #1526 shipped 15 of them that way).

This step runs after ``monitor_ci``. It waits for SonarCloud's check on the
head commit, fetches the PR's open SonarCloud issues, gives the agent the ones
no run on this PR has handed over already, pushes the fix, and watches CI
again. It skips itself unless SonarCloud is set up for the project
(``external_reviews.sonarcloud_configured``), and when SonarCloud never
reports on the head commit it proceeds without waiting out the full timeout.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from sova.adapters.external_reviews import _CHECK_NAMES, fetch_sonarcloud_issues, format_findings_for_prompt
from sova.core.context import ExecutionContext
from sova.core.steps.address_external_findings import (
    AddressExternalFindingsStep,
    ExternalFindingDict,
    _DispatchFailedButPreserved,
)
from sova.core.steps.base import StepResult
from sova.core.steps.monitor_ci import MonitorCIStep
from sova.db.models import TaskRun
from sova.db.session import get_session
from sova.git.operations import CICheck, UnknownCommitError, get_ci_checks_for_sha
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.address_sonarcloud_findings")

SONARCLOUD_SOURCE = "sonarcloud"


class SonarAnalysis(StrEnum):
    """What SonarCloud's check on the head commit showed by the end of the wait."""

    PASSED = "passed"
    FAILED = "failed"
    # Every other check on the commit finished and no SonarCloud check appeared
    # within the grace period: the repository does not run SonarCloud, or did
    # not run it for this commit.
    ABSENT = "absent"
    # SonarCloud's check appeared but was still running at the timeout.
    TIMED_OUT = "timed_out"
    # The head commit could not be resolved or GitHub has no record of it.
    UNKNOWN = "unknown"


class AddressSonarCloudFindingsStep(AddressExternalFindingsStep):
    """Fix SonarCloud issues raised after CI passed, then re-watch CI."""

    name = "address_sonarcloud_findings"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if ctx.pr_number is None:
            return StepResult(success=False, summary="No PR to address", error="pr_number is None")

        cost_before = ctx.cost_usd
        if ctx.sonarcloud_unverified_head and ctx.sonarcloud_unverified_head == await _head_sha(ctx):
            # A retry after this step pushed a fix whose CI watch did not pass. SonarCloud
            # never analyses a red head, so waiting for it would read "did not report" and
            # turn red CI into a success: watch CI on that head again instead.
            log.info("step.address_sonarcloud_findings.rewatch_unverified_head", pr=ctx.pr_number)
            return await self._watch_ci(ctx, "Fix pushed earlier", cost_before)

        analysis = await self._wait_for_analysis(ctx)
        if analysis in (SonarAnalysis.ABSENT, SonarAnalysis.TIMED_OUT, SonarAnalysis.UNKNOWN):
            log.warning("step.address_sonarcloud_findings.no_analysis", pr=ctx.pr_number, analysis=str(analysis))
            return StepResult(success=True, summary=_NO_ANALYSIS_SUMMARY[analysis])

        ext = ctx.config.external_reviews
        findings = await fetch_sonarcloud_issues(ext.sonarcloud.project_key, ctx.pr_number)
        if findings is None:
            log.warning("step.address_sonarcloud_findings.fetch_failed", pr=ctx.pr_number)
            return StepResult(success=True, summary="Could not read SonarCloud issues; skipped")
        handled = await self._handled_tool_ids(ctx)
        new_findings = [f for f in findings if not f.tool_id or f.tool_id not in handled]
        log.info(
            "step.address_sonarcloud_findings.fetched",
            pr=ctx.pr_number,
            open=len(findings),
            new=len(new_findings),
            analysis=str(analysis),
        )

        pushed = partial = False
        if new_findings:
            handed_over = [
                ExternalFindingDict(
                    source=f.source,
                    severity=f.severity,
                    file_path=f.file_path,
                    tool_id=f.tool_id,
                    message=f.message,
                )
                for f in new_findings
            ]
            try:
                _, _, _, pushed = await self._apply_fixes(ctx, format_findings_for_prompt(new_findings))
            except _DispatchFailedButPreserved as exc:
                log.error("step.address_sonarcloud_findings.dispatch_failed_partial_preserved", error=str(exc.original))
                pushed = partial = True
            except RuntimeError as exc:
                # Not recorded as handled: a retry must hand the same findings to the agent again.
                log.error("step.address_sonarcloud_findings.dispatch_failed", error=str(exc))
                return StepResult(
                    success=False,
                    summary=f"Failed to address SonarCloud findings: {exc}",
                    error=str(exc),
                    cost_usd=ctx.cost_usd - cost_before,
                )
            ctx.addressed_external_findings = [*ctx.addressed_external_findings, *handed_over]

        if not new_findings and analysis == SonarAnalysis.PASSED:
            return StepResult(success=True, summary="No new SonarCloud findings after CI passed")

        # A pushed fix needs CI and SonarCloud to pass again on the new head, and a failed
        # SonarCloud check (a quality gate that reported after monitor_ci finished) needs
        # monitor_ci's fix loop, whose CI poll sees SonarCloud's check unless the operator
        # listed it in ci.exclude_checks.
        if pushed or analysis == SonarAnalysis.FAILED:
            if pushed:
                ctx.sonarcloud_unverified_head = await _head_sha(ctx)
            return await self._watch_ci(ctx, _findings_summary(new_findings, pushed, partial=partial), cost_before)

        return StepResult(
            success=True,
            summary=_findings_summary(new_findings, pushed, partial=partial),
            cost_usd=ctx.cost_usd - cost_before,
        )

    async def _watch_ci(self, ctx: ExecutionContext, prefix: str, cost_before: Decimal) -> StepResult:
        """Watch CI on the head, running monitor_ci's fix loop; green clears the unverified-head marker."""
        ci_result = await MonitorCIStep().watch(ctx)
        if ci_result.success:
            ctx.sonarcloud_unverified_head = ""
        return StepResult(
            success=ci_result.success,
            summary=f"{prefix}; {ci_result.summary}",
            error=ci_result.error,
            cost_usd=ctx.cost_usd - cost_before,
        )

    async def _wait_for_analysis(self, ctx: ExecutionContext) -> SonarAnalysis:
        """Wait for SonarCloud's check on the working tree's HEAD to complete.

        SonarCloud posts its check only when its own analysis finishes, which
        can be after every other check on the commit is done, so a missing
        check is only read as "SonarCloud does not run here" once the rest of
        the commit's checks have all been complete for
        ``ci.no_checks_grace_period`` seconds. A check that appeared waits up
        to ``external_reviews.timeout`` minutes. A failed lookup is retried
        until that same deadline.
        """
        head = await _head_sha(ctx)
        if not head:
            return SonarAnalysis.UNKNOWN

        check_name = _CHECK_NAMES[SONARCLOUD_SOURCE]
        ext = ctx.config.external_reviews
        grace = ctx.config.ci.no_checks_grace_period
        deadline = time.monotonic() + ext.timeout * 60
        settled_since: float | None = None

        while True:
            try:
                checks = await get_ci_checks_for_sha(head, repo=ctx.repo, github_user=ctx.config.github_user)
            except UnknownCommitError:
                log.warning("step.address_sonarcloud_findings.unknown_commit", sha=head[:8])
                return SonarAnalysis.UNKNOWN

            if checks is not None:
                verdict, settled = _read_checks(checks, check_name)
                if verdict is not None:
                    return verdict
                settled_since = _settle_clock(settled_since, settled)
                if settled_since is not None and time.monotonic() - settled_since >= grace:
                    return SonarAnalysis.ABSENT

            if time.monotonic() >= deadline:
                return SonarAnalysis.TIMED_OUT
            await asyncio.sleep(ext.poll_interval)

    async def _handled_tool_ids(self, ctx: ExecutionContext) -> set[str]:
        """SonarCloud issue keys an agent was already given on this PR.

        Covers this run (``address_external_findings`` may have handed some
        over before CI) and every earlier run on the PR, from the
        ``addressed_findings`` each one wrote to its handoff. An issue still
        open after that was judged a false positive or left on purpose, so
        handing it over again on every address-review cycle only spends
        money. A failed lookup falls back to this run's own findings and is
        logged: the cost is a repeated finding, not a lost one.
        """
        ids = _sonar_ids(ctx.addressed_external_findings)
        try:
            async with await get_session() as session:
                stmt = select(TaskRun.handoff_json).where(
                    TaskRun.pr_number == ctx.pr_number,
                    TaskRun.handoff_json.isnot(None),
                )
                for (handoff,) in await session.execute(stmt):
                    if isinstance(handoff, dict):
                        ids |= _sonar_ids(handoff.get("addressed_findings") or [])
        except (OSError, RuntimeError, SQLAlchemyError):
            log.warning("step.address_sonarcloud_findings.history_lookup_failed", pr=ctx.pr_number, exc_info=True)
        return ids

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        if self.name in ctx.completed_steps:
            return True
        return not ctx.config.external_reviews.sonarcloud_configured


_NO_ANALYSIS_SUMMARY: dict[SonarAnalysis, str] = {
    SonarAnalysis.ABSENT: "SonarCloud did not report on the head commit; skipped",
    SonarAnalysis.TIMED_OUT: "SonarCloud analysis still running at the timeout; skipped",
    SonarAnalysis.UNKNOWN: "Could not resolve the head commit to check SonarCloud; skipped",
}


def _read_checks(checks: list[CICheck], check_name: str) -> tuple[SonarAnalysis | None, bool]:
    """Read one poll of the head commit's checks.

    Returns SonarCloud's verdict once its check has completed (else ``None``),
    and whether the commit has settled without it: no SonarCloud check, and
    every other check complete.
    """
    sonar = next((c for c in checks if c.name == check_name), None)
    if sonar is None:
        # An empty list is GitHub not having registered the commit's checks yet, not a settled commit.
        return None, bool(checks) and all(c.is_completed for c in checks)
    if sonar.is_completed:
        return (SonarAnalysis.FAILED if sonar.is_failed else SonarAnalysis.PASSED), False
    return None, False


def _settle_clock(settled_since: float | None, settled: bool) -> float | None:
    """Start the grace clock on the first settled poll, keep it while settled, and reset it otherwise."""
    if not settled:
        return None
    return settled_since if settled_since is not None else time.monotonic()


def _sonar_ids(findings: list) -> set[str]:
    """Return the SonarCloud issue keys in a list of serialized findings."""
    return {
        str(f["tool_id"])
        for f in findings
        if isinstance(f, dict) and f.get("source") == SONARCLOUD_SOURCE and f.get("tool_id")
    }


def _findings_summary(findings: list, pushed: bool, *, partial: bool = False) -> str:
    if not findings:
        return "SonarCloud check failed with no new issues"
    if partial:
        verb = "Partially addressed (agent failed, edits pushed)"
    else:
        verb = "Addressed" if pushed else "Reviewed (no changes pushed)"
    return f"{verb} {len(findings)} SonarCloud finding(s) raised after CI"


async def _head_sha(ctx: ExecutionContext) -> str:
    """Return the working tree's HEAD SHA, or ``""`` when it cannot be read."""
    try:
        result = await run("git", "rev-parse", "HEAD", cwd=ctx.working_dir)
    except OSError:
        log.warning("step.address_sonarcloud_findings.head_sha_failed", cwd=str(ctx.working_dir), exc_info=True)
        return ""
    return result.stdout.strip() if result.success else ""
