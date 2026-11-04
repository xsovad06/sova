"""Step 3: Create an isolated git worktree for development."""

from __future__ import annotations

import re
import uuid

from sova.core.check_cache import clear_marker
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git import worktree
from sova.utils.logging import get_logger

log = get_logger(component="step.worktree")


def _sanitize_label(label: str) -> str:
    """Sanitize a label for safe use in branch names and filesystem paths."""
    return re.sub(r"[^a-zA-Z0-9_-]", "-", label).strip("-") or "run"


def default_branch_name(ctx: ExecutionContext) -> str:
    """Branch a development run creates when the context names none."""
    if ctx.has_issue:
        ts = ctx.config.task_source if ctx.config else None
        if ts and ts.is_jira:
            jira_key = ts.jira_issue_key(ctx.issue_number)
            slug = _sanitize_label(ctx.task.title) if ctx.task else ""
            return f"feat/{jira_key}-{slug}" if slug else f"feat/{jira_key}"
        return f"feat/issue-{ctx.issue_number}"
    safe_label = _sanitize_label(ctx.run_label) if ctx.run_label else "run"
    return f"feat/{safe_label}"


def start_fresh_after_closed_pr(ctx: ExecutionContext, closed_pr: int) -> None:
    """Point *ctx* at a new development cycle once *closed_pr* is confirmed merged or closed.

    The closed PR's branch, worktree and completed steps belong to that PR: keeping them would let
    WorktreeStep reuse its worktree and a resume skip steps such as validate or push. The new branch
    carries the closed PR's number, so it can never be that PR's branch, which existed before the PR did.
    """
    ctx.pr_number = None
    ctx.worktree_dir = None
    ctx.completed_steps = frozenset()
    ctx.branch_name = f"{default_branch_name(ctx)}-after-pr-{closed_pr}"


class WorktreeStep(BaseStep):
    name = "create_worktree"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if not ctx.branch_name:
            ctx.branch_name = default_branch_name(ctx)

        raw_id = ctx.issue_number or ctx.run_label or f"run-{ctx.task_run_id or uuid.uuid4().hex[:8]}"
        worktree_id = _sanitize_label(raw_id) if not ctx.issue_number else raw_id
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
            # A reused worktree (same path, different issue/branch across
            # cycles) must not inherit a stale marker whose hash happens to
            # coincidentally match the new cycle's content.
            clear_marker(info.path)
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
