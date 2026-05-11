"""Step: Extract Memory -- extract reusable learnings before handoff."""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.utils.logging import get_logger

log = get_logger(component="step.extract_memory")


class ExtractMemoryStep(BaseStep):
    name = "extract_memory"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        """Extract learnings from this run's context. Always succeeds."""
        try:
            from sova.knowledge.extraction import extract_memories

            step_summaries = [f"{step}: completed" for step in ctx.completed_steps]

            result = await extract_memories(
                role=ctx.role,
                issue_number=ctx.issue_number,
                repo=ctx.repo,
                task_title=ctx.task.title if ctx.task else f"Issue #{ctx.issue_number}",
                files_changed=ctx.files_changed,
                step_summaries=step_summaries,
                cwd=ctx.working_dir,
            )

            total = result.memories_stored + result.memories_confirmed
            if total > 0:
                summary = f"Extracted {result.memories_stored} new, {result.memories_confirmed} confirmed learnings"
            elif result.error:
                summary = f"Memory extraction failed (non-fatal): {result.error}"
            else:
                summary = "No novel learnings to extract"

            return StepResult(success=True, summary=summary, cost_usd=result.cost_usd)

        except Exception as exc:
            log.warning("step.extract_memory.failed", exc_info=True)
            return StepResult(
                success=True,
                summary=f"Memory extraction failed (non-fatal): {exc}",
            )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)
