"""Step 7: Commit -- stage and commit all changes before pushing."""

from __future__ import annotations

import re

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git import operations as git_ops
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.commit")

_AGENT_ARTIFACT_PREFIXES = (".claude/", ".agent-", ".sova-")

_CONVENTIONAL_COMMIT_RE = re.compile(r"^(feat|fix|refactor|test|docs|chore)\([^)]+\):\s*")


def _is_agent_artifact(path: str) -> bool:
    """Check if a path is an agent/tool artifact that shouldn't be committed."""
    return any(path.startswith(p) for p in _AGENT_ARTIFACT_PREFIXES)


def _normalize_commit_subject(
    subject: str,
    *,
    commit_type: str = "feat",
    default_scope: str = "core",
) -> str:
    """Normalize a commit subject to conventional commit format."""
    subject = " ".join(subject.strip().split())
    subject = re.sub(r"^(feat|fix|refactor|test|docs|chore):\s*", "", subject)
    subject = re.sub(r"^(feat|fix|refactor|test|docs|chore)\([^)]+\):\s*", "", subject)

    if not subject:
        subject = "update issue implementation"

    normalized = f"{commit_type}({default_scope}): {subject}"

    if not _CONVENTIONAL_COMMIT_RE.match(normalized):
        raise ValueError(f"Invalid conventional commit message: {normalized}")

    return normalized


def _resolve_task_title(ctx: ExecutionContext) -> str:
    """Resolve a human-readable title from context, preferring task.title."""
    if ctx.task:
        return ctx.task.title
    if ctx.has_issue:
        return f"issue {ctx.issue_number}"
    return ctx.run_label or "run"


class CommitStep(BaseStep):
    name = "commit"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.commit", issue=ctx.issue_number, cwd=str(ctx.working_dir))

        has_changes = await self._detect_changes(ctx)
        if not has_changes:
            return await self._handle_no_changes(ctx)

        message = self._build_commit_message(ctx)

        try:
            await git_ops.commit(message, cwd=ctx.working_dir)
            return StepResult(success=True, summary=f"Committed: {message}")
        except RuntimeError as exc:
            return StepResult(success=False, summary="Commit failed", error=str(exc))

    async def _detect_changes(self, ctx: ExecutionContext) -> bool:
        diff_result = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        untracked = await run("git", "ls-files", "--others", "--exclude-standard", cwd=ctx.working_dir)

        has_unstaged = bool(diff_result.success and diff_result.stdout.strip())
        has_staged = bool(staged.success and staged.stdout.strip())

        untracked_files = [f for f in untracked.stdout.strip().splitlines() if f.strip()] if untracked.success else []
        meaningful_untracked = [f for f in untracked_files if not _is_agent_artifact(f)]

        return has_unstaged or has_staged or bool(meaningful_untracked)

    async def _handle_no_changes(self, ctx: ExecutionContext) -> StepResult:
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_commits = bool(log_result.success and log_result.stdout.strip())
        if has_commits:
            return StepResult(success=True, summary="Nothing to commit, commits already exist")
        return StepResult(success=False, summary="No changes to commit", error="No changes to commit")

    @staticmethod
    def _build_commit_message(ctx: ExecutionContext) -> str:
        is_address_review = ctx.pr_number is not None and "address_review" in ctx.completed_steps

        if is_address_review:
            label = f"issue {ctx.issue_number}" if ctx.has_issue else (ctx.run_label or "run")
            subject = f"address review findings for {label}"
            return _normalize_commit_subject(subject, commit_type="fix", default_scope="core")

        title = _resolve_task_title(ctx)
        normalized = _normalize_commit_subject(title, default_scope="core")
        if ctx.has_issue:
            return f"{normalized}\n\nCloses #{ctx.issue_number}"
        return normalized

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: branch must have at least one commit ahead of base."""
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_commits = bool(log_result.success and log_result.stdout.strip())
        if has_commits:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="No commits ahead of base branch after commit step")
