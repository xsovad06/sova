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
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git.operations import (
    CICheck,
    UnknownCommitError,
    get_ci_checks,
    get_ci_checks_for_sha,
    get_ci_failure_logs,
    get_pr_head_sha,
)
from sova.llm.egress import scan_and_redact
from sova.llm.errors import format_fix_llm_failure
from sova.utils.logging import get_logger

_ShellRunner = Callable[..., Any]
_LLMInvoker = Callable[..., Any]

log = get_logger(component="step.monitor_ci")

_MAX_CI_FIX_ATTEMPTS = 3
_CI_LOG_LIMIT = 12_000


def _redact_logs(text: str) -> str:
    """Strip tokens, keys, and secrets from CI log output."""
    return scan_and_redact(text).redacted_text


class MonitorCIStep(BaseStep):
    name = "monitor_ci"
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
            log.info("step.monitor_ci.no_existing_checks")
            return StepResult(success=True, summary="No new commits and no CI checks configured")

        exclude = ctx.config.ci.exclude_checks
        if exclude:
            checks = [c for c in checks if not any(pat in c.name for pat in exclude)]
        if not checks:
            return StepResult(success=True, summary="No new commits and all CI checks excluded")

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
        passes the step after the PR-level lookup confirms the PR genuinely
        has no CI configured (through the same ``ci.exclude_checks``
        filter), since the empty SHA-scoped result is itself ambiguous
        between "no CI" and "not registered yet"; a PR-level fetch failure
        during that cross-check is never read as "confirmed no CI", so an
        outage there falls closed to continued polling rather than a silent
        pass. A SHA GitHub has no record of at all (both the check-runs and
        commit-status endpoints independently return 404/422; either one
        succeeding proves the commit exists) fails immediately unless the
        PR's own head SHA still matches *expected_sha*, in which case that is
        stronger evidence of a transient GitHub-side inconsistency than of a
        failed push, so polling continues instead. If the PR head moves to a
        different commit entirely (another actor pushed after this
        pipeline's push), the step rebinds to the new head, resets the
        no-checks grace clock (the freshly bound head has its own
        registration window), and keeps polling rather than failing the run
        over a benign event. Falls back to the PR-level lookup only when the
        local HEAD SHA cannot be resolved.
        Elapsed time is measured from a real monotonic clock, not by
        counting polls, so extra API round trips per iteration never drift
        the loop past ``max_wait``.
        """
        poll_interval = ctx.config.ci.poll_interval
        max_wait = ctx.config.ci.max_wait
        grace_period = ctx.config.ci.no_checks_grace_period
        start_time = time.monotonic()
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
                    if head_sha == expected_sha:
                        log.warning(
                            "step.monitor_ci.unknown_commit_but_pr_confirms",
                            pr=ctx.pr_number,
                            sha=sha_short,
                        )
                        self._write_heartbeat(
                            ctx,
                            f"CI poll: {elapsed}s/{max_wait}s elapsed, GitHub reports unknown commit "
                            f"{sha_short} but the PR head still matches it (retrying)",
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
            if exclude:
                before = len(checks)
                checks = [c for c in checks if not any(pat in c.name for pat in exclude)]
                skipped = before - len(checks)
                if skipped:
                    log.info("step.monitor_ci.excluded_checks", skipped=skipped, patterns=exclude)

            if not checks:
                if elapsed >= grace_period:
                    if expected_sha and not await self._pr_confirms_no_checks(ctx):
                        log.info(
                            "step.monitor_ci.sha_checks_not_registered",
                            elapsed=elapsed,
                            sha=sha_short,
                        )
                        self._write_heartbeat(
                            ctx,
                            f"CI poll: {elapsed}s/{max_wait}s elapsed, no checks registered yet for "
                            f"{sha_short} (PR reports checks exist)",
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
    async def _pr_confirms_no_checks(ctx: ExecutionContext) -> bool:
        """Whether the PR-level lookup also reports no (non-excluded) checks.

        Used to disambiguate an empty SHA-scoped result: GitHub may simply
        not have registered checks for this specific commit yet, which the
        PR-level lookup (backed by ``gh pr checks``) can still see since it
        is not SHA-bound. The PR-level result is filtered through the same
        ``ci.exclude_checks`` patterns as the SHA-scoped list, otherwise a
        project whose only checks are excluded would see this cross-check
        report "the PR has checks" forever and poll until ``max_wait``.
        Fails closed (returns False, i.e. "not confirmed empty") on a
        PR-level fetch failure, since reading an unverified fetch failure as
        "confirmed no CI" is the same silent pass this cross-check exists to
        prevent, just moved behind one failed API call; the caller keeps
        polling, with ``max_wait`` as the fail-closed terminal outcome.
        """
        pr_checks = await get_ci_checks(ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user)
        if pr_checks is None:
            log.warning("step.monitor_ci.pr_confirm_fetch_failed", pr=ctx.pr_number)
            return False
        exclude = ctx.config.ci.exclude_checks
        if exclude and pr_checks:
            pr_checks = [c for c in pr_checks if not any(pat in c.name for pat in exclude)]
        return not pr_checks

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

            fix_cost, error = await self._invoke_fix(ctx, failed_checks, invoke, run)
            total_fix_cost += fix_cost
            if error:
                msg = f"CI fix LLM invocation failed on attempt {attempt}: {error}"
                return StepResult(success=False, summary=msg, error=format_fix_llm_failure(error, attempt))

            skip, result = await self._validate_fix(ctx, attempt, max_attempts, run)
            if result:
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
                return StepResult(success=False, summary=msg, error=str(exc))

            result, new_failed = await self._poll_ci(ctx, expected_sha=head_sha)
            if result.success:
                return StepResult(
                    success=True,
                    summary=f"CI passed after {attempt} fix attempt(s)",
                    cost_usd=total_fix_cost,
                )
            if not new_failed:
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
        )

    async def _invoke_fix(
        self,
        ctx: ExecutionContext,
        failed_checks: list[CICheck],
        invoke: _LLMInvoker,
        run: _ShellRunner,
    ) -> tuple:
        """Invoke Claude to fix CI failures. Returns (cost, error_or_None)."""
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
        try:
            llm_result = await invoke(
                prompt,
                model=ctx.resolved_model or ctx.config.agent.model,
                task_type="monitor_ci",
                cwd=ctx.working_dir,
                max_budget_usd=budget,
                timeout=ctx.config.validation.fix_timeout,
                ctx=ctx,
            )
            ctx.add_usage(llm_result)
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
