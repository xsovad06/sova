"""Step: Ensure worktree exists for the address-review pipeline.

Unlike WorktreeStep (which creates a new branch), this step verifies that
a worktree exists for an already-existing PR branch and creates one if missing.
"""

from __future__ import annotations

from pathlib import Path

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git.worktree import create_worktree, find_worktree_by_branch
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.ensure_worktree")


def _is_project_dir(path: Path | None, project_dir: Path) -> bool:
    """Return True if *path* resolves to *project_dir* (not isolated)."""
    if path is None:
        return False
    try:
        return path.resolve() == project_dir.resolve()
    except OSError:
        return False


class EnsureWorktreeStep(BaseStep):
    name = "ensure_worktree"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if ctx.worktree_dir and ctx.worktree_dir.exists() and not _is_project_dir(ctx.worktree_dir, ctx.project_dir):
            return StepResult(success=True, summary=f"Worktree already exists at {ctx.worktree_dir}")

        if _is_project_dir(ctx.worktree_dir, ctx.project_dir):
            ctx.worktree_dir = None

        if not ctx.branch_name:
            return StepResult(
                success=False,
                summary="No branch name available",
                error="EnsureWorktreeStep requires branch_name to be set (from PR metadata or handoff)",
            )

        try:
            wt_path = await find_worktree_by_branch(ctx.branch_name, cwd=ctx.project_dir)
            if wt_path is not None and not _is_project_dir(wt_path, ctx.project_dir):
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

        base_branch = await self._resolve_base_branch(ctx.branch_name, ctx.project_dir)

        try:
            info = await create_worktree(
                issue_id=wt_id,
                branch=ctx.branch_name,
                base_branch=base_branch,
                project_dir=ctx.project_dir,
            )
            ctx.worktree_dir = info.path
            log.info("step.ensure_worktree.created", wt_id=wt_id, path=str(info.path))
            return StepResult(success=True, summary=f"Created worktree at {info.path}")
        except RuntimeError as exc:
            return StepResult(success=False, summary="Failed to create worktree", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        if ctx.worktree_dir and ctx.worktree_dir.exists() and not _is_project_dir(ctx.worktree_dir, ctx.project_dir):
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="Worktree directory does not exist or is the project root")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return (
            ctx.worktree_dir is not None
            and ctx.worktree_dir.exists()
            and not _is_project_dir(ctx.worktree_dir, ctx.project_dir)
        )

    @staticmethod
    async def _resolve_base_branch(branch_name: str, project_dir: Path) -> str:
        """Resolve a verified local or remote ref for the PR branch.

        Checks local branch first, then remote tracking branch. Falls back
        to the branch name itself (which create_worktree handles via
        git worktree add <path> <branch> for existing branches).
        """
        result = await run("git", "rev-parse", "--verify", branch_name, cwd=project_dir)
        if result.success:
            return branch_name

        result = await run("git", "rev-parse", "--verify", f"origin/{branch_name}", cwd=project_dir)
        if result.success:
            return f"origin/{branch_name}"

        return branch_name
