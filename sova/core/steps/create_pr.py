"""Step 9: Create PR -- generate a structured description and open a pull request."""

from __future__ import annotations

import asyncio
import re

from sova.adapters.base import TaskState
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git import operations as git_ops
from sova.utils.formatting import truncate
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.create_pr")

_PLACEHOLDER = "(none)"

_ISSUE_BODY_EXCERPT_LIMIT = 500

_CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|refactor|test|docs|chore|ci)"
    r"(?:\([^)]*\))?"
    r":\s*",
)


def _build_pr_title(task_title: str, issue_number: str | None) -> str:
    """Build a PR title from the task title, avoiding double conventional prefixes.

    If the task title already has a conventional commit prefix (e.g. "feat(llm): ..."),
    strip it and re-wrap with the issue-scoped prefix to produce a clean title like
    "feat(#117): add local model support via Ollama".
    """
    match = _CONVENTIONAL_RE.match(task_title)
    if match:
        commit_type = match.group(1)
        description = task_title[match.end() :]
    else:
        commit_type = "feat"
        description = task_title

    if issue_number:
        return f"{commit_type}(#{issue_number}): {description}"
    return f"{commit_type}: {description}"


class CreatePRStep(BaseStep):
    name = "create_pr"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.create_pr", label=ctx.display_label, branch=ctx.branch_name)

        adopted = await self._try_adopt_existing_pr(ctx)
        if adopted:
            return adopted

        task_title = ctx.task.title if ctx.task else ctx.branch_name
        title = _build_pr_title(task_title, ctx.issue_number if ctx.has_issue else None)
        body = await self._generate_pr_body(ctx, task_title)

        try:
            pr_info = await git_ops.create_pr(
                title=title,
                body=body,
                base=ctx.base_branch,
                head=ctx.branch_name,
                repo=ctx.repo,
            )
            ctx.pr_number = pr_info.number
            ctx.pr_url = pr_info.url
            await self._post_create_side_effects(ctx, pr_info.number)
            return StepResult(success=True, summary=f"Created PR #{pr_info.number}")
        except RuntimeError as exc:
            return StepResult(success=False, summary="Failed to create PR", error=str(exc))

    async def _try_adopt_existing_pr(self, ctx: ExecutionContext) -> StepResult | None:
        if not ctx.has_issue:
            return None
        existing = await git_ops.find_pr_for_issue(
            ctx.issue_number,
            repo=ctx.repo,
            github_user=ctx.config.github_user,
        )
        if not existing:
            return None
        log.info("step.create_pr.existing_found", pr=existing.number)
        ctx.pr_number = existing.number
        ctx.pr_url = existing.url
        try:
            await ctx.adapter.transition_state(ctx.issue_number, TaskState.IN_REVIEW)
        except Exception:
            log.warning("step.create_pr.tracker_update_failed", exc_info=True)
        return StepResult(success=True, summary=f"Adopted existing PR #{existing.number}")

    async def _post_create_side_effects(self, ctx: ExecutionContext, pr_number: int) -> None:
        if ctx.config.github_user:
            try:
                await git_ops.assign_pr(
                    pr_number,
                    assignee=ctx.config.github_user,
                    repo=ctx.repo,
                    github_user=ctx.config.github_user,
                )
            except Exception:
                log.warning("step.create_pr.assign_failed", exc_info=True)
        if ctx.has_issue:
            try:
                await ctx.adapter.transition_state(ctx.issue_number, TaskState.IN_REVIEW)
            except Exception:
                log.warning("step.create_pr.tracker_update_failed", exc_info=True)

    async def _generate_pr_body(self, ctx: ExecutionContext, task_title: str) -> str:
        log_result, diff_result = await asyncio.gather(
            run(
                "git",
                "log",
                f"{ctx.base_branch}..HEAD",
                "--format=%h %s%n%b",
                "--no-merges",
                cwd=ctx.working_dir,
            ),
            run(
                "git",
                "diff",
                f"{ctx.base_branch}..HEAD",
                "--stat",
                cwd=ctx.working_dir,
            ),
        )

        commit_log = log_result.stdout.strip() if log_result.success else "(unavailable)"
        diff_stat = diff_result.stdout.strip() if diff_result.success else "(unavailable)"
        return self._build_pr_body(ctx, task_title, commit_log, diff_stat)

    @staticmethod
    def _build_pr_body(ctx: ExecutionContext, task_title: str, commit_log: str, diff_stat: str) -> str:
        lines = [
            "## Summary",
            "",
            f"Automated changes for: {task_title}",
            "",
        ]
        if ctx.has_issue:
            lines.append(f"Closes #{ctx.issue_number}")
            lines.append("")

        issue_body = ctx.task.body if ctx.task else ""
        if issue_body:
            excerpt = truncate(issue_body.strip(), max_length=_ISSUE_BODY_EXCERPT_LIMIT)
            lines.extend(["## Context", "", excerpt, ""])

        lines.extend(
            [
                "## Commits",
                "",
                "```",
                commit_log or _PLACEHOLDER,
                "```",
                "",
                "## Files changed",
                "",
                "```",
                diff_stat or _PLACEHOLDER,
                "```",
            ]
        )
        return "\n".join(lines)

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: PR number must have been extracted."""
        if ctx.pr_number is not None:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="No PR number after create_pr step")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        # Skip if PR already exists (resumed run)
        return ctx.pr_number is not None
