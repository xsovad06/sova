"""Step 3: Create an isolated git worktree for development."""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git import worktree
from sova.utils.logging import get_logger

log = get_logger(component="step.worktree")


class WorktreeStep(BaseStep):
    name = "create_worktree"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if not ctx.branch_name:
            if ctx.has_issue:
                ctx.branch_name = f"feat/issue-{ctx.issue_number}"
            else:
                ctx.branch_name = f"feat/{ctx.run_label or 'run'}"

        worktree_id = ctx.issue_number or ctx.run_label or f"run-{ctx.task_run_id or 'tmp'}"
        log.info("step.worktree", label=ctx.display_label, branch=ctx.branch_name)

        try:
            info = await worktree.create_worktree(
                issue_id=worktree_id,
                branch=ctx.branch_name,
                base_branch=ctx.config.base_branch,
                project_dir=ctx.project_dir,
                copy_files=ctx.config.worktree.copy_files,
            )
            ctx.worktree_dir = info.path
            return StepResult(success=True, summary=f"Created worktree at {info.path}")
        except RuntimeError as exc:
            return StepResult(success=False, summary="Failed to create worktree", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        if ctx.worktree_dir and ctx.worktree_dir.exists():
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="Worktree directory does not exist")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        # Skip if worktree already exists (e.g., resumed run)
        return ctx.worktree_dir is not None and ctx.worktree_dir.exists()
