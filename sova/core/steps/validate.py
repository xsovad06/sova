"""Step 7b: Validate -- run project pre-push hooks before pushing.

Discovers the project's pre-push hook (via core.hooksPath, .githooks/
auto-detect, or .git/hooks fallback) and runs it. If it fails, invokes
Claude to fix the issues and re-commits.

Also checks for test regressions against the baseline snapshot captured
by CaptureBaselineStep (issue #233).
"""

from __future__ import annotations

from pathlib import Path

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.core.test_baseline import diff_results, load_baseline, run_test_suite
from sova.llm.client import invoke
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.validate")

_HOOK_OUTPUT_LIMIT = 8000


def _truncate_hook_output(output: str) -> str:
    """Keep the tail of hook output, where actionable errors appear."""
    if len(output) <= _HOOK_OUTPUT_LIMIT:
        return output
    return "...(truncated)\n" + output[-_HOOK_OUTPUT_LIMIT:]


async def find_pre_push_hook(cwd: Path | str | None) -> str | None:
    """Locate the pre-push hook script for the project.

    Auto-configures core.hooksPath when .githooks/pre-push exists but the
    config is missing, preventing silent bypass of invariant checks.
    """
    result = await run("git", "config", "--get", "core.hooksPath", cwd=cwd)
    if result.success and result.stdout.strip():
        hooks_dir = result.stdout.strip()
    else:
        toplevel = await run("git", "rev-parse", "--show-toplevel", cwd=cwd)
        if toplevel.success:
            githooks_hook = Path(toplevel.stdout.strip()) / ".githooks" / "pre-push"
            if githooks_hook.exists():
                log.info("step.validate.auto_configure_hooks")
                config_result = await run("git", "config", "core.hooksPath", ".githooks", cwd=cwd)
                if not config_result.success:
                    log.warning("step.validate.auto_configure_failed", error=config_result.stderr)
                hooks_dir = ".githooks"
            else:
                git_dir = await run("git", "rev-parse", "--git-dir", cwd=cwd)
                if not git_dir.success:
                    return None
                hooks_dir = f"{git_dir.stdout.strip()}/hooks"
        else:
            return None

    hook_path = f"{hooks_dir}/pre-push"
    test_result = await run("test", "-x", hook_path, cwd=cwd)
    if test_result.success:
        return hook_path
    return None


class ValidateStep(BaseStep):
    name = "validate"
    max_retries = 0

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        cwd = ctx.working_dir
        hook = await find_pre_push_hook(cwd)

        if hook is None:
            log.info("step.validate.no_hook", cwd=str(cwd))
            return StepResult(success=True, summary="No pre-push hook found, skipping validation")

        log.info("step.validate.running_hook", hook=hook)

        hook_timeout = ctx.config.validation.hook_timeout
        result = await run(hook, "origin", "unused", cwd=cwd, timeout=hook_timeout)
        if result.success:
            return StepResult(success=True, summary="Pre-push hook passed")

        hook_output = (result.stdout + "\n" + result.stderr).strip()
        log.warning("step.validate.hook_failed", output=hook_output[:500])

        max_fix_attempts = ctx.config.validation.max_fix_attempts
        for attempt in range(1, max_fix_attempts + 1):
            # Check budget before attempting fix
            if ctx.is_budget_exceeded:
                return StepResult(
                    success=False,
                    summary=f"Pre-push hook failed; budget exceeded after {attempt - 1} fix attempt(s)",
                    error="budget_exceeded",
                )

            log.info("step.validate.fix_attempt", attempt=attempt)

            prompt = (
                f"The project's pre-push hook failed with the following output:\n\n"
                f"```\n{_truncate_hook_output(hook_output)}\n```\n\n"
                f"Fix ALL issues reported by the hook. After fixing, stage and commit "
                f"the changes with message 'fix: address pre-push hook violations'. "
                f"Do not modify or disable the hook itself."
            )

            try:
                llm_result = await invoke(
                    prompt,
                    model=ctx.resolved_model or ctx.config.agent.model,
                    cwd=ctx.working_dir,
                    max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
                    timeout=ctx.config.validation.fix_timeout,
                )
                ctx.add_cost(llm_result.cost_usd)
            except RuntimeError as exc:
                log.error("step.validate.llm_failed", error=str(exc))
                return StepResult(
                    success=False,
                    summary=f"Pre-push hook failed and auto-fix failed: {exc}",
                    error=str(exc),
                )

            retry = await run(hook, "origin", "unused", cwd=cwd, timeout=hook_timeout)
            if retry.success:
                return StepResult(
                    success=True,
                    summary=f"Pre-push hook passed after {attempt} fix attempt(s)",
                    cost_usd=llm_result.cost_usd,
                )

            hook_output = (retry.stdout + "\n" + retry.stderr).strip()
            log.warning("step.validate.still_failing", attempt=attempt, output=hook_output[:500])

        return StepResult(
            success=False,
            summary=f"Pre-push hook still failing after {max_fix_attempts} fix attempts",
            error=hook_output[:1000],
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Structural gate: commits must still exist after validation.

        Checks commits ahead of base, plus unstaged and staged diffs to
        catch uncommitted changes left by the hook fix loop. Regression
        checking is performed separately in verify_output() to allow
        longer timeouts for test suite execution.
        """
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        if not (log_result.success and log_result.stdout.strip()):
            return GateCheckResult(passed=False, reason="No commits ahead of base after validation")

        unstaged = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        if unstaged.success and unstaged.stdout.strip():
            detail = unstaged.stdout.strip()[:200]
            return GateCheckResult(passed=False, reason=f"Unstaged changes after validation: {detail}")

        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        if staged.success and staged.stdout.strip():
            detail = staged.stdout.strip()[:200]
            return GateCheckResult(passed=False, reason=f"Staged but uncommitted changes after validation: {detail}")

        return GateCheckResult(passed=True)

    async def verify_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Heavyweight verification: run test suite and check for regressions."""
        regression_check = await self._check_regressions(ctx)
        if regression_check is not None:
            return regression_check

        return GateCheckResult(passed=True)

    async def _check_regressions(self, ctx: ExecutionContext) -> GateCheckResult | None:
        """Check for test regressions against baseline. Returns None if no issues."""
        if ctx.test_baseline_path is None:
            return None

        # Use worktree_dir (where CaptureBaselineStep saves the baseline),
        # falling back to working_dir for consistency.
        test_dir = ctx.worktree_dir or ctx.working_dir
        baseline = load_baseline(test_dir)
        if baseline is None:
            return None

        test_cmd = ctx.config.test_cmd
        if not test_cmd or not test_cmd.strip():
            return None

        try:
            current = await run_test_suite(
                test_cmd=test_cmd,
                cwd=test_dir,
                cmd_timeout=ctx.config.testing.baseline_timeout,
            )
        except Exception:
            log.warning("step.validate.regression_check_failed", exc_info=True)
            return None

        report = diff_results(baseline, current)
        if report.has_regressions:
            names = [r.nodeid for r in report.regressions[:10]]
            log.error(
                "step.validate.regressions_detected",
                count=len(report.regressions),
                tests=names,
            )
            return GateCheckResult(
                passed=False,
                reason=f"Test regressions detected: {report.summary()}. First: {', '.join(names[:3])}",
            )

        if report.fixed:
            log.info("step.validate.tests_fixed", count=len(report.fixed))

        return None
