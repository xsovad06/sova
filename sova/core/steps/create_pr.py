"""Step 9: Create PR -- generate a structured description and open a pull request."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from sova.adapters.base import TaskState
from sova.config.models import TaskSourceConfig
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git import operations as git_ops
from sova.llm.client import invoke
from sova.utils.formatting import truncate
from sova.utils.logging import get_logger
from sova.utils.markdown import strip_preamble
from sova.utils.shell import run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(component="step.create_pr")

_PLACEHOLDER = "(none)"

_ISSUE_BODY_EXCERPT_LIMIT = 500

_CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|refactor|test|docs|chore|ci)"
    r"(?:\([^)]*\))?"
    r":\s*",
)

_PR_BODY_PROMPT_BASE = """\
Generate a pull request description for the changes below. Output ONLY the \
markdown body (no fences, no commentary, no preamble). Start directly with \
the first heading. Do not use emojis or icons. Use this structure:

## Summary
1-3 bullet points: WHAT changed and WHY.

## Changes
Brief description of each logical change grouped by area.

## Review guidance
What should a reviewer focus on? Any trade-offs or shortcuts?

## Test plan
How were these changes verified?
"""

_PR_BODY_ISSUE_SECTION_GITHUB = """
Closes #{issue_number}

---
Issue #{issue_number}: {issue_title}

{issue_body}

"""

_PR_BODY_ISSUE_SECTION_JIRA = """
JIRA: {jira_link}

Do NOT use "Closes #N", "Fixes #N", or "Resolves #N" syntax.

---
{jira_key}: {issue_title}

{issue_body}

"""

_PR_BODY_NO_ISSUE_SECTION = """
---
Task: {issue_title}

"""

_PR_BODY_CONTEXT = """\
Commits on this branch:
{commit_log}

