"""Step: Ensure worktree exists for the address-review pipeline.

Unlike WorktreeStep (which creates a new branch), this step verifies that
a worktree exists for an already-existing PR branch and creates one if missing.
"""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git.worktree import create_worktree, find_worktree_by_branch
from sova.utils.logging import get_logger

log = get_logger(component="step.ensure_worktree")


class EnsureWorktreeStep(BaseStep):
    name = "ensure_worktree"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if ctx.worktree_dir and ctx.worktree_dir.exists():
            return StepResult(success=True, summary=f"Worktree already exists at {ctx.worktree_dir}")

        if not ctx.branch_name:
            return StepResult(
                success=False,
                summary="No branch name available",
                error="EnsureWorktreeStep requires branch_name to be set (from PR metadata or handoff)",
            )

        try:
            wt_path = await find_worktree_by_branch(ctx.branch_name, cwd=ctx.project_dir)
            if wt_path is not None and wt_path.resolve() != ctx.project_dir.resolve():
                ctx.worktree_dir = wt_path
                log.info("step.ensure_worktree.found", branch=ctx.branch_name, path=str(wt_path))
                return StepResult(success=True, summary=f"Found existing worktree at {wt_path}")
        except Exception:
            log.debug("step.ensure_worktree.lookup_failed", branch=ctx.branch_name, exc_info=True)

        if ctx.has_issue:
            wt_id = ctx.issue_number.lstrip("#").strip()
        elif ctx.pr_number:
            wt_id = f"pr-{ctx.pr_number}"
        else:
            wt_id = ctx.branch_name.replace("/", "-").replace(" ", "-")[:50]

        if not wt_id.strip("-"):
            return StepResult(
                success=False,
                summary="Cannot determine worktree ID",
                error=f"No valid worktree ID: issue={ctx.issue_number}, pr={ctx.pr_number}, branch={ctx.branch_name}",
            )

        try:
            info = await create_worktree(
                issue_id=wt_id,
                branch=ctx.branch_name,
                base_branch="HEAD",
                project_dir=ctx.project_dir,
            )
            ctx.worktree_dir = info.path
            log.info("step.ensure_worktree.created", wt_id=wt_id, path=str(info.path))
            return StepResult(success=True, summary=f"Created worktree at {info.path}")
        except RuntimeError as exc:
            return StepResult(success=False, summary="Failed to create worktree", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        if ctx.worktree_dir and ctx.worktree_dir.exists():
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="Worktree directory does not exist after ensure step")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return ctx.worktree_dir is not None and ctx.worktree_dir.exists()
