"""Step 8: Monitor CI -- poll CI checks until they complete.

When CI fails, optionally invokes Claude to fix the issue and re-pushes,
looping up to ``ci.max_fix_attempts`` times before giving up. Set
``max_fix_attempts = 0`` in sova.toml to disable auto-recovery (the
pre-existing behaviour).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git.operations import CICheck, get_ci_checks, get_ci_failure_logs
from sova.llm.egress import scan_and_redact
from sova.utils.logging import get_logger

_ShellRunner = Callable[..., Any]
_LLMInvoker = Callable[..., Any]

log = get_logger(component="step.monitor_ci")

_MAX_CI_FIX_ATTEMPTS = 3


def _redact_logs(text: str) -> str:
    """Strip tokens, keys, and secrets from CI log output."""
    return scan_and_redact(text).redacted_text


class MonitorCIStep(BaseStep):
    name = "monitor_ci"
    max_retries = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if ctx.pr_number is None:
            return StepResult(success=False, summary="No PR to monitor", error="pr_number is None")

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
    # CI polling (shared between initial check and post-fix re-checks)
    # ------------------------------------------------------------------

    async def _poll_ci(
        self,
        ctx: ExecutionContext,
        *,
        expected_sha: str = "",
    ) -> tuple[StepResult, list[CICheck]]:
        """Poll CI checks until completion. Returns (result, failed_checks).

        When *expected_sha* is provided (e.g. after a force-push), the first
        poll verifies the PR's head SHA matches.  Stale checks from a prior
        commit are treated as "no checks yet" until GitHub registers the push.
        """
        poll_interval = ctx.config.ci.poll_interval
        max_wait = ctx.config.ci.max_wait
        grace_period = ctx.config.ci.no_checks_grace_period
        elapsed = 0
        sha_validated = not expected_sha

        sha_short = expected_sha[:8] if expected_sha else ""
        log.info("step.monitor_ci.poll", pr=ctx.pr_number, max_wait=max_wait, expected_sha=sha_short)

        while elapsed < max_wait:
            if not sha_validated:
                sha_validated = await self._verify_pr_head_sha(ctx, expected_sha)
                if not sha_validated:
                    log.debug("step.monitor_ci.sha_mismatch", elapsed=elapsed)
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval
                    continue

            checks = await get_ci_checks(
                ctx.pr_number,
                repo=ctx.repo,
                github_user=ctx.config.github_user,
            )

            if checks is None:
                log.warning("step.monitor_ci.fetch_failed", elapsed=elapsed)
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                continue

            if not checks:
                if elapsed >= grace_period:
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
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                continue

            all_completed = all(c.is_completed for c in checks)
            if all_completed:
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

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        return (
            StepResult(
                success=False,
                summary="CI monitoring timed out",
                error=f"Timed out after {max_wait}s",
            ),
            [],
        )

    @staticmethod
    async def _verify_pr_head_sha(ctx: ExecutionContext, expected_sha: str) -> bool:
        """Check that the PR's head commit matches *expected_sha*."""
        from sova.utils.gh import resolve_gh_env
        from sova.utils.shell import run

        env = await resolve_gh_env(ctx.config.github_user)
        result = await run(
            "gh",
            "pr",
            "view",
            str(ctx.pr_number),
            "--repo",
            ctx.repo,
            "--json",
            "headRefOid",
            env=env,
        )
        if not result.success:
            log.warning("step.monitor_ci.sha_check_failed", pr=ctx.pr_number)
            return False
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            log.warning("step.monitor_ci.sha_check_parse_failed", pr=ctx.pr_number)
            return False
        return data.get("headRefOid", "").startswith(expected_sha[:12])

    @staticmethod
    async def _get_local_head_sha(ctx: ExecutionContext) -> str:
        """Get the current HEAD SHA in the working directory."""
        from sova.utils.shell import run

        result = await run("git", "rev-parse", "HEAD", cwd=ctx.working_dir)
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
        from decimal import Decimal

        from sova.git import operations as git_ops
        from sova.llm.client import invoke
        from sova.utils.shell import run

        total_fix_cost = Decimal("0")

        for attempt in range(1, max_attempts + 1):
            if ctx.is_budget_exceeded:
                names = ", ".join(c.name for c in failed_checks)
                return StepResult(
                    success=False,
                    summary=f"CI fix aborted: budget exceeded after {attempt - 1} attempt(s)",
                    error=f"Budget exceeded. Remaining failures: {names}",
                )

            log.info("step.monitor_ci.fix_attempt", attempt=attempt, max=max_attempts)

            fix_cost, error = await self._invoke_fix(ctx, failed_checks, invoke, run)
            total_fix_cost += fix_cost
            if error:
                msg = f"CI fix LLM invocation failed on attempt {attempt}: {error}"
                return StepResult(success=False, summary=msg, error=str(error))

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
                model=ctx.config.agent.model,
                cwd=ctx.working_dir,
                max_budget_usd=budget,
            )
            ctx.add_cost(llm_result.cost_usd)
            return llm_result.cost_usd, None
        except RuntimeError as exc:
            log.error("step.monitor_ci.llm_failed", error=str(exc), exc_info=True)
            return Decimal("0"), exc

    async def _validate_fix(
        self,
        ctx: ExecutionContext,
        attempt: int,
        max_attempts: int,
        run: _ShellRunner,
    ) -> tuple[bool, StepResult | None]:
        """Run pre-push hook and check for new commits. Returns (should_skip, error_result)."""
        from sova.core.steps.validate import find_pre_push_hook

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
            f"After fixing, stage and commit the changes with a message like "
            f"'fix: address CI failure in {check_names}'. "
            f"Do not modify CI configuration files unless that is genuinely the fix."
        )
