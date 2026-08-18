"""Step: Research -- invoke Claude CLI for codebase investigation.

Runs the /research command which explores the codebase interactively
and writes a structured research assessment back to the issue tracker.
"""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.llm.client import invoke_command
from sova.utils.logging import get_logger

log = get_logger(component="step.research")


class ResearchStep(BaseStep):
    name = "research"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.research", issue=ctx.issue_number, cwd=str(ctx.project_dir))

        try:
            result = await invoke_command(
                "/research",
                args=ctx.issue_number,
                model=ctx.resolved_model or ctx.config.agent.model,
                fallback_model=ctx.get_cli_fallback_model(),
                cwd=ctx.project_dir,
                max_budget_usd=ctx.config.agent.max_budget / 5,
                timeout=ctx.config.agent.step_timeout,
            )
            ctx.add_cost(result.cost_usd)

            return StepResult(
                success=True,
                summary=f"Research completed ({result.total_tokens} tokens)",
                cost_usd=result.cost_usd,
            )
        except RuntimeError as exc:
            return StepResult(success=False, summary="Research failed", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: research section must exist in issue body or comments."""
        task = await ctx.adapter.get_task(ctx.issue_number)
        if "## Research" in (task.body or ""):
            return GateCheckResult(passed=True)
        try:
            comments = await ctx.adapter.get_comments(ctx.issue_number)
            if any("## Research" in c for c in comments):
                return GateCheckResult(passed=True)
        except Exception:
            log.warning("step.research.comments_check_failed", issue=ctx.issue_number, exc_info=True)
        return GateCheckResult(
            passed=False,
            reason="Research section not found in issue body or comments",
        )
