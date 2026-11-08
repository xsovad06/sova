"""Step: Resolve external reviews (dismiss bot CHANGES_REQUESTED reviews once CI has passed).

Thread resolution and the address summary belong to ``resolve_review_threads``, which runs right after the push.
A persisted ``[pipelines] address_review`` override that predates that step has no such step, so this one falls back
to the same logic when the cycle's summary is still unposted: without it the cycle would post no ``sova-addressed``
marker and escape ``pipeline.max_address_review_cycles``.
"""

from __future__ import annotations

import json

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.core.steps.resolve_review_threads import finish_address_cycle
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.resolve_external_reviews")


async def _dismiss_bot_reviews(
    pr_number: int,
    *,
    repo: str,
    github_user: str = "",
) -> int:
    """Dismiss CHANGES_REQUESTED reviews from bot accounts.

    Returns count of dismissed reviews. Never raises: logs warnings on failure.
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


class ResolveExternalReviewsStep(BaseStep):
    name = "resolve_external_reviews"
    max_retries = 0

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if not ctx.pr_number:
            return StepResult(success=True, summary="No PR to resolve reviews for")

        log.info("step.resolve_external_reviews", pr=ctx.pr_number)

        dismissed_count = 0
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

        summary = f"{dismissed_count} bot reviews dismissed" if dismissed_count else "No bot reviews to dismiss"
        fallback = await self._finish_unrecorded_cycle(ctx)
        if fallback:
            summary = f"{summary}; {fallback}"
        return StepResult(success=True, summary=summary)

    @staticmethod
    async def _finish_unrecorded_cycle(ctx: ExecutionContext) -> str:
        """Run ``resolve_review_threads``' logic when the pipeline did not (or its summary post failed).

        A no-op in the default pipeline, where ``resolve_review_threads`` already posted the summary and set
        ``address_summary_posted``. Returns the fallback's summary, or "" when nothing ran.
        """
        if ctx.pipeline_variant != "address_review" or not ctx.address_cycle_recorded or ctx.address_summary_posted:
            return ""
        log.warning("step.resolve_external_reviews.address_cycle_fallback", pr=ctx.pr_number)
        try:
            return (await finish_address_cycle(ctx)).summary
        except Exception:  # noqa: BLE001 (best effort; the bot-review dismissal above already succeeded)
            log.warning("step.resolve_external_reviews.address_cycle_fallback_failed", exc_info=True)
            return ""

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        if self.name in ctx.completed_steps:
            return True
        return not ctx.pr_number
