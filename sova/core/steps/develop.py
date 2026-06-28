"""Step 4: Develop -- invoke Claude CLI for TDD implementation.

This is the critical step where actual code is written. The gate check
ensures that development actually produced code changes (preventing the
issue #60 failure mode where the agent ran through the entire pipeline
with zero changes).
"""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.llm.client import invoke, invoke_command
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.develop")


async def _append_implementation_notes(ctx: ExecutionContext) -> None:
    """Summarize implementation deviations and append to spec (non-fatal)."""
    try:
        import asyncio

        from sova.core.steps._spec_helpers import (
            SECTION_IMPLEMENTATION_NOTES,
            SPEC_PLAN_SECTIONS,
            append_spec_section,
            read_spec_sections,
        )

        original_plan = read_spec_sections(ctx.issue_number, ctx.working_dir, SPEC_PLAN_SECTIONS)
        if not original_plan:
            return

        diff_task = run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        log_task = run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        diff_result, log_result = await asyncio.gather(diff_task, log_task)

        diff_stat = diff_result.stdout.strip() if diff_result.success else "(unavailable)"
        commit_log = log_result.stdout.strip() if log_result.success else "(no commits)"

        prompt = f"""You are a technical writer. Given the original spec and the actual implementation, \
produce a concise "Implementation Notes" section listing:
- Deviations from the spec's proposed approach
- Unexpected constraints discovered during implementation
- Key architectural choices not covered in the original spec

If the implementation matches the spec exactly, return "Implementation followed the spec as designed."

## Original Spec Plan
{original_plan}

## Implementation Summary
Diff stats:
{diff_stat}

Commits:
{commit_log}

Return ONLY the section content (no heading, no markdown fences). Keep it under 10 bullet points."""

        llm_result = await invoke(prompt, model="haiku", cwd=ctx.working_dir, timeout=60)
        ctx.add_cost(llm_result.cost_usd)

        append_spec_section(ctx.issue_number, SECTION_IMPLEMENTATION_NOTES, llm_result.text.strip(), ctx.working_dir)
    except Exception:
        log.warning("step.develop.implementation_notes_failed", exc_info=True)


class DevelopStep(BaseStep):
    name = "develop"
    max_retries = 1

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.develop", issue=ctx.issue_number, cwd=str(ctx.working_dir))

        try:
            result = await invoke_command(
                "/develop",
                args=ctx.issue_number,
                model=ctx.config.agent.model,
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
                timeout=ctx.config.agent.step_timeout,
            )
            ctx.add_cost(result.cost_usd)
            ctx.session_id = result.session_id

            await _append_implementation_notes(ctx)

            return StepResult(
                success=True,
                summary=f"Development completed ({result.total_tokens} tokens)",
                cost_usd=result.cost_usd,
            )
        except RuntimeError as exc:
            return StepResult(success=False, summary="Development failed", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: development must produce actual code changes."""
        diff_result = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        has_changes = bool(
            (diff_result.success and diff_result.stdout.strip()) or (staged.success and staged.stdout.strip())
        )
        # Also check commits ahead of base branch (Claude may have committed)
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_commits = bool(log_result.success and log_result.stdout.strip())

        if has_changes or has_commits:
            return GateCheckResult(passed=True)
        return GateCheckResult(
            passed=False,
            reason="Development produced no code changes",
        )
