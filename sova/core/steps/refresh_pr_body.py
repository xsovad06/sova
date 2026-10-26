"""Step: Refresh PR body. Regenerate the PR description after address-review changes the diff."""

from __future__ import annotations

import asyncio

from sova.core.context import ExecutionContext
from sova.core.steps._pr_body import (
    _ISSUE_SECTION_HEADING,
    _closing_keyword_re,
    _jira_ticket_link,
    apply_sections,
    build_prompt,
    compose_body,
    process_llm_result,
    split_sections,
    title_from_branch,
    truncate_diff,
)
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git.pr import get_pr_body, update_pr_body
from sova.llm.client import invoke
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.refresh_pr_body")


class RefreshPrBodyStep(BaseStep):
    name = "refresh_pr_body"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.refresh_pr_body", pr=ctx.pr_number)

        if not ctx.pr_number:
            return StepResult(success=True, summary="No PR to refresh")

        task_title, issue_body, resolved = await self._resolve_task(ctx)

        diffs = await self._collect_diff(ctx)
        if diffs is None:
            return StepResult(success=True, summary="PR body refresh skipped: could not collect the branch diff")
        commit_log, diff_stat, diff_content = diffs

        prompt, criteria = build_prompt(
            ctx,
            task_title=task_title,
            issue_body=issue_body,
            commit_log=commit_log,
            diff_stat=diff_stat,
            diff_content=diff_content,
        )

        model = ctx.resolved_model or ctx.config.agent.model
        try:
            result = await invoke(prompt, model=model, task_type="pr_body", cwd=ctx.working_dir, timeout=120)
        except Exception as exc:  # noqa: BLE001 (advisory step; a provider or guard failure must not abort the round)
            log.warning("step.refresh_pr_body.llm_failed", pr=ctx.pr_number, exc_info=True)
            return StepResult(success=True, summary=f"PR body refresh skipped (non-fatal): {exc}")

        ctx.add_usage(result)
        sections = self._build_sections(ctx, result.text, criteria, resolved=resolved)
        if not sections:
            log.warning("step.refresh_pr_body.no_sections", pr=ctx.pr_number)
            return StepResult(success=True, summary="PR body refresh skipped: LLM output had no sections")

        try:
            current_body = await get_pr_body(ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user)
            if _ISSUE_SECTION_HEADING in sections:
                # Strip any closing keyword the existing body carries outside a
                # "## Issue" heading (pre-migration bodies, or a round whose
                # heading casing drifted and orphaned the old section), so the
                # fresh decision below is never left sitting beside a stale one.
                current_body = _closing_keyword_re(ctx.issue_number).sub("", current_body)
            new_body = apply_sections(current_body, sections)
            await update_pr_body(ctx.pr_number, body=new_body, repo=ctx.repo, github_user=ctx.config.github_user)
        except Exception:  # noqa: BLE001 (advisory step; a push/auth failure must not abort the round)
            log.warning("step.refresh_pr_body.update_failed", pr=ctx.pr_number, exc_info=True)
            return StepResult(success=True, summary="PR body refresh skipped (non-fatal): could not update PR body")

        return StepResult(success=True, summary=f"Refreshed PR #{ctx.pr_number} body")

    @staticmethod
    async def _collect_diff(ctx: ExecutionContext) -> tuple[str, str, str] | None:
        """Collect (commit_log, diff_stat, diff_content) for the refresh prompt.

        Returns ``None`` when any of the three git calls failed. The caller
        then skips the whole refresh rather than ask the LLM to describe a
        diff it cannot see: the model would still produce a confident
        Summary/Review guidance/Test plan from the issue text alone, which
        would overwrite a correct PR description with unfounded text, and
        every round the failure persists repeats both the mistake and the
        LLM call's cost.

        Two deliberately different ranges: the three-dot form is the
        merge-base diff (what this branch changed), but for ``git log`` it
        is the symmetric difference, which after an address-review round
        would also list the base branch's own new commits. Both use the
        remote-tracking ref, not the local one: the address-review
        pipeline's own rebase step fetches and rebases onto origin/{base}
        without ever moving the local ref, so a stale local base would make
        a base branch that advanced after this PR's fork point pollute both
        the diff and the commit log.
        """
        diff_range = f"origin/{ctx.base_branch}...HEAD"
        log_range = f"origin/{ctx.base_branch}..HEAD"
        log_result, diff_stat_result, diff_content_result = await asyncio.gather(
            run("git", "log", log_range, "--format=%h %s%n%b", "--no-merges", cwd=ctx.working_dir),
            run("git", "diff", diff_range, "--stat", cwd=ctx.working_dir),
            run("git", "diff", diff_range, cwd=ctx.working_dir),
        )
        if not (log_result.success and diff_stat_result.success and diff_content_result.success):
            log.warning(
                "step.refresh_pr_body.diff_unavailable",
                pr=ctx.pr_number,
                log_failed=not log_result.success,
                diff_stat_failed=not diff_stat_result.success,
                diff_content_failed=not diff_content_result.success,
            )
            return None
        commit_log = log_result.stdout.strip()
        diff_stat = diff_stat_result.stdout.strip()
        diff_content = truncate_diff(diff_content_result.stdout.strip(), True)
        return commit_log, diff_stat, diff_content

    @staticmethod
    def _build_sections(ctx: ExecutionContext, llm_text: str, criteria: list[str], *, resolved: bool) -> dict[str, str]:
        """Build the refreshed section map, applying the closing decision.

        ``resolved`` is False when no task (fresh or cached) could be
        obtained: the verdicts were not computed from real evidence, so the
        closing-decision section is dropped rather than overwrite
        create_pr's correct decision with an unconditional "Closes #N" (or a
        false "Part of #N") derived from no evidence at all.
        """
        sections_markdown, verdicts = process_llm_result(llm_text, criteria, ctx.config.commit.owner_only_patterns)

        if ctx.has_issue:
            ts = ctx.config.task_source
            jira_link = _jira_ticket_link(ts, ctx.issue_number) if ts.is_jira else ""
            fresh_body = compose_body(
                sections_markdown, ctx.issue_number, verdicts, is_jira=ts.is_jira, jira_link=jira_link
            )
        else:
            fresh_body = sections_markdown

        sections = split_sections(fresh_body)
        if not resolved and ctx.has_issue:
            sections.pop(_ISSUE_SECTION_HEADING, None)
        return sections

    @staticmethod
    async def _resolve_task(ctx: ExecutionContext) -> tuple[str, str, bool]:
        """Return (task_title, issue_body, resolved), re-fetched from the adapter.

        Address-review rounds can span enough time that the issue's
        acceptance criteria were themselves edited since ``create_pr`` ran;
        a cached ``ctx.task`` would miss that. Falls back to the cached task,
        then the branch name, on a fetch failure.

        ``resolved`` is False when no task (fresh or cached) could be
        obtained at all: the caller must then skip the closing-decision
        update rather than read the resulting empty issue body as "no
        acceptance criteria" and emit an unconditional ``Closes #N``, which
        would re-arm auto-close on a PR this code never actually judged.
        """
        task = ctx.task
        if ctx.has_issue:
            try:
                task = await ctx.adapter.get_task(ctx.issue_number)
                ctx.task = task
            except Exception:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
                log.warning("step.refresh_pr_body.task_refetch_failed", issue=ctx.issue_number, exc_info=True)
                task = ctx.task

        if task:
            return task.title, task.body or "", True
        return title_from_branch(ctx.branch_name), "", False

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: always passes. Advisory step, like ConfidenceScoreStep: a
        failure to refresh the body must never block the address-review
        pipeline, so execute() never returns success=False."""
        return GateCheckResult(passed=True)
