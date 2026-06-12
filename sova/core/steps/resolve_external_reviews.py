"""Step: Resolve external reviews -- resolve threads and dismiss bot reviews after push."""

from __future__ import annotations

import json

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.resolve_external_reviews")

_REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          path
          line
          comments(first: 1) {
            nodes {
              author { login }
            }
          }
        }
      }
    }
  }
}
"""


async def _fetch_unresolved_thread_ids(
    repo: str,
    pr_number: int,
    *,
    authors: set[str],
    github_user: str = "",
) -> list[str]:
    """Fetch unresolved review thread IDs authored by any of the given logins."""
    from sova.utils.gh import resolve_gh_env

    owner, name = repo.split("/", 1) if "/" in repo else ("", repo)
    env = await resolve_gh_env(github_user)

    result = await run(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_REVIEW_THREADS_QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"pr={pr_number}",
        env=env,
        timeout=30,
    )
    if not result.success:
        log.warning("resolve_reviews.threads_fetch_failed", stderr=result.stderr[:200])
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("resolve_reviews.threads_parse_failed", exc_info=True)
        return []

    threads = (
        data.get("data", {}).get("repository", {}).get("pullRequest", {}).get("reviewThreads", {}).get("nodes", [])
    )

    thread_ids: list[str] = []
    for thread in threads:
        if thread.get("isResolved"):
            continue
        comments = thread.get("comments", {}).get("nodes", [])
        if not comments:
            continue
        author = comments[0].get("author", {}).get("login", "")
        if author in authors:
            thread_ids.append(thread.get("id", ""))

    return [tid for tid in thread_ids if tid]


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
        "--paginate",
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

        resolved_count = 0
        dismissed_count = 0

        # Build set of authors whose threads we should resolve:
        # - CodeRabbit bot accounts
        # - The SOVA github_user (reviewer posts findings under this account)
        authors = {"coderabbitai", "coderabbitai[bot]", "coderabbit[bot]"}
        if ctx.config.github_user:
            authors.add(ctx.config.github_user)

        try:
            from sova.adapters.external_reviews import resolve_coderabbit_threads

            thread_ids = await _fetch_unresolved_thread_ids(
                ctx.repo,
                ctx.pr_number,
                authors=authors,
                github_user=ctx.config.github_user,
            )
            if thread_ids:
                await resolve_coderabbit_threads(
                    thread_ids,
                    github_user=ctx.config.github_user,
                )
                resolved_count = len(thread_ids)
                log.info("step.resolve_external_reviews.threads_resolved", count=resolved_count)
        except Exception:
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
        except Exception:
            log.warning("step.resolve_external_reviews.dismiss_failed", exc_info=True)

        parts = []
        if resolved_count:
            parts.append(f"{resolved_count} threads resolved")
        if dismissed_count:
            parts.append(f"{dismissed_count} bot reviews dismissed")
        summary = ", ".join(parts) if parts else "No external review threads to resolve"

        return StepResult(success=True, summary=summary)

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        if self.name in ctx.completed_steps:
            return True
        return not ctx.pr_number
