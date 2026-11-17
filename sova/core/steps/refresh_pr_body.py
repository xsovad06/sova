"""Step: Refresh PR body. Regenerate the PR description after address-review changes the diff."""

from __future__ import annotations

import asyncio

from sova.core.context import ExecutionContext
from sova.core.steps._pr_body import (
    _ISSUE_SECTION_HEADING,
    _closing_keyword_re,
    _neutralize_inline_closing_keyword,
    apply_sections,
    compose_run_body,
    generate_pr_body_text,
    split_sections,
    split_title_tail,
    title_from_branch,
)
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git.pr import get_pr_body, update_pr_body
from sova.llm.client import invoke
from sova.utils.gh import parse_issue_links
from sova.utils.logging import get_logger
from sova.utils.markdown import extract_section, strip_preamble
from sova.utils.shell import run

log = get_logger(component="step.refresh_pr_body")


class RefreshPrBodyStep(BaseStep):
    name = "refresh_pr_body"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.refresh_pr_body", pr=ctx.pr_number)

        if not ctx.pr_number:
            return StepResult(success=True, summary="No PR to refresh")

        # Read the body and check the Issue section first: a mismatch discards the whole
        # refresh, so the diff collection and the LLM call below would be paid for nothing.
        try:
            current_body = await get_pr_body(ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user)
        except Exception:  # noqa: BLE001 (advisory step; a push/auth failure must not abort the round)
            log.warning("step.refresh_pr_body.fetch_failed", pr=ctx.pr_number, exc_info=True)
            return StepResult(success=True, summary="PR body refresh skipped (non-fatal): could not read PR body")

        mismatch = self._issue_section_mismatch(current_body, ctx.issue_number) if ctx.has_issue else []
        if mismatch:
            # The existing section was written by /pr or create_pr for the
            # PR's issue; the run's own issue is the value in
            # doubt (the resolver can pick the wrong one of several closed
            # issues). The fresh sections would be generated from that
            # issue's title and criteria (PR #1351's "Scope gap against
            # issue #12" landed in Review guidance), so keep the whole body.
            log.warning(
                "step.refresh_pr_body.issue_section_mismatch",
                pr=ctx.pr_number,
                run_issue=ctx.issue_number,
                section_issues=mismatch,
            )
            named = ", ".join(f"#{n}" for n in mismatch)
            return StepResult(
                success=True,
                summary=(
                    f"PR #{ctx.pr_number} body refresh skipped: its Issue section names {named}, "
                    f"not run issue #{ctx.issue_number}"
                ),
            )

        # A body that links the run's issue only as "Part of" (hand-written, or opened before every PR closed its
        # issue) is left as it stands: SOVA cannot tell the two apart, and "Part of" is inert at merge, so
        # refreshing must not flip it to "Closes".
        kept_part_of = ctx.has_issue and self._links_issue_only_as_part_of(current_body, ctx.issue_number)

        task_title, issue_body = await self._resolve_task(ctx)

        diffs = await self._collect_diff(ctx)
        if diffs is None:
            return StepResult(success=True, summary="PR body refresh skipped: could not collect the branch diff")
        commit_log, diff_stat, full_diff = diffs

        try:
            result = await generate_pr_body_text(
                ctx,
                invoke,
                task_title=task_title,
                issue_body=issue_body,
                commit_log=commit_log,
                diff_stat=diff_stat,
                full_diff=full_diff,
                diff_succeeded=True,
                request_title=False,
            )
        except Exception as exc:  # noqa: BLE001 (advisory step; a provider or guard failure must not abort the round)
            log.warning("step.refresh_pr_body.llm_failed", pr=ctx.pr_number, exc_info=True)
            return StepResult(success=True, summary=f"PR body refresh skipped (non-fatal): {exc}")

        ctx.add_usage(result)
        # This step only refreshes the PR body, never the title: the prompt
        # asks for no title (request_title=False), but a title block the model
        # emits anyway is stripped so it cannot leak into the sections below.
        llm_text, _title_candidate = split_title_tail(result.text)
        sections = self._build_sections(ctx, llm_text)
        if not sections:
            log.warning("step.refresh_pr_body.no_sections", pr=ctx.pr_number)
            return StepResult(success=True, summary="PR body refresh skipped: LLM output had no sections")

        if kept_part_of:
            sections.pop(_ISSUE_SECTION_HEADING, None)
            # GitHub honors a closing keyword anywhere in the body, so the refreshed prose must not carry one
            # for the issue the person marked partial.
            sections = {
                heading: _neutralize_inline_closing_keyword(content, ctx.issue_number)
                for heading, content in sections.items()
            }
            log.info("step.refresh_pr_body.part_of_kept", pr=ctx.pr_number, issue=ctx.issue_number)
            if not sections:
                return StepResult(success=True, summary="PR body refresh skipped: LLM output had no sections")

        try:
            if _ISSUE_SECTION_HEADING in sections:
                # Strip any closing keyword the existing body carries outside a
                # "## Issue" heading (pre-migration bodies, or a round whose
                # heading casing drifted and orphaned the old section), so the
                # fresh closing line below is never left sitting beside a stale one.
                current_body = _closing_keyword_re(ctx.issue_number).sub("", current_body)
            new_body = apply_sections(current_body, sections)
            await update_pr_body(ctx.pr_number, body=new_body, repo=ctx.repo, github_user=ctx.config.github_user)
        except Exception:  # noqa: BLE001 (advisory step; a push/auth failure must not abort the round)
            log.warning("step.refresh_pr_body.update_failed", pr=ctx.pr_number, exc_info=True)
            return StepResult(success=True, summary="PR body refresh skipped (non-fatal): could not update PR body")

        summary = f"Refreshed PR #{ctx.pr_number} body"
        if kept_part_of:
            summary += f" (kept existing Part of #{ctx.issue_number})"
        return StepResult(success=True, summary=summary)

    @staticmethod
    def _links_issue_only_as_part_of(current_body: str, issue_number: str) -> bool:
        """True when the body marks the run's issue ``Part of`` and nothing closes it.

        Reads the body with ``parse_issue_links()``, which scans the ``## Issue`` section for a
        "Part of" link and falls back to the whole body when there is none, so a heading-less
        "Part of #N" counts too.
        """
        links = parse_issue_links(current_body)
        return str(issue_number) in links.part_of and str(issue_number) not in links.closes

    @staticmethod
    def _issue_section_mismatch(current_body: str, issue_number: str) -> list[str]:
        """Return the issues the body's ``## Issue`` section names when the run's is not one of them.

        Reads the section with ``parse_issue_links()``, like the ``/pr`` check and
        ``linked-issues``. A section naming no GitHub issue (none, or only a Jira
        link) or naming the run's issue among several is not a mismatch.
        """
        links = parse_issue_links(extract_section(current_body, _ISSUE_SECTION_HEADING))
        named = [*links.closes, *links.part_of]
        if not named or str(issue_number) in named:
            return []
        return named

    @staticmethod
    async def _collect_diff(ctx: ExecutionContext) -> tuple[str, str, str] | None:
        """Collect (commit_log, diff_stat, full_diff) for the refresh prompt.

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
        return commit_log, diff_stat, diff_content_result.stdout.strip()

    @staticmethod
    def _build_sections(ctx: ExecutionContext, llm_text: str) -> dict[str, str]:
        """Build the refreshed section map, with the closing ``## Issue`` section composed by code."""
        return split_sections(compose_run_body(ctx, strip_preamble(llm_text).strip()))

    @staticmethod
    async def _resolve_task(ctx: ExecutionContext) -> tuple[str, str]:
        """Return (task_title, issue_body), re-fetched from the adapter.

        Address-review rounds can span enough time that the issue was edited
        since ``create_pr`` ran; a cached ``ctx.task`` would miss that. Falls
        back to the cached task, then the branch name, on a fetch failure. The
        closing ``## Issue`` section never depends on the result, so an
        unresolved task only thins the prompt.
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
            return task.title, task.body or ""
        return title_from_branch(ctx.branch_name), ""

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: always passes. Advisory step, like ConfidenceScoreStep: a
        failure to refresh the body must never block the address-review
        pipeline, so execute() never returns success=False."""
        return GateCheckResult(passed=True)
