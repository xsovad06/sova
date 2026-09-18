"""Step: Resolve external reviews (resolve threads, dismiss bot reviews, post the address summary after push)."""

from __future__ import annotations

import json

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.utils.gh import get_active_gh_user
from sova.utils.logging import get_logger
from sova.utils.review_markers import SOVA_ADDRESSED_MARKER_RE, format_address_summary
from sova.utils.shell import run

log = get_logger(component="step.resolve_external_reviews")


async def _dismiss_bot_reviews(
    pr_number: int,
    *,
    repo: str,
    github_user: str = "",
) -> int:
    """Dismiss CHANGES_REQUESTED reviews from bot accounts.

    Returns count of dismissed reviews. Never raises -- logs warnings on failure.
    """
    from sova.utils.gh import resolve_gh_env

    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "api",
        f"repos/{repo}/pulls/{pr_number}/reviews",
        env=env,
        timeout=15,
    )
    if not result.success:
        log.warning("resolve_reviews.fetch_failed", stderr=result.stderr[:200])
        return 0

    try:
        reviews = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("resolve_reviews.parse_failed", exc_info=True)
        return 0

    if not isinstance(reviews, list):
        return 0

    dismissed = 0
    for review in reviews:
        if review.get("state") != "CHANGES_REQUESTED":
            continue
        user = review.get("user", {})
        if user.get("type") != "Bot":
            continue

        review_id = review.get("id")
        dismiss_result = await run(
            "gh",
            "api",
            "-X",
            "PUT",
            f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}/dismissals",
            "-f",
            "message=Findings addressed in latest push.",
            env=env,
            timeout=15,
        )
        if dismiss_result.success:
            dismissed += 1
            log.info("resolve_reviews.dismissed", review_id=review_id, user=user.get("login"))
        else:
            log.warning(
                "resolve_reviews.dismiss_failed",
                review_id=review_id,
                stderr=dismiss_result.stderr[:200],
            )

    return dismissed


async def _post_address_summary(ctx: ExecutionContext) -> bool:
    """Post the ``## Address Review`` summary as a COMMENT-state review on the PR.

    The autonomous counterpart of ``/address-pr`` step 14. Runs after the push
    and CI so the marker is anchored to the commit that actually carries the
    fixes; the dashboard reads the marker from the PR's review list to tell
    that the standing SOVA verdict has been addressed, so without this post a
    pipeline-addressed PR keeps showing "SOVA Changes Requested". Never
    raises: a failed post is logged, and the local DB record of the completed
    address cycle still supersedes the verdict on this machine.
    """
    findings = ctx.addressed_review_findings
    if not findings or ctx.pr_number is None:
        return False

    head_sha: str | None = None
    head = await run("git", "rev-parse", "HEAD", cwd=ctx.working_dir)
    if head.success:
        head_sha = head.stdout.strip() or None

    round_no = 1
    try:
        reviews = await ctx.adapter.get_pr_reviews(ctx.pr_number)
        round_no += sum(1 for r in reviews if SOVA_ADDRESSED_MARKER_RE.search(r.body or ""))
    except Exception:  # noqa: BLE001 (round numbering is cosmetic; adapter raises AdapterError/ValueError too)
        log.debug("step.resolve_external_reviews.round_lookup_failed", pr=ctx.pr_number, exc_info=True)

    body = format_address_summary(findings, head_sha=head_sha, round_no=round_no)
    try:
        await ctx.adapter.post_pr_review(ctx.pr_number, body=body, event="COMMENT", comments=[])
    except Exception:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
        log.warning("step.resolve_external_reviews.summary_post_failed", pr=ctx.pr_number, exc_info=True)
        return False
    log.info("step.resolve_external_reviews.summary_posted", pr=ctx.pr_number, findings=len(findings), round=round_no)
    return True


class ResolveExternalReviewsStep(BaseStep):
    name = "resolve_external_reviews"
    max_retries = 0

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if not ctx.pr_number:
            return StepResult(success=True, summary="No PR to resolve reviews for")

        log.info("step.resolve_external_reviews", pr=ctx.pr_number)

        resolved_count = 0
        dismissed_count = 0

        try:
            from sova.adapters.external_reviews import (
                _DEFAULT_CODERABBIT_AUTHORS,
                _fetch_coderabbit_threads,
                resolve_coderabbit_threads,
            )

            authors = set(_DEFAULT_CODERABBIT_AUTHORS)
            if ctx.config.github_user:
                authors.add(ctx.config.github_user)
            active_user = await get_active_gh_user()
            if active_user:
                authors.add(active_user)

            cr_result = await _fetch_coderabbit_threads(
                ctx.repo,
                ctx.pr_number,
                authors=authors,
                github_user=ctx.config.github_user,
            )
            if cr_result.thread_ids:
                resolved_count = await resolve_coderabbit_threads(
                    cr_result.thread_ids,
                    github_user=ctx.config.github_user,
                )
                log.info("step.resolve_external_reviews.threads_resolved", count=resolved_count)
        except (ImportError, RuntimeError, OSError):
            log.warning("step.resolve_external_reviews.resolve_failed", exc_info=True)

        # Dismiss bot CHANGES_REQUESTED reviews
        try:
            dismissed_count = await _dismiss_bot_reviews(
                ctx.pr_number,
                repo=ctx.repo,
                github_user=ctx.config.github_user,
            )
            if dismissed_count:
                log.info("step.resolve_external_reviews.reviews_dismissed", count=dismissed_count)
        except (RuntimeError, OSError):
            log.warning("step.resolve_external_reviews.dismiss_failed", exc_info=True)

        summary_posted = await _post_address_summary(ctx)

        parts = []
        if resolved_count:
            parts.append(f"{resolved_count} threads resolved")
        if dismissed_count:
            parts.append(f"{dismissed_count} bot reviews dismissed")
        if summary_posted:
            parts.append("address summary posted")
        summary = ", ".join(parts) if parts else "No external review threads to resolve"

        return StepResult(success=True, summary=summary)

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        if self.name in ctx.completed_steps:
            return True
        return not ctx.pr_number
