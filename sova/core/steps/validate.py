"""Step 7b: Validate -- run project pre-push hooks before pushing.

Discovers the project's pre-push hook (via core.hooksPath or .git/hooks)
and runs it. If it fails, invokes Claude to fix the issues and re-commits.
This prevents the push step from failing due to hook violations that the
developer agent could have fixed.
"""

from __future__ import annotations

from pathlib import Path

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.llm.client import invoke
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.validate")

_MAX_FIX_ATTEMPTS = 2


async def _find_pre_push_hook(cwd: Path | str | None) -> str | None:
    """Locate the pre-push hook script for the project."""
    result = await run("git", "config", "--get", "core.hooksPath", cwd=cwd)
    if result.success and result.stdout.strip():
        hooks_dir = result.stdout.strip()
    else:
        git_dir = await run("git", "rev-parse", "--git-dir", cwd=cwd)
        if not git_dir.success:
            return None
        hooks_dir = f"{git_dir.stdout.strip()}/hooks"

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
        hook = await _find_pre_push_hook(cwd)

        if hook is None:
            log.info("step.validate.no_hook", cwd=str(cwd))
            return StepResult(success=True, summary="No pre-push hook found, skipping validation")

        log.info("step.validate.running_hook", hook=hook)

        result = await run(hook, "origin", "unused", cwd=cwd, timeout=120)
        if result.success:
            return StepResult(success=True, summary="Pre-push hook passed")

        hook_output = (result.stdout + "\n" + result.stderr).strip()
        log.warning("step.validate.hook_failed", output=hook_output[:500])

        for attempt in range(1, _MAX_FIX_ATTEMPTS + 1):
            log.info("step.validate.fix_attempt", attempt=attempt)

            prompt = (
                f"The project's pre-push hook failed with the following output:\n\n"
                f"```\n{hook_output}\n```\n\n"
                f"Fix ALL issues reported by the hook. After fixing, stage and commit "
                f"the changes with message 'fix: address pre-push hook violations'. "
                f"Do not modify or disable the hook itself."
            )

            try:
                llm_result = await invoke(
                    prompt,
                    model=ctx.config.agent.model,
                    cwd=ctx.working_dir,
                    max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
                )
                ctx.add_cost(llm_result.cost_usd)
            except RuntimeError as exc:
                log.error("step.validate.llm_failed", error=str(exc))
                return StepResult(
                    success=False,
                    summary=f"Pre-push hook failed and auto-fix failed: {exc}",
                    error=str(exc),
                )

            retry = await run(hook, "origin", "unused", cwd=cwd, timeout=120)
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
            summary=f"Pre-push hook still failing after {_MAX_FIX_ATTEMPTS} fix attempts",
            error=hook_output[:1000],
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: commits must still exist after validation fixes."""
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        if log_result.success and log_result.stdout.strip():
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="No commits ahead of base after validation")
