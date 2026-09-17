"""Review completed gate: blocks SPAWN_INTEGRATE when no review exists.

Three-source check (defense in depth):
  1. Issue labels: sova:approved only (zero API cost). sova:revise and
     sova:block explicitly reject the PR and must not satisfy this gate.
  2. DB TaskRun: completed (status="done") reviewer run with handoff_json.
     Deliberately verdict-blind (see below).
  3. PR review_decision: non-bot GitHub approval (from cached PR data)

Additionally blocks regardless of the above when the PR has unresolved review
threads (from any reviewer), or when thread resolution state could not be
determined at all (unknown fails closed, same as unresolved): a stale
"reviewed" signal must not authorize integration while open conversations
remain, or might remain undetected.

Known residual gap: source 2 (``_has_reviewer_run()``) only checks that a
reviewer run completed, not what it concluded, so a completed run with a
"revise" or "block" verdict (or one whose findings never reached any external
observer, e.g. "post_failed") still satisfies it even though source 1 rejects
the matching label. Making source 2 verdict-aware was considered and
deliberately deferred: it overlaps issue #991's next-action resolver work and
was out of scope for the label-narrowing fix that introduced source 1's
rejection rule (issue #993). A genuine human GitHub approval (source 3)
posted after a rejecting label or run still authorizes integration, since this
gate only fails to satisfy on a rejection, it does not veto.

Only blocks supervisor autonomy; dashboard "Integrate" button remains available
for human-initiated integration (explicit user choice).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sova.supervisor.gates import BlockReason

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger("sova.supervisor.gates.review_completed")

_APPROVED_LABEL = "sova:approved"


async def check_review_completed_gate(
    issue_number: int,
    *,
    labels: list[str],
    pr_number: int | None,
    project_dir: Path,
    pr_data: dict | None = None,
) -> BlockReason | None:
    """Block SPAWN_INTEGRATE when no review of any kind exists.

    Returns BlockReason if no review found, None if review exists.
    """
    unresolved = _unresolved_thread_count(pr_data)
    if unresolved is None:
        return BlockReason(
            gate="review_completed",
            detail=f"PR #{pr_number} thread resolution state is unknown: cannot verify before integration",
        )
    if unresolved > 0:
        return BlockReason(
            gate="review_completed",
            detail=f"PR #{pr_number} has {unresolved} unresolved review thread(s): "
            "requires thread resolution before integration",
        )

    if _has_sova_label(labels):
        return None

    if await _has_reviewer_run(issue_number, pr_number, project_dir):
        return None

    if _has_human_approval(pr_data):
        return None

    return BlockReason(
        gate="review_completed",
        detail=f"No review found for issue #{issue_number}: "
        "requires SOVA review, human approval, or completed reviewer run",
    )


def _has_sova_label(labels: list[str]) -> bool:
    """Check if the sova:approved label is present on the issue.

    sova:revise and sova:block are deliberately excluded: they mean a review
    explicitly rejected the PR, not that integration was authorized.
    """
    return _APPROVED_LABEL in labels


def _unresolved_thread_count(pr_data: dict | None) -> int | None:
    """Return the unresolved review thread count from enriched PR data.

    ``pr_data is None`` means no PR data was fetched at all (e.g. a transient
    GraphQL failure in the best-effort enrichment step): this is a distinct,
    pre-existing case from "fetched but unusable" and is treated as zero so
    the gate falls through to its other review sources, matching prior
    behavior. Only a ``pr_data`` dict whose thread keys are explicitly
    ``None`` (fetch succeeded but data is unusable) is genuinely unknown.
    """
    if pr_data is None:
        return 0

    from sova.dashboard.services.pr_service import get_unresolved_thread_count

    return get_unresolved_thread_count(pr_data)


async def _has_reviewer_run(
    issue_number: int,
    pr_number: int | None,
    project_dir: Path,
) -> bool:
    """Check if a completed (status="done") reviewer TaskRun exists for this issue/PR.

    A failed or interrupted run may still carry handoff_json (e.g. a crash after
    writing findings), but it never finished the review, so it must not satisfy
    this safety-critical gate. ``get_sova_review_verdict()`` is shared with
    display-only callers that legitimately want to surface a verdict from a
    non-"done" run, so the completion check is done here instead.

    Deliberately verdict-blind: a completed run's verdict (including "revise"
    or "block") is not consulted here. See the module docstring's "Known
    residual gap" note.
    """
    try:
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        verdict = await get_sova_review_verdict(str(issue_number), pr_number=pr_number, project_dir=project_dir)
        return verdict.get("has_sova_review", False) and verdict.get("run_status") == "done"
    except Exception:  # noqa: BLE001 (one of three review sources; failure falls through to the others)
        log.debug("review_completed.db_check_failed", exc_info=True)
        return False


def _has_human_approval(pr_data: dict | None) -> bool:
    """Check if a non-bot human approved the PR on GitHub.

    CodeRabbit's approval alone is not sufficient. ``gh pr list --json
    latestReviews`` (the source of ``pr_data["latest_reviews"]``) does not
    return a ``type``/``__typename`` discriminator on the review author, so
    bot detection must key off the login: GitHub App bots are conventionally
    suffixed ``[bot]``, and CodeRabbit's classic-bot login has no suffix at
    all, hence the explicit ``DEFAULT_CODERABBIT_AUTHORS`` allowlist. The
    ``type``/``__typename`` checks are kept for callers that do supply a
    richer (e.g. REST) shape.
    """
    if pr_data is None:
        return False
    review_decision = pr_data.get("review_decision", "")
    if review_decision != "APPROVED":
        return False

    from sova.adapters.external_reviews import DEFAULT_CODERABBIT_AUTHORS

    latest_reviews = pr_data.get("latest_reviews") or []
    for review in latest_reviews:
        if review.get("state") != "APPROVED":
            continue
        user = review.get("author", {}) or review.get("user", {}) or {}
        if user.get("type") == "Bot" or user.get("__typename") == "Bot":
            continue
        login = (user.get("login") or "").lower()
        if login.endswith("[bot]") or login in DEFAULT_CODERABBIT_AUTHORS:
            continue
        return True
    return False
