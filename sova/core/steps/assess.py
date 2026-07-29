"""Step 2: Assess -- verify the issue is ready for development.

Gate 3: The Developer agent refuses to pick up any issue not in
"Researched" state. This prevents the old failure mode where the agent
blindly started work on underspecified issues.

When an existing open PR is found for the issue, AssessStep adopts it into
ctx.pr_number and ctx.branch_name so the developer pipeline can continue
without creating a duplicate PR. This does NOT switch to the address-review
pipeline variant; variant selection happens in DeveloperRole before any steps
run, driven by --pr passed to the subprocess at spawn time. See
_recover_last_pr_number() in agent_lifecycle for the primary routing path.
"""

from __future__ import annotations

from sova.adapters.base import TaskState
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git.pr import find_pr_for_issue, get_pr_branch
from sova.llm.client import resolve_model
from sova.llm.complexity import assess_complexity
from sova.utils.logging import get_logger

log = get_logger(component="step.assess")

_READY_STATES = frozenset({TaskState.RESEARCHED, TaskState.IN_PROGRESS})


class AssessStep(BaseStep):
    name = "assess"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        task = await ctx.adapter.get_task(ctx.issue_number)
        ctx.task = task
        state = await ctx.adapter.get_state(ctx.issue_number)

        # Assess complexity and resolve model for this task.
        # file_count_estimate is intentionally omitted: the file count can only be
        # determined from plan/spec output, which is not available at assess-time.
        complexity = assess_complexity(
            title=task.title,
            description=task.body,
            labels=task.labels,
        )
        ctx.complexity = complexity

        # Resolve model based on complexity and role
        resolved = resolve_model(
            role=ctx.role,
            roles_config=ctx.config.roles,
            complexity=complexity,
            llm_config=ctx.config.llm,
        )
        if resolved:
            ctx.resolved_model, ctx.model_selection_reason = resolved
        else:
            # Fallback to default model when no complexity-based routing applies
            ctx.resolved_model = ctx.config.agent.model
            ctx.model_selection_reason = "default (no complexity override)"

        log.info(
            "step.assess.model_routing",
            issue=ctx.issue_number,
            complexity=complexity.value,
            model=ctx.resolved_model,
            reason=ctx.model_selection_reason,
        )

        log.info("step.assess", issue=ctx.issue_number, tracker_state=state, complexity=complexity.value)

        if state not in _READY_STATES:
            return StepResult(
                success=False,
                summary=f"Issue #{ctx.issue_number} is in {state} state, not ready for development",
                error=f"Issue must be in {', '.join(_READY_STATES)} state (current: {state})",
            )

        if not ctx.pr_number and not ctx.force:
            existing_pr = await find_pr_for_issue(
                ctx.issue_number,
                repo=ctx.repo,
                github_user=ctx.config.github_user,
            )
            if existing_pr:
                log.info(
                    "step.assess.adopting_existing_pr",
                    issue=ctx.issue_number,
                    pr=existing_pr.number,
                )
                ctx.pr_number = existing_pr.number
                branch = existing_pr.branch or await get_pr_branch(
                    existing_pr.number,
                    repo=ctx.repo,
                    github_user=ctx.config.github_user,
                )
                if branch:
                    ctx.branch_name = branch
                return StepResult(
                    success=True,
                    summary=(
                        f"Adopted existing PR #{existing_pr.number} for issue #{ctx.issue_number} "
                        "(address-review pipeline)"
                    ),
                )

        return StepResult(success=True, summary=f"Issue #{ctx.issue_number} is in {state} state")

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        # Re-run assessment if routing state is missing on resume (unless force/no-issue)
        if ctx.resolved_model is None and not (ctx.force or not ctx.has_issue):
            return False
        return self.name in ctx.completed_steps or ctx.force or not ctx.has_issue