Files changed:
{diff_stat}
"""


def _build_pr_title(
    task_title: str,
    issue_number: str | None,
    task_source: TaskSourceConfig | None = None,
) -> str:
    """Build a PR title from the task title, avoiding double conventional prefixes.

    For JIRA projects, prepends ``[PROJECT_KEY-NUMBER]`` so JIRA's GitHub
    integration auto-links the PR to the ticket.
    """
    match = _CONVENTIONAL_RE.match(task_title)
    if match:
        commit_type = match.group(1)
        description = task_title[match.end() :]
    else:
        commit_type = "feat"
        description = task_title

    if task_source and task_source.is_jira and issue_number:
        jira_key = f"[{task_source.jira_issue_key(issue_number)}]"
        return f"{jira_key} {commit_type}: {description}"

    if issue_number:
        return f"{commit_type}(#{issue_number}): {description}"
    return f"{commit_type}: {description}"


def _jira_ticket_link(task_source: TaskSourceConfig, issue_number: str) -> str:
    """Return a markdown JIRA ticket link for the PR body."""
    return f"JIRA: {task_source.jira_browse_url(issue_number)}"


_BRANCH_PREFIX_RE = re.compile(r"^(?:feat|fix|refactor|chore)/")
_ISSUE_PREFIX_RE = re.compile(r"^issue-\d+-?")
_JIRA_KEY_PREFIX_RE = re.compile(r"^[A-Z]+-\d+-?")


def _title_from_branch(branch: str) -> str:
    """Extract a human-readable title from a branch name."""
    raw = _BRANCH_PREFIX_RE.sub("", branch)
    raw = _ISSUE_PREFIX_RE.sub("", raw)
    raw = _JIRA_KEY_PREFIX_RE.sub("", raw)
    title = raw.replace("-", " ").strip()
    return title if title else "update"


class CreatePRStep(BaseStep):
    name = "create_pr"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.create_pr", label=ctx.display_label, branch=ctx.branch_name)

        # Adopted PR skips throttle (edge case 5)
        adopted = await self._try_adopt_existing_pr(ctx)
        if adopted:
            return adopted

        task_title = ctx.task.title if ctx.task else _title_from_branch(ctx.branch_name)
        title = _build_pr_title(
            task_title,
            ctx.issue_number if ctx.has_issue else None,
            task_source=ctx.config.task_source,
        )
        body = await self._generate_pr_body(ctx, task_title)

        if ctx.config.coderabbit_quota.enabled:
            return await self._create_pr_throttled(ctx, title, body)

        return await self._create_pr_immediate(ctx, title, body)

    async def _create_pr_immediate(self, ctx: ExecutionContext, title: str, body: str) -> StepResult:
        """Create PR directly (no throttle)."""
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
            adopted = await self._try_adopt_from_error(ctx, str(exc))
            if adopted:
                return adopted
            return StepResult(success=False, summary="Failed to create PR", error=str(exc))

    async def _create_pr_throttled(self, ctx: ExecutionContext, title: str, body: str) -> StepResult:
        """Enqueue PR creation and poll until the background processor creates it."""
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import enqueue, poll_until_created

        if ctx.task_run_id is None:
            log.warning("step.create_pr.no_task_run_id_for_throttle")
            return await self._create_pr_immediate(ctx, title, body)

        try:
            async with await get_session(project_dir=ctx.project_dir) as session:
                async with session.begin():
                    entry_id = await enqueue(
                        session,
                        task_run_id=ctx.task_run_id,
                        issue_number=ctx.issue_number if ctx.has_issue else None,
                        title=title,
                        body=body,
                        base_branch=ctx.base_branch,
                        head_branch=ctx.branch_name,
                        repo=ctx.repo,
                        github_user=ctx.config.github_user,
                        project_slug=ctx.config.github_repo,
                    )
        except Exception as exc:
            log.warning("step.create_pr.enqueue_failed", error=str(exc), exc_info=True)
            return await self._create_pr_immediate(ctx, title, body)

        log.info("step.create_pr.queued", entry_id=entry_id, label=ctx.display_label)

        # Session factory for polling
        async def _session_factory() -> AsyncSession:
            return await get_session(project_dir=ctx.project_dir)

        result = await poll_until_created(_session_factory, entry_id)
        if result is None:
            return StepResult(success=False, summary="PR creation timed out in queue")

        from sova.db.models import PRQueueStatus

        if result["status"] == PRQueueStatus.CREATED and result["pr_number"]:
            ctx.pr_number = result["pr_number"]
            ctx.pr_url = result.get("pr_url", "")
            return StepResult(success=True, summary=f"Created PR #{result['pr_number']} (throttled)")

        error = result.get("error_message", "Unknown error")
        return StepResult(success=False, summary=f"PR creation failed in queue: {error}")

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

    async def _try_adopt_from_error(self, ctx: ExecutionContext, error_msg: str) -> StepResult | None:
        """Parse 'already exists' error from gh CLI and adopt the existing PR."""
        if "already exists" not in error_msg:
            return None
        match = re.search(r"/pull/(\d+)", error_msg)
        if not match:
            return None
        pr_number = int(match.group(1))
        log.info("step.create_pr.adopted_from_error", pr=pr_number)
        ctx.pr_number = pr_number
        url_match = re.search(r"(https://github\.com/\S+/pull/\d+)", error_msg)
        ctx.pr_url = url_match.group(1) if url_match else ""
        await self._post_create_side_effects(ctx, pr_number)
        return StepResult(success=True, summary=f"Adopted existing PR #{pr_number}")

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

        issue_body = ctx.task.body if ctx.task else ""
        commit_log = log_result.stdout.strip() if log_result.success else "(unavailable)"
        diff_stat = diff_result.stdout.strip() if diff_result.success else "(unavailable)"

        ts = ctx.config.task_source

        if ctx.has_issue:
            if ts.is_jira:
                jira_key = ts.jira_issue_key(ctx.issue_number)
                jira_link = _jira_ticket_link(ts, ctx.issue_number)
                middle = _PR_BODY_ISSUE_SECTION_JIRA.format(
                    jira_link=jira_link,
                    jira_key=jira_key,
                    issue_title=task_title,
                    issue_body=issue_body or "(no description)",
                )
            else:
                middle = _PR_BODY_ISSUE_SECTION_GITHUB.format(
                    issue_number=ctx.issue_number,
                    issue_title=task_title,
                    issue_body=issue_body or "(no description)",
                )
        else:
            middle = _PR_BODY_NO_ISSUE_SECTION.format(issue_title=task_title)

        prompt = (
            _PR_BODY_PROMPT_BASE
            + middle
            + _PR_BODY_CONTEXT.format(
                commit_log=commit_log,
                diff_stat=diff_stat,
            )
        )

        try:
            result = await invoke(prompt, model="sonnet", cwd=ctx.working_dir, timeout=120)
        except RuntimeError:
            log.warning("step.create_pr.body_generation_failed", fallback="structured")
            return self._build_fallback_body(ctx, task_title, commit_log, diff_stat)

        ctx.add_cost(result.cost_usd)
        body = strip_preamble(result.text)
        if ctx.has_issue:
            if ts.is_jira:
                close_re = re.compile(
                    rf"(?:closes|fixes|resolves)\s+#{re.escape(ctx.issue_number)}\b",
                    re.IGNORECASE,
                )
                body = close_re.sub("", body)
                jira_link = _jira_ticket_link(ts, ctx.issue_number)
                if jira_link not in body:
                    body += f"\n\n{jira_link}"
            elif f"#{ctx.issue_number}" not in body:
                body += f"\n\nCloses #{ctx.issue_number}"
        return body

    @staticmethod
    def _build_fallback_body(ctx: ExecutionContext, task_title: str, commit_log: str, diff_stat: str) -> str:
        ts = ctx.config.task_source
        lines = [
            "## Summary",
            "",
            f"Automated changes for: {task_title}",
            "",
        ]
        if ctx.has_issue:
            if ts.is_jira:
                lines.append(_jira_ticket_link(ts, ctx.issue_number))
            else:
                lines.append(f"Closes #{ctx.issue_number}")
            lines.append("")

        issue_body = (ctx.task.body if ctx.task else "") or ""
        stripped_body = issue_body.strip()
        if stripped_body:
            excerpt = truncate(stripped_body, max_length=_ISSUE_BODY_EXCERPT_LIMIT)
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
