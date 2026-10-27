"""Step 8: Monitor CI. Poll CI checks until they complete.

When CI fails, optionally invokes Claude to fix the issue and re-pushes,
looping up to ``ci.max_fix_attempts`` times before giving up. Set
``ci.max_fix_attempts = 0`` in SOVA config to disable auto-recovery (the
pre-existing behaviour).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from sova.core.agent_dispatch import dispatch_prompt
from sova.core.context import BUDGET_STOP_RETRY_THRESHOLD, ExecutionContext
from sova.core.steps.base import (
    BaseStep,
    GateCheckResult,
    StepResult,
    fix_attempt_routed_reason,
    resolve_fix_attempt_route,
)
from sova.git.operations import (
    CheckConclusion,
    CICheck,
    UnknownCommitError,
    get_ci_checks,
    get_ci_checks_for_sha,
    get_ci_failure_logs,
    get_pr_head_sha,
    sha_has_workflow_runs,
)
from sova.llm.egress import scan_and_redact
from sova.llm.errors import format_fix_llm_failure
from sova.utils.logging import get_logger

_ShellRunner = Callable[..., Any]
_LLMInvoker = Callable[..., Any]

log = get_logger(component="step.monitor_ci")

_MAX_CI_FIX_ATTEMPTS = 3
_CI_LOG_LIMIT = 12_000

# Operator-facing wording for each _pr_confirms_no_checks() non-confirmation
# reason, keyed by the reason string it returns.
_NO_CHECKS_REASON_TEXT: dict[str, str] = {
    "pr_reports_checks": "PR reports checks exist",
    "pr_lookup_failed": "PR-level check lookup failed",
    "run_without_checks": "a workflow run exists with no check yet",
    "run_lookup_unreadable": "workflow-run lookup failed",
}


def _redact_logs(text: str) -> str:
    """Strip tokens, keys, and secrets from CI log output."""
    return scan_and_redact(text).redacted_text


class MonitorCIStep(BaseStep):
    name = "monitor_ci"
    TASK_TYPE = "monitor_ci"
    max_retries = 0
    requires_tools = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if ctx.pr_number is None:
            return StepResult(success=False, summary="No PR to monitor", error="pr_number is None")

        if ctx.no_new_commits:
            skip_result = await self._check_existing_ci(ctx)
            if skip_result is not None:
                return skip_result

        if ctx.resume_run_id is not None:
            resume_result = await self._check_ci_on_resume(ctx)
            if resume_result is not None:
                return resume_result

        result, failed = await self._poll_ci(ctx)
        if result.success or not failed:
            return result

        max_attempts = min(ctx.config.ci.max_fix_attempts, _MAX_CI_FIX_ATTEMPTS)
        if max_attempts == 0:
            return result

        return await self._try_fix_ci(ctx, failed, max_attempts)

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    # ------------------------------------------------------------------
    # Fast path: no new push, check if CI already passed for current SHA
    # ------------------------------------------------------------------

    async def _check_existing_ci(self, ctx: ExecutionContext) -> StepResult | None:
        """When no new commits were produced, check if CI already passed.

        Returns a StepResult to short-circuit polling, or None to fall
        through to the normal poll loop.
        """
        log.info("step.monitor_ci.no_new_commits", pr=ctx.pr_number)
        try:
            checks = await get_ci_checks(
                ctx.pr_number,
                repo=ctx.repo,
                github_user=ctx.config.github_user,
            )
        except (RuntimeError, OSError):
            log.warning("step.monitor_ci.existing_check_failed", exc_info=True)
            return None

        if checks is None:
            return None

        if not checks:
            # An empty list is ambiguous between "no CI configured" and "gh
            # reported no checks because they haven't registered yet"
            # (see get_ci_checks()'s docstring). Falling through to
            # _poll_ci() applies the grace period and the two-source
            # cross-check (_pr_confirms_no_checks) instead of passing on an
            # uncorroborated empty result.
            log.info("step.monitor_ci.no_existing_checks")
            return None

        exclude = ctx.config.ci.exclude_checks
        if exclude:
            checks = [c for c in checks if not any(pat in c.name for pat in exclude)]
        if not checks:
            # Same registration-window ambiguity as the empty-list branch
            # above: this fast path can run seconds after a real push (see
            # module docstring), so an all-excluded result here is not yet
            # distinguishable from "the other checks haven't registered".
            # Fall through to _poll_ci(), whose all_excluded handling waits
            # out the grace period before passing.
            log.info("step.monitor_ci.no_existing_checks_all_excluded", patterns=exclude)
            return None

        completed = [c for c in checks if c.is_completed]
        if len(completed) < len(checks):
            return None

        failed = [c for c in completed if c.is_failed]
        if failed:
            names = ", ".join(c.name for c in failed)
            return StepResult(success=False, summary=f"CI already failed: {names}", error=f"Failed checks: {names}")

        return StepResult(success=True, summary=f"All {len(checks)} CI checks already passed (no new push)")

    # ------------------------------------------------------------------
    # CI polling (shared between initial check and post-fix re-checks)
    # ------------------------------------------------------------------

    async def _poll_ci(
        self,
        ctx: ExecutionContext,
        *,
        expected_sha: str = "",
    ) -> tuple[StepResult, list[CICheck]]:
        """Poll CI checks until completion. Returns (result, failed_checks).

        Checks are looked up for *expected_sha* (resolved from the local
        working tree's HEAD when not passed in), so a poll right after a
        push can never see a prior commit's stale, possibly-green checks.
        A SHA GitHub hasn't registered checks for yet is treated as pending:
        once the grace period elapses, an empty SHA-scoped result only
        passes the step after the cross-check confirms the PR genuinely has
        no CI configured: the PR-level lookup must also report no checks
        (through the same ``ci.exclude_checks`` filter) *and* GitHub must
        have no workflow run recorded for the commit, since the empty
        SHA-scoped result is itself ambiguous between "no CI" and "not
        registered yet" and the PR-level lookup reads the same head commit.
        A fetch failure in either half of that cross-check is never read as
        "confirmed no CI", so an outage there falls closed to continued
        polling rather than a silent pass. A SHA GitHub has no record of at
        all (both the check-runs and commit-status endpoints independently
        return 404/422; either one succeeding proves the commit exists)
        fails only when the PR's own head SHA is confirmed to be a different
        commit; a head that still matches *expected_sha*, or one that could
        not be read at all (transient gh/network failure), is not
        corroboration of a failed push, so polling continues instead. If the
        PR head moves to a different commit entirely (another actor pushed
        after this pipeline's push), the step rebinds to the new head, resets the
        no-checks grace clock (the freshly bound head has its own
        registration window), and keeps polling rather than failing the run
        over a benign event. A completed check set with nothing passing only
        fails the step when it contains a CANCELLED conclusion (the normal
        effect of a superseding push); an all-SKIPPED or all-NEUTRAL set is a
        deliberate non-blocking outcome and still passes. Falls back to the
        PR-level lookup only when the local HEAD SHA cannot be resolved; that
        fallback is not exempt from the two-source cross-check either, since
        it resolves the PR's current head via ``get_pr_head_sha`` to
        corroborate against, and keeps polling (rather than passing
        uncorroborated) when even that cannot be read.
        Elapsed time is measured from a real monotonic clock, not by
        counting polls, so extra API round trips per iteration never drift
        the loop past ``max_wait``.
        """
        poll_interval = ctx.config.ci.poll_interval
        max_wait = ctx.config.ci.max_wait
        grace_period = ctx.config.ci.no_checks_grace_period
        start_time = time.monotonic()
        grace_start = start_time
        elapsed = 0

        if not expected_sha:
            expected_sha = await self._get_local_head_sha(ctx)
            if not expected_sha:
                log.warning("step.monitor_ci.head_sha_unresolved", pr=ctx.pr_number)

        sha_short = expected_sha[:8] if expected_sha else ""
        log.info("step.monitor_ci.poll", pr=ctx.pr_number, max_wait=max_wait, expected_sha=sha_short)

        while elapsed < max_wait:
            if expected_sha:
                try:
                    checks = await get_ci_checks_for_sha(
                        expected_sha,
                        repo=ctx.repo,
                        github_user=ctx.config.github_user,
                    )
                except UnknownCommitError as exc:
                    head_sha = await get_pr_head_sha(ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user)
                    if not head_sha or head_sha == expected_sha:
                        # An unreadable head (transient gh/network failure) is "no
                        # corroboration either way", not "confirmed bad push": fail
                        # open and keep polling, mirroring _find_pr_head_mismatch.
                        reason = "the PR head could not be verified" if not head_sha else "the PR head still matches it"
                        log.warning(
                            "step.monitor_ci.unknown_commit_head_unverifiable"
                            if not head_sha
                            else "step.monitor_ci.unknown_commit_but_pr_confirms",
                            pr=ctx.pr_number,
                            sha=sha_short,
                        )
                        self._write_heartbeat(
                            ctx,
                            f"CI poll: {elapsed}s/{max_wait}s elapsed, GitHub reports unknown commit "
                            f"{sha_short} but {reason} (retrying)",
                        )
                        await asyncio.sleep(poll_interval)
                        elapsed = int(time.monotonic() - start_time)
                        continue
                    log.error("step.monitor_ci.unknown_commit", pr=ctx.pr_number, sha=sha_short)
                    msg = f"Polled commit {sha_short} is not on the remote; push may have failed"
                    self._write_heartbeat(ctx, msg)
                    return (StepResult(success=False, summary=msg, error=str(exc)), [])
            else:
                checks = await get_ci_checks(
                    ctx.pr_number,
                    repo=ctx.repo,
                    github_user=ctx.config.github_user,
                )

            if checks is None:
                log.warning("step.monitor_ci.fetch_failed", elapsed=elapsed)
                self._write_heartbeat(ctx, f"CI poll: {elapsed}s/{max_wait}s elapsed, fetch failed (retrying)")
                await asyncio.sleep(poll_interval)
                elapsed = int(time.monotonic() - start_time)
                continue

            exclude = ctx.config.ci.exclude_checks
            all_excluded = False
            if exclude:
                before = len(checks)
                checks = [c for c in checks if not any(pat in c.name for pat in exclude)]
                skipped = before - len(checks)
                if skipped:
                    log.info("step.monitor_ci.excluded_checks", skipped=skipped, patterns=exclude)
                    all_excluded = not checks

            if not checks:
                grace_elapsed = time.monotonic() - grace_start >= grace_period
                if all_excluded:
                    if grace_elapsed:
                        # Every check on this lookup matched ci.exclude_checks: the
                        # checks are registered, just excluded, so there is no
                        # registration ambiguity to corroborate against a workflow
                        # run, and the run corroboration is skipped. The grace
                        # period itself still applies: a poll this early could be
                        # seeing only the excluded check because the rest haven't
                        # registered yet, not because there are no others.
                        log.info("step.monitor_ci.all_checks_excluded", elapsed=elapsed, patterns=exclude)
                        return (
                            StepResult(
                                success=True,
                                summary="No CI checks found (all excluded via ci.exclude_checks), proceeding",
                            ),
                            [],
                        )
                elif grace_elapsed:
                    confirm_sha = expected_sha
                    if not confirm_sha:
                        # No local HEAD to bind to (e.g. an unresolvable
                        # working tree): fall back to the PR's current head so
                        # the cross-check still has a SHA to corroborate
                        # against, rather than exempting this path from it.
                        confirm_sha = await get_pr_head_sha(
                            ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user
                        )
                    if not confirm_sha:
                        log.info("step.monitor_ci.no_checks_head_unresolvable", elapsed=elapsed)
                        self._write_heartbeat(
                            ctx,
                            f"CI poll: {elapsed}s/{max_wait}s elapsed, no checks registered yet "
                            f"(PR head unresolvable, retrying)",
                        )
                        await asyncio.sleep(poll_interval)
                        elapsed = int(time.monotonic() - start_time)
                        continue
                    # On the PR-level fallback path (expected_sha unresolved), `checks`
                    # above is already this exact PR-level lookup, empty; reuse it
                    # instead of making _pr_confirms_no_checks() re-fetch it.
                    confirmed_none, reason = await self._pr_confirms_no_checks(
                        ctx, confirm_sha, pr_checks=checks if not expected_sha else None
                    )
                    if not confirmed_none:
                        log.info(
                            "step.monitor_ci.sha_checks_not_registered",
                            elapsed=elapsed,
                            sha=confirm_sha[:8],
                            reason=reason,
                        )
                        reason_text = _NO_CHECKS_REASON_TEXT.get(reason, "absence of CI not yet confirmed")
                        self._write_heartbeat(
                            ctx,
                            f"CI poll: {elapsed}s/{max_wait}s elapsed, no checks registered yet for "
                            f"{confirm_sha[:8]} ({reason_text})",
                        )
                        await asyncio.sleep(poll_interval)
                        elapsed = int(time.monotonic() - start_time)
                        continue
                    log.warning(
                        "step.monitor_ci.no_checks_after_grace",
                        elapsed=elapsed,
                        grace=grace_period,
                    )
                    return (
                        StepResult(
                            success=True,
                            summary=f"No CI checks found after {elapsed}s grace period, proceeding",
                        ),
                        [],
                    )
                log.debug("step.monitor_ci.no_checks", elapsed=elapsed)
                self._write_heartbeat(ctx, f"CI poll: {elapsed}s/{max_wait}s elapsed, no checks registered yet")
                await asyncio.sleep(poll_interval)
                elapsed = int(time.monotonic() - start_time)
                continue

            all_completed = all(c.is_completed for c in checks)
            if all_completed:
                if expected_sha:
                    new_head = await self._find_pr_head_mismatch(ctx, expected_sha)
                    if new_head:
                        log.warning(
                            "step.monitor_ci.head_mismatch",
                            pr=ctx.pr_number,
                            elapsed=elapsed,
                            old_sha=sha_short,
                            new_sha=new_head[:8],
                        )
                        self._write_heartbeat(
                            ctx,
                            f"CI poll: PR head moved from {sha_short} to {new_head[:8]}; "
                            f"rebinding and continuing to poll the new head",
                        )
                        expected_sha = new_head
                        sha_short = new_head[:8]
                        grace_start = time.monotonic()
                        elapsed = int(time.monotonic() - start_time)
                        continue

                failed = [c for c in checks if c.is_failed]
                if failed:
                    names = ", ".join(c.name for c in failed)
                    return (
                        StepResult(
                            success=False,
                            summary=f"CI failed: {names}",
                            error=f"Failed checks: {names}",
                        ),
                        failed,
                    )
                if not any(c.is_passed for c in checks) and any(
                    c.conclusion == CheckConclusion.CANCELLED for c in checks
                ):
                    # A cancelled run from a superseded push is not evidence of
                    # success. Scoped to CANCELLED specifically (not "nothing
                    # passed" generally), since an all-SKIPPED or all-NEUTRAL
                    # set is a deliberate non-blocking outcome, not a failure.
                    names = ", ".join(c.name for c in checks)
                    msg = f"CI checks completed without any passing: {names}"
                    self._write_heartbeat(ctx, msg)
                    return (
                        StepResult(success=False, summary=msg, error=f"No passing checks: {names}"),
                        [],
                    )
                return (
                    StepResult(
                        success=True,
                        summary=f"All {len(checks)} CI checks passed",
                    ),
                    [],
                )

            completed_count = sum(1 for c in checks if c.is_completed)
            self._write_heartbeat(
                ctx,
                f"CI poll: {elapsed}s/{max_wait}s elapsed, {completed_count}/{len(checks)} checks completed",
            )
            await asyncio.sleep(poll_interval)
            elapsed = int(time.monotonic() - start_time)

        return (
            StepResult(
                success=False,
                summary="CI monitoring timed out",
                error=f"Timed out after {max_wait}s",
            ),
            [],
        )

    async def _check_ci_on_resume(self, ctx: ExecutionContext) -> StepResult | None:
        """On resume, check CI once. Return a result if CI already completed, else None to continue polling."""
        expected_sha = await self._get_local_head_sha(ctx)
        if expected_sha:
            try:
                checks = await get_ci_checks_for_sha(
                    expected_sha,
                    repo=ctx.repo,
                    github_user=ctx.config.github_user,
                )
            except UnknownCommitError:
                log.warning("step.monitor_ci.resume_unknown_commit", pr=ctx.pr_number, sha=expected_sha[:8])
                return None
        else:
            log.warning("step.monitor_ci.head_sha_unresolved", pr=ctx.pr_number)
            checks = await get_ci_checks(
                ctx.pr_number,
                repo=ctx.repo,
                github_user=ctx.config.github_user,
            )
        if checks is None or not checks:
            return None
        exclude = ctx.config.ci.exclude_checks
        if exclude:
            checks = [c for c in checks if not any(pat in c.name for pat in exclude)]
        if not checks:
            return None
        if not all(c.is_completed for c in checks):
            return None
        if expected_sha and await self._find_pr_head_mismatch(ctx, expected_sha):
            log.warning("step.monitor_ci.resume_head_mismatch", pr=ctx.pr_number)
            return None
        failed = [c for c in checks if c.is_failed]
        if failed:
            names = ", ".join(c.name for c in failed)
            return StepResult(success=False, summary=f"CI failed: {names}", error=f"Failed checks: {names}")
        if not any(c.is_passed for c in checks) and any(c.conclusion == CheckConclusion.CANCELLED for c in checks):
            names = ", ".join(c.name for c in checks)
            return StepResult(
                success=False,
                summary=f"CI checks completed without any passing: {names}",
                error=f"No passing checks: {names}",
            )
        return StepResult(success=True, summary=f"CI already passed (resumed after interruption, {len(checks)} checks)")

    @staticmethod
    def _write_heartbeat(ctx: ExecutionContext, message: str) -> None:
        """Write a progress line so the watchdog's no-output detector works."""
        if ctx.output_writer is not None:
            ctx.output_writer.write_line(message)

    @staticmethod
    async def _find_pr_head_mismatch(ctx: ExecutionContext, expected_sha: str) -> str | None:
        """Return the PR's current head SHA if it no longer matches *expected_sha*.

        Fails open (returns None, i.e. "no mismatch") when the head SHA can't
        be fetched: a transient gh/network failure must not block a result
        forever. Only a confirmed mismatch (another actor updated the PR to a
        different commit after this pipeline's push) returns the new SHA, so
        the caller can fail fast instead of polling a stale commit forever.
        """
        head_sha = await get_pr_head_sha(ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user)
        if not head_sha:
            log.warning("step.monitor_ci.head_sha_check_failed", pr=ctx.pr_number)
            return None
        return None if head_sha == expected_sha else head_sha

    @staticmethod
    async def _pr_confirms_no_checks(
        ctx: ExecutionContext, expected_sha: str, *, pr_checks: list[CICheck] | None = None
    ) -> tuple[bool, str]:
        """Whether the PR genuinely has no CI, not just no checks registered yet.

        Used to disambiguate an empty SHA-scoped result, which is itself
        ambiguous between "this PR has no CI" and "GitHub has not
        registered this commit's checks yet". Two independent answers have
        to agree before the caller may read it as the former.

        The first is the PR-level lookup (``gh pr checks``), which may
        report checks the SHA-scoped endpoints have not caught up with. Its
        result is filtered through the same ``ci.exclude_checks`` patterns
        as the SHA-scoped list, otherwise a project whose only checks are
        excluded would see this cross-check report "the PR has checks"
        forever and poll until ``max_wait``.

        The second is a workflow-run lookup for *expected_sha*. The
        PR-level lookup reads the PR's head commit, the same commit the
        SHA-scoped lookup reads, so on its own it corroborates nothing
        during a registration race: both are empty for a commit whose
        checks are seconds away from appearing. A workflow run record is
        created when the workflow is triggered, ahead of the check runs it
        reports, so "no check and no run" is the one combination that
        confirms CI never started for this commit (the same corroboration
        the ``/pr`` command's CI wait requires before it reports NO CI
        TRIGGERED). A run that exists without any check is an anomaly, not
        a pass, so the caller keeps polling.

        Fails closed (returns ``(False, reason)``) whenever either lookup
        fails, since reading an unverified fetch failure as "confirmed no
        CI" is the same silent pass this cross-check exists to prevent, just
        moved behind one failed API call; the caller keeps polling, with
        ``max_wait`` as the fail-closed terminal outcome. *reason* is one of
        ``"pr_reports_checks"``, ``"pr_lookup_failed"``,
        ``"run_without_checks"``, or ``"run_lookup_unreadable"`` when the
        verdict is ``False``, so a caller logging or reporting the
        non-confirmation can say which of the four it was instead of
        assuming the PR has checks. Empty when the verdict is ``True``.

        *pr_checks*, when given, is used in place of a fresh PR-level
        lookup: passed only by ``_poll_ci()``'s PR-level fallback path,
        where the caller already made that identical ``get_ci_checks()``
        call to produce the empty list it is now trying to corroborate, so
        issuing it again would be a redundant round trip carrying no new
        information. The workflow-run lookup below remains the only
        independent half of the cross-check on that path. Always omitted
        on the SHA-scoped path, where the PR-level lookup genuinely is
        independent of the SHA-scoped one.
        """
        if pr_checks is None:
            pr_checks = await get_ci_checks(ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user)
        if pr_checks is None:
            log.warning("step.monitor_ci.pr_confirm_fetch_failed", pr=ctx.pr_number)
            return False, "pr_lookup_failed"
        exclude = ctx.config.ci.exclude_checks
        if exclude and pr_checks:
            pr_checks = [c for c in pr_checks if not any(pat in c.name for pat in exclude)]
        if pr_checks:
            return False, "pr_reports_checks"

        has_runs = await sha_has_workflow_runs(expected_sha, repo=ctx.repo, github_user=ctx.config.github_user)
        if has_runs is None:
            log.warning("step.monitor_ci.workflow_runs_unreadable", pr=ctx.pr_number, sha=expected_sha[:8])
            return False, "run_lookup_unreadable"
        if has_runs:
            log.info("step.monitor_ci.workflow_run_without_checks", pr=ctx.pr_number, sha=expected_sha[:8])
            return False, "run_without_checks"
        return True, ""

    @staticmethod
    async def _get_local_head_sha(ctx: ExecutionContext) -> str:
        """Get the current HEAD SHA in the working directory."""
        from sova.utils.shell import run

        try:
            result = await run("git", "rev-parse", "HEAD", cwd=ctx.working_dir)
        except OSError:
            # A pruned or never-created ``working_dir`` makes the spawn
            # itself raise (FileNotFoundError on the cwd). Fall back to the
            # PR-level lookup rather than letting this abort the whole step.
            log.warning("step.monitor_ci.head_sha_spawn_failed", cwd=str(ctx.working_dir), exc_info=True)
            return ""
        return result.stdout.strip() if result.success else ""

    # ------------------------------------------------------------------
    # CI fix loop
    # ------------------------------------------------------------------

    async def _try_fix_ci(
        self,
        ctx: ExecutionContext,
        failed_checks: list[CICheck],
        max_attempts: int,
    ) -> StepResult:
        """Attempt to fix CI failures by invoking Claude, re-pushing, and re-polling."""
        import functools
        from decimal import Decimal

        from sova.git import operations as git_ops
        from sova.utils.shell import run

        invoke = functools.partial(dispatch_prompt, self)
        total_fix_cost = Decimal("0")

        for attempt in range(1, max_attempts + 1):
            if ctx.is_budget_exceeded:
                names = ", ".join(c.name for c in failed_checks)
                return StepResult(
                    success=False,
                    summary=f"CI fix aborted: budget exceeded after {attempt - 1} attempt(s)",
                    error=f"Budget exceeded. Remaining failures: {names}",
                    cost_usd=total_fix_cost,
                )

            if ctx.resource_remaining_fraction < BUDGET_STOP_RETRY_THRESHOLD:
                log.warning(
                    "step.monitor_ci.budget_fraction_stop_retry",
                    attempt=attempt,
                    fraction=ctx.resource_remaining_fraction,
                )
                names = ", ".join(c.name for c in failed_checks)
                return StepResult(
                    success=False,
                    summary=(
                        f"CI still failing after {attempt - 1} fix attempt(s); stopping retries at "
                        f"{ctx.resource_remaining_fraction:.0%} resource remaining. Remaining failures: {names}"
                    ),
                    error=f"Failed checks: {names}",
                    cost_usd=total_fix_cost,
                )

            log.info("step.monitor_ci.fix_attempt", attempt=attempt, max=max_attempts)

            fix_cost, error = await self._invoke_fix(ctx, failed_checks, invoke, run, attempt)
            total_fix_cost += fix_cost
            if error:
                msg = f"CI fix LLM invocation failed on attempt {attempt}: {error}"
                return StepResult(
                    success=False,
                    summary=msg,
                    error=format_fix_llm_failure(error, attempt),
                    cost_usd=total_fix_cost,
                )

            skip, result = await self._validate_fix(ctx, attempt, max_attempts, run)
            if result:
                result.cost_usd += total_fix_cost
                return result
            if skip:
                continue

            head_sha = await self._get_local_head_sha(ctx)

            try:
                await git_ops.push(
                    ctx.branch_name,
                    force=True,
                    set_upstream=True,
                    cwd=ctx.working_dir,
                    github_user=ctx.config.github_user,
                )
            except RuntimeError as exc:
                log.error("step.monitor_ci.push_failed", error=str(exc), exc_info=True)
                msg = f"CI fix push failed on attempt {attempt}: {exc}"
                return StepResult(success=False, summary=msg, error=str(exc), cost_usd=total_fix_cost)

            result, new_failed = await self._poll_ci(ctx, expected_sha=head_sha)
            if result.success:
                return StepResult(
                    success=True,
                    summary=f"CI passed after {attempt} fix attempt(s)",
                    cost_usd=total_fix_cost,
                )
            if not new_failed:
                result.cost_usd += total_fix_cost
                return result

            failed_checks = new_failed
            log.warning(
                "step.monitor_ci.still_failing",
                attempt=attempt,
                checks=", ".join(c.name for c in failed_checks),
            )

        names = ", ".join(c.name for c in failed_checks)
        return StepResult(
            success=False,
            summary=f"CI still failing after {max_attempts} fix attempt(s): {names}",
            error=f"Failed checks after {max_attempts} fix attempt(s): {names}",
            cost_usd=total_fix_cost,
        )

    async def _invoke_fix(
        self,
        ctx: ExecutionContext,
        failed_checks: list[CICheck],
        invoke: _LLMInvoker,
        run: _ShellRunner,
        attempt: int,
    ) -> tuple:
        """Invoke Claude to fix CI failures. Returns (cost, error_or_None).

        *attempt* selects the model through ``resolve_fix_attempt_route()``,
        shared with ``ValidateStep``: attempt 1 routes by task type, every
        later attempt escalates to the step's dev model.
        """
        from decimal import Decimal

        sonar_checks, other_checks = self._split_sonarcloud_checks(failed_checks)
        coverage_prompt = await self._fetch_sonarcloud_coverage_prompt(ctx, sonar_checks)

        if coverage_prompt and not other_checks:
            prompt = coverage_prompt
        else:
            # Fetch logs for non-Sonar checks when coverage_prompt handles Sonar,
            # otherwise fetch logs for all failed checks (including Sonar).
            log_checks = other_checks if coverage_prompt and other_checks else failed_checks
            raw_logs = await get_ci_failure_logs(
                log_checks,
                repo=ctx.repo,
                github_user=ctx.config.github_user,
            )
            ci_logs = _redact_logs(raw_logs)
            if len(ci_logs) > _CI_LOG_LIMIT:
                ci_logs = "...(truncated)\n" + ci_logs[-_CI_LOG_LIMIT:]
            log_result = await run("git", "log", "--oneline", "-5", cwd=ctx.working_dir)
            recent_commits = log_result.stdout.strip() if log_result.success else ""
            prompt = self._build_fix_prompt(failed_checks, ci_logs, recent_commits, ctx)
            if coverage_prompt:
                prompt += f"\n\n---\n\n{coverage_prompt}"

        budget = ctx.config.agent.max_budget - ctx.cost_usd
        if budget <= 0:
            return Decimal("0"), RuntimeError("Budget exhausted")

        attempt_model, attempt_task_type = resolve_fix_attempt_route(ctx, self.TASK_TYPE, attempt)

        try:
            llm_result = await invoke(
                prompt,
                model=attempt_model,
                task_type=attempt_task_type,
                cwd=ctx.working_dir,
                max_budget_usd=budget,
                timeout=ctx.config.validation.fix_timeout,
                ctx=ctx,
            )
            ctx.add_usage(
                llm_result, routed_reason=fix_attempt_routed_reason(llm_result, self.TASK_TYPE, attempt_task_type)
            )
            return llm_result.cost_usd, None
        except RuntimeError as exc:
            log.error("step.monitor_ci.llm_failed", error=str(exc), exc_info=True)
            return Decimal("0"), exc

    @staticmethod
    async def _ensure_committed(
        ctx: ExecutionContext,
        run: _ShellRunner,
    ) -> bool | None:
        """Run ruff (if available) and auto-commit any uncommitted changes.

        Returns True if a new commit was created, False if the tree was
        already clean, or None if staging/committing failed (caller should
        stop or retry rather than pushing stale HEAD).
        """
        ruff_result = await run("ruff", "check", "--fix", ".", cwd=ctx.working_dir, timeout=60)
        if ruff_result.success:
            await run("ruff", "format", ".", cwd=ctx.working_dir, timeout=60)
        else:
            log.debug("step.monitor_ci.ruff_unavailable_or_failed")

        status = await run("git", "status", "--porcelain", cwd=ctx.working_dir)
        has_changes = bool(status.success and status.stdout.strip())

        if not has_changes:
            return False

        stage = await run("git", "add", "-u", cwd=ctx.working_dir)
        if not stage.success:
            log.warning("step.monitor_ci.stage_failed")
            return None

        commit = await run(
            "git",
            "commit",
            "-m",
            "fix(core): address CI failures",
            cwd=ctx.working_dir,
        )
        if commit.success:
            log.info("step.monitor_ci.auto_committed")
            return True

        log.warning("step.monitor_ci.auto_commit_failed")
        return None

    async def _validate_fix(
        self,
        ctx: ExecutionContext,
        attempt: int,
        max_attempts: int,
        run: _ShellRunner,
    ) -> tuple[bool, StepResult | None]:
        """Run lint safety net, pre-push hook and check for new commits.

        Returns (should_skip, error_result).
        """
        from sova.core.steps.validate import find_pre_push_hook

        commit_result = await self._ensure_committed(ctx, run)
        if commit_result is None:
            log.warning("step.monitor_ci.ensure_committed_failed", attempt=attempt)
            if attempt == max_attempts:
                return False, StepResult(
                    success=False,
                    summary=f"CI fix failed to stage/commit after {max_attempts} attempt(s)",
                    error="git add or git commit failed; changes remain uncommitted",
                )
            return True, None

        hook = await find_pre_push_hook(ctx.working_dir)
        if hook:
            hook_result = await run(hook, "origin", "unused", cwd=ctx.working_dir, timeout=120)
            if not hook_result.success:
                hook_output = (hook_result.stdout + "\n" + hook_result.stderr).strip()
                log.warning(
                    "step.monitor_ci.hook_failed_after_fix",
                    attempt=attempt,
                    output=hook_output[:500],
                )
                if attempt == max_attempts:
                    return False, StepResult(
                        success=False,
                        summary=f"CI fix failed local validation after {max_attempts} attempt(s)",
                        error=hook_output[:1000],
                    )
                return True, None

        ahead = await run(
            "git",
            "rev-list",
            "--count",
            f"origin/{ctx.branch_name}..HEAD",
            cwd=ctx.working_dir,
        )
        try:
            ahead_count = int(ahead.stdout.strip() or "0") if ahead.success else 0
        except ValueError:
            ahead_count = 0
        if ahead_count == 0:
            log.warning("step.monitor_ci.no_new_commit", attempt=attempt)
            if attempt == max_attempts:
                return False, StepResult(
                    success=False,
                    summary=f"CI fix produced no pushable commit after {max_attempts} attempt(s)",
                    error="Auto-recovery needs a committed fix before pushing",
                )
            return True, None

        return False, None

    # ------------------------------------------------------------------
    # SonarCloud coverage detection
    # ------------------------------------------------------------------

    @staticmethod
    def _split_sonarcloud_checks(
        failed_checks: list[CICheck],
    ) -> tuple[list[CICheck], list[CICheck]]:
        """Split failed checks into SonarCloud and non-SonarCloud groups."""
        from sova.adapters.external_reviews import _CHECK_NAMES

        sonar_name = _CHECK_NAMES.get("sonarcloud", "")
        sonar: list[CICheck] = []
        other: list[CICheck] = []
        for check in failed_checks:
            if check.name == sonar_name:
                sonar.append(check)
            else:
                other.append(check)
        return sonar, other

    @staticmethod
    async def _fetch_sonarcloud_coverage_prompt(
        ctx: ExecutionContext,
        sonar_checks: list[CICheck],
    ) -> str:
        """Fetch coverage data from SonarCloud and build a test-writing prompt.

        Returns an empty string when SonarCloud is not configured, unreachable,
        or the coverage gate is passing.
        """
        if not sonar_checks:
            return ""

        ext = ctx.config.external_reviews
        if not ext.enabled or "sonarcloud" not in ext.tools:
            return ""
        project_key = ext.sonarcloud.project_key if ext.sonarcloud else ""
        if not project_key or ctx.pr_number is None:
            return ""

        from sova.adapters.external_reviews import (
            fetch_sonarcloud_coverage_issues,
            format_coverage_findings_for_prompt,
        )

        report = await fetch_sonarcloud_coverage_issues(
            project_key,
            ctx.pr_number,
            required_pct=ext.sonarcloud.coverage_threshold,
        )
        if report is None:
            return ""

        if report.coverage_pct >= report.required_pct and not report.findings:
            log.info("step.monitor_ci.sonarcloud_coverage_ok", pct=report.coverage_pct)
            return ""

        log.info(
            "step.monitor_ci.sonarcloud_coverage_gap",
            coverage=report.coverage_pct,
            required=report.required_pct,
            findings=len(report.findings),
        )
        return format_coverage_findings_for_prompt(report)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_fix_prompt(
        failed_checks: list[CICheck],
        ci_logs: str,
        recent_commits: str,
        ctx: ExecutionContext,
    ) -> str:
        check_names = ", ".join(c.name for c in failed_checks)
        task_title = ctx.task.title if ctx.task else f"Issue #{ctx.issue_number}"

        return (
            f"The following CI checks failed on PR #{ctx.pr_number} "
            f"for '{task_title}':\n\n"
            f"Failed checks: {check_names}\n\n"
            f"CI failure logs:\n```\n{ci_logs}\n```\n\n"
            f"Recent commits on this branch:\n{recent_commits}\n\n"
            f"Fix ALL issues causing CI failures. The code works locally "
            f"(pre-push hooks passed) but the remote CI environment caught "
            f"these issues. Common causes include:\n"
            f"- Missing dependencies in CI requirements/lockfiles\n"
            f"- Tests that import modules not declared in project dependencies\n"
            f"- Environment-specific path or config differences\n"
            f"- Type checking or linting issues in stricter CI config\n\n"
            f"Before staging and committing, run "
            f"`ruff check --fix . && ruff format .` "
            f"to ensure code passes linting.\n\n"
            f"## Project Invariant Rules\n"
            f"- Never represent monetary values "
            f"(cost, amount, price, total, balance) as floats; use Decimal instead\n"
            f"- Do not add AI co-author lines in commits\n"
            f"- Do not use emojis in code or comments\n\n"
            f"After fixing, stage and commit the changes with a message like "
            f"'fix(core): address CI failure in {check_names}'. "
            f"Do not modify CI configuration files unless that is genuinely the fix."
        )
