"""Step 7: Commit -- stage and commit all changes before pushing."""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git import operations as git_ops
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.commit")

_AGENT_ARTIFACT_PREFIXES = (".claude/", ".agent-", ".sova-")


def _is_agent_artifact(path: str) -> bool:
    """Check if a path is an agent/tool artifact that shouldn't be committed."""
    return any(path.startswith(p) for p in _AGENT_ARTIFACT_PREFIXES)


class CommitStep(BaseStep):
    name = "commit"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.commit", issue=ctx.issue_number, cwd=str(ctx.working_dir))

        diff_result = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        untracked = await run("git", "ls-files", "--others", "--exclude-standard", cwd=ctx.working_dir)

        has_unstaged = bool(diff_result.success and diff_result.stdout.strip())
        has_staged = bool(staged.success and staged.stdout.strip())

        untracked_files = [f for f in untracked.stdout.strip().splitlines() if f.strip()] if untracked.success else []
        meaningful_untracked = [f for f in untracked_files if not _is_agent_artifact(f)]
        has_meaningful_untracked = bool(meaningful_untracked)

        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_commits = bool(log_result.success and log_result.stdout.strip())

        if not has_unstaged and not has_staged and not has_meaningful_untracked:
            if has_commits:
                return StepResult(success=True, summary="Nothing to commit, commits already exist")
            return StepResult(success=False, summary="No changes to commit", error="No changes to commit")

        task = ctx.task
        title = task.title if task else f"issue #{ctx.issue_number}"
        message = f"feat: {title}\n\nCloses #{ctx.issue_number}"

        try:
            await git_ops.commit(message, cwd=ctx.working_dir)
            return StepResult(success=True, summary=f"Committed: {message}")
        except RuntimeError as exc:
            return StepResult(success=False, summary="Commit failed", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: branch must have at least one commit ahead of base."""
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_commits = bool(log_result.success and log_result.stdout.strip())
        if has_commits:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="No commits ahead of base branch after commit step")
