"""Step: Rearrange commits -- reorganize branch history into clean, logical commits.

Invokes /rearrange-commits in the working directory so that review fixes are
folded back into the original commits rather than appended as separate
"address review" commits. The result is a history that reads as if the code
was written correctly from the start.

Used in the address-review pipeline in place of CommitStep.
"""

from __future__ import annotations

import re

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.llm.client import invoke_command
from sova.utils.logging import get_logger
from sova.utils.shell import run

_IGNORABLE_UNTRACKED_RE = re.compile(r"^\.claude/|^\.sova/")

log = get_logger(component="step.rearrange_commits")


class RearrangeCommitsStep(BaseStep):
    name = "rearrange_commits"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.rearrange_commits", branch=ctx.branch_name, base=ctx.base_branch)

        try:
            result = await invoke_command(
                "/rearrange-commits",
                model=ctx.config.agent.model,
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
                timeout=ctx.config.agent.step_timeout,
            )
            ctx.add_cost(result.cost_usd)
            return StepResult(
                success=True,
                summary="Commits reorganized into clean logical units",
                cost_usd=result.cost_usd,
            )
        except RuntimeError as exc:
            return StepResult(success=False, summary="Commit reorganization failed", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: branch must have at least one commit ahead of base with no uncommitted changes."""
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_commits = bool(log_result.success and log_result.stdout.strip())
        if not has_commits:
            return GateCheckResult(passed=False, reason="No commits ahead of base after rearranging")

        diff_result = await run(
            "git", "diff", "--stat", "HEAD", "--", ".", ":(exclude).claude/agent-memory/", cwd=ctx.working_dir
        )
        staged = await run(
            "git", "diff", "--cached", "--stat", "--", ".", ":(exclude).claude/agent-memory/", cwd=ctx.working_dir
        )
        has_uncommitted = bool(
            (diff_result.success and diff_result.stdout.strip()) or (staged.success and staged.stdout.strip())
        )
        if has_uncommitted:
            return GateCheckResult(passed=False, reason="Uncommitted changes remain after rearranging")

        status_result = await run("git", "status", "--porcelain", cwd=ctx.working_dir)
        if not status_result.success:
            return GateCheckResult(passed=False, reason="git status failed")
        untracked_lines = [
            line[3:].strip()
            for line in status_result.stdout.splitlines()
            if line.startswith("??") and not _IGNORABLE_UNTRACKED_RE.search(line[3:].strip())
        ]
        if untracked_lines:
            return GateCheckResult(
                passed=False,
                reason=f"Untracked files remain after rearranging: {', '.join(untracked_lines[:5])}",
            )

        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps
