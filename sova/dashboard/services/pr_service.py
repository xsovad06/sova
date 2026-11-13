"""PR tracker service: lists open PRs with computed lifecycle state."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from sova.dashboard.services.work_state import state_color, state_label
from sova.dashboard.services.work_verdict import parse_review_history
from sova.git.pr import OPEN_PR_LIST_LIMIT, PROpenState, get_pr_open_state
from sova.utils.gh import BRANCH_ISSUE_RE as _BRANCH_ISSUE_RE
from sova.utils.gh import TITLE_ISSUE_RE as _TITLE_ISSUE_RE
from sova.utils.gh import choose_pr_issue, get_active_gh_user, parse_issue_links
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig
    from sova.git.pr import PRReviewData
    from sova.ipc.handoff import DashboardHandoff

log = get_logger(component="dashboard.pr_service")

_JIRA_MARKDOWN_RE = re.compile(r"\[([A-Z]+-\d+)\]\(https?://")
_JIRA_PLAIN_RE = re.compile(r"JIRA:\s*https?://\S+/browse/[A-Z]+-(\d+)")
_TITLE_JIRA_KEY_RE = re.compile(r"\[[A-Z]+-(\d+)\]")
_BRANCH_JIRA_KEY_RE = re.compile(r"(?:^|/)[A-Z]+-(\d+)(?=$|[-_/])")

_PR_CACHE_TTL = 120  # seconds (shared across supervisor, PR monitor, dashboard)
_pr_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
# Per-key generation counter, bumped on every invalidation and never reset.
# A fetch in flight when invalidation fires can still land afterward; it
# compares its captured generation before caching so it can't resurrect the
# pre-invalidation result and undo the invalidation for the rest of the TTL.
_pr_cache_generation: dict[tuple[str, str], int] = {}
# A failed fetch is not cached as a result, but it is remembered briefly so an outage costs one
# `gh pr list` per window instead of one per poll per open tab. Value: monotonic time the pause ends.
_PR_FETCH_FAILURE_PAUSE = 30  # seconds
_pr_fetch_failed_until: dict[tuple[str, str], float] = {}

# Multi-reference resolutions already logged, keyed by (PR number, head SHA, issues). The resolver runs on
# every cache refresh, so without this a PR that stays open would repeat the same line every TTL.
# Cache invalidation does not clear it (that would re-log other repos' PRs too): a new push changes the head SHA.
_logged_closing_ties: set[tuple[int | None, str, tuple[int, ...], int, str]] = set()
_LOGGED_CLOSING_TIES_MAX = 1000

_last_known_states: dict[int, str] = {}
_bg_tasks: set[asyncio.Task[None]] = set()

_COMPUTED_TO_EVENT: dict[str, str] = {
    "approved": "approved",
    "approved_ci_green": "approved",
    "ci_failed": "ci_failed",
    "changes_requested": "reviewed",
    "sova_changes": "reviewed",
    "review_addressed": "reviewed",
}


def invalidate_pr_cache(repo: str) -> None:
    """Drop every cached open-PR list for a repo so the next call refetches it.

    Called when an agent finishes, since developer, reviewer, and command runs
    open, push to, review, or merge PRs. Matches on repo alone: the github_user
    half of the key only selects which token fetched the list.
    """
    keys = {k for k in _pr_cache if k[0] == repo} | {k for k in _pr_cache_generation if k[0] == repo}
    for key in keys:
        _pr_cache.pop(key, None)
        _pr_fetch_failed_until.pop(key, None)
        _pr_cache_generation[key] = _pr_cache_generation.get(key, 0) + 1


class ComputedPRState(StrEnum):
    DRAFT = "draft"
    CONFLICTED = "conflicted"
    CI_RUNNING = "ci_running"
    CI_FAILED = "ci_failed"
    CHANGES_REQUESTED = "changes_requested"
    # SOVA's own review said revise or block for the current head; no notification, never an approved state
    SOVA_CHANGES = "sova_changes"
    APPROVED_CI_GREEN = "approved_ci_green"
    APPROVED = "approved"
    REVIEW_ADDRESSED = "review_addressed"
    AWAITING_REVIEW = "awaiting_review"


def parse_linked_issue(body: str | None, branch: str = "") -> int | None:
    """Extract the issue number a PR body links to.

    GitHub syntax (Closes/Fixes/Resolves/Part of #N) is resolved by
    :func:`sova.utils.gh.choose_pr_issue` with the PR's *branch*, the same rules the run's
    recorded issue uses, so code spans and fenced blocks never name an issue and the
    ``## Issue`` section is read before the rest of the body. JIRA syntax
    ([PROJ-42](https://...) or JIRA: https://.../browse/PROJ-42) ranks below a GitHub closing
    keyword and above a body whose only GitHub link is "Part of #N", which can name an epic.
    """
    if not body:
        return None
    primary = choose_pr_issue(body, branch).primary
    if primary is not None and parse_issue_links(body).closes:
        return int(primary)
    m = _JIRA_MARKDOWN_RE.search(body)
    if m:
        return int(m.group(1).split("-")[-1])
    m = _JIRA_PLAIN_RE.search(body)
    if m:
        return int(m.group(1))
    return int(primary) if primary is not None else None


def _title_or_branch_linked_issue(raw: dict) -> int | None:
    """Fall back to the PR's own title or branch name for a linked issue number.

    Used only once closingIssuesReferences and body-keyword parsing have both
    come up empty, by both :func:`_extract_linked_issue` and
    :func:`_extract_all_linked_issues` so they can't disagree on a PR with no
    closing reference and no body keyword (e.g. a ``Part of #N`` PR whose
    body phrasing a future change misses).
    """
    title = raw.get("title") or ""
    m = _TITLE_ISSUE_RE.search(title)
    if m:
        return int(m.group(1))
    m = _TITLE_JIRA_KEY_RE.search(title)
    if m:
        return int(m.group(1))
    branch = raw.get("headRefName") or ""
    m = _BRANCH_ISSUE_RE.search(branch)
    if m:
        return int(m.group(1))
    m = _BRANCH_JIRA_KEY_RE.search(branch)
    if m:
        return int(m.group(1))
    return None


def _closing_references(raw: dict) -> list[int]:
    """Issue numbers GitHub lists in closingIssuesReferences, entries without a number skipped."""
    return [r["number"] for r in raw.get("closingIssuesReferences") or [] if r.get("number") is not None]


def _primary_closing_reference(refs: list[int], raw: dict) -> list[int]:
    """Pick the PR's own issue out of the closingIssuesReferences entries.

    Returns the chosen issue first, then the other candidates :func:`sova.utils.gh.choose_pr_issue`
    keeps (none when the choice overrode every listed reference).

    GitHub lists every issue a body closes in the order it chooses, so ``refs[0]`` can be an
    issue the body only mentions in passing (a quoted ``Closes #12`` in a review note closed #12
    once), and even a single entry can be that stray one. :func:`sova.utils.gh.choose_pr_issue`
    picks: the ``## Issue`` section and the branch's ``issue-N`` outrank GitHub's order, and a
    branch issue GitHub does not list can win over the listed ones. With neither signal the first
    entry stays primary. A multi-reference resolution is recorded once per PR, head SHA and
    reference set as ``multiple_closing_references`` (warning when the choice overrides ``refs[0]``).
    A single reference is recorded as ``closing_reference_overridden`` (warning) when overridden, or
    as ``closing_reference_branch_mismatch`` (info) when the section kept it against a different
    branch issue (a stacked PR); any other single reference logs nothing.
    """
    choice = choose_pr_issue(raw.get("body") or "", raw.get("headRefName") or "", refs)
    chosen = int(choice.candidates[0])
    branch_match = _BRANCH_ISSUE_RE.search(raw.get("headRefName") or "")
    branch_issue = int(branch_match.group(1)) if branch_match else None
    overridden = choice.signal != "none" and chosen != refs[0]
    branch_mismatch = branch_issue is not None and branch_issue != chosen
    disagreement = overridden or branch_mismatch

    if len(refs) > 1 or disagreement:
        key = (raw.get("number"), raw.get("headRefOid") or "", tuple(refs), chosen, choice.signal)
        if key not in _logged_closing_ties:
            if len(_logged_closing_ties) >= _LOGGED_CLOSING_TIES_MAX:
                _logged_closing_ties.clear()
            _logged_closing_ties.add(key)
            emit = log.warning if overridden else log.info
            if len(refs) > 1:
                event = "multiple_closing_references"
            else:
                event = "closing_reference_overridden" if overridden else "closing_reference_branch_mismatch"
            emit(
                f"pr_service.{event}",
                pr=raw.get("number"),
                issues=refs,
                first_listed=refs[0],
                chosen=chosen,
                signal=choice.signal,
                section_issue=chosen if choice.signal == "section" else None,
                branch_issue=branch_issue,
                disagreement=disagreement,
            )
    return [int(n) for n in choice.candidates]


def _extract_linked_issue(raw: dict) -> int | None:
    """Extract linked issue from closingIssuesReferences, PR body, title, or branch.

    Priority: closingIssuesReferences (accurate, excludes PR-to-PR refs; the ``## Issue``
    section and the branch name decide which entry is primary, and a branch issue can
    outrank a lone foreign entry, see :func:`_primary_closing_reference`)
    > body keywords (Closes/Fixes/Resolves/Part of #N, JIRA links)
    > title (#N or [PROJ-N]) > branch (issue-N or PROJ-N).
    """
    issues = _extract_all_linked_issues(raw)
    return issues[0] if issues else None


def _extract_all_linked_issues(raw: dict) -> list[int]:
    """Return all issue numbers linked to a PR from closingIssuesReferences, body,
    or (with the same fallback chain as :func:`_extract_linked_issue`) title/branch.

    The primary issue, the one :func:`_extract_linked_issue` returns, comes first; the other
    closingIssuesReferences entries follow as secondary entries. The exception is a primary that
    is not among those entries (the branch or the ``## Issue`` section overrode every stray closing
    phrase GitHub listed): it is returned alone, because work items and open-PR issue collection
    read this list and would otherwise give each stray issue a duplicate row.
    """
    refs = _closing_references(raw)
    if refs:
        return _primary_closing_reference(refs, raw)
    single = parse_linked_issue(raw.get("body"), raw.get("headRefName") or "")
    if single is not None:
        return [single]
    fallback = _title_or_branch_linked_issue(raw)
    return [fallback] if fallback is not None else []


extract_all_linked_issues = _extract_all_linked_issues  # for callers outside this module (the Work board)


def _summarize_ci(rollup: list[dict] | None) -> str:
    """Summarize statusCheckRollup contexts into a single CI status string.

    Handles both CheckRun (status/conclusion) and StatusContext (state) entries.
    """
    if not rollup:
        return "none"
    states = set()
    for ctx in rollup:
        is_status_context = ctx.get("__typename") == "StatusContext"
        if is_status_context:
            sc_state = (ctx.get("state") or "").upper()
            if sc_state == "SUCCESS":
                states.add("passed")
            elif sc_state in ("FAILURE", "ERROR"):
                states.add("failed")
            elif sc_state == "PENDING":
                states.add("pending")
            continue

        status = (ctx.get("status") or "").upper()
        conclusion = (ctx.get("conclusion") or "").upper()
        if conclusion in (
            "FAILURE",
            "ERROR",
            "TIMED_OUT",
            "STARTUP_FAILURE",
            "ACTION_REQUIRED",
            "STALE",
        ):
            states.add("failed")
        elif conclusion == "SUCCESS":
            states.add("passed")
        elif conclusion in ("SKIPPED", "NEUTRAL", "CANCELLED"):
            states.add("skipped")
        elif status == "COMPLETED":
            states.add("passed")
        else:
            states.add("pending")
    if "failed" in states:
        return "failed"
    if "pending" in states:
        return "pending"
    if "passed" in states:
        return "passed"
    if "skipped" in states:
        return "passed"
    return "none"


def _is_bot_login(login: str) -> bool:
    """Check if a login belongs to a bot account: [bot] suffix or a known CodeRabbit alias."""
    from sova.adapters.external_reviews import DEFAULT_CODERABBIT_AUTHORS

    login = login.lower()
    return login.endswith("[bot]") or login in DEFAULT_CODERABBIT_AUTHORS


def _is_bot_review(review: dict) -> bool:
    """Check if a review is from a bot account."""
    author = review.get("author") or {}
    return _is_bot_login(author.get("login") or "")


def _extract_cr_reviews(latest_reviews: list[dict]) -> list[dict]:
    """Extract CHANGES_REQUESTED reviews from latest_reviews."""
    return [r for r in latest_reviews if r.get("state") == "CHANGES_REQUESTED"]


def _should_unblock_bot_reviews(
    cr_reviews: list[dict],
    all_threads_resolved: bool | None,
    ci_status: str,
    mergeable: str,
    superseded_by_new_commit: bool = False,
) -> str | None:
    """Check if bot CHANGES_REQUESTED reviews with resolved threads should unblock the PR.

    Returns APPROVED_CI_GREEN if all conditions are met and CI is green + mergeable,
    APPROVED if conditions are met but CI is not green or not mergeable,
    None if any condition fails: no CR reviews, threads unresolved or unknown, human
    CR present, or the bot review has not been superseded by a newer commit (new
    commits do not auto-dismiss a bot review on GitHub, so this is required in
    addition to, not instead of, resolved/vacuous-zero threads).
    """
    if not cr_reviews:
        return None
    if not all_threads_resolved:
        return None
    if not all(_is_bot_review(r) for r in cr_reviews):
        return None
    if not superseded_by_new_commit:
        return None
    if ci_status == "passed" and mergeable == "MERGEABLE":
        return ComputedPRState.APPROVED_CI_GREEN
    return ComputedPRState.APPROVED


# Mirrors work_state.py's _MERGE_STATES_BLOCKED_BY_GITHUB/_MERGE_STATES_UNMERGEABLE: a
# GitHub mergeStateStatus of BLOCKED (e.g. branch protection awaiting a required review)
# or DIRTY (a real conflict) must never be masked by an otherwise-green ci_status/mergeable
# reading, since this is the signal sova/supervisor/pr_monitor.py's notify_on_ready_to_merge
# trusts directly (CodeRabbit, PR #1114).
_MERGE_STATES_BLOCKED_BY_GITHUB = frozenset({"BLOCKED"})
_MERGE_STATES_UNMERGEABLE = _MERGE_STATES_BLOCKED_BY_GITHUB | {"DIRTY"}


# Verdicts that stand against a PR head. "none" (no marker for this head) and "unknown" (GitHub could not
# be read) never block on their own: the merge gate is /integrate-pr, which stops and asks on "unknown".
_BLOCKING_SOVA_VERDICTS = frozenset({"revise", "block"})


def _same_commit(a: str, b: str) -> bool:
    """True when two SHAs name one commit (a marker may carry an abbreviated sha)."""
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def _github_head_verdict(review_data: PRReviewData | None, head_sha: str, trusted_login: str) -> str:
    """The latest SOVA verdict GitHub holds for ``head_sha``: approve, revise, block, none or unknown.

    A marker anchored to an older head, or one an address cycle superseded, is "none": the re-review
    of the new head produces the verdict that counts. "unknown" means GitHub could not answer.
    """
    if review_data is None or not trusted_login:
        return "unknown"
    parsed = parse_review_history(review_data.review_history, trusted_login=trusted_login)
    if not parsed["has_sova_review"]:
        # A truncated window may have cut the marker off, so an absent one proves nothing.
        return "unknown" if review_data.review_history_truncated else "none"
    marker_sha = parsed["review_head_sha"]
    if parsed["verdict"] == "addressed" or (marker_sha and head_sha and not _same_commit(marker_sha, head_sha)):
        return "none"
    return parsed["verdict"]


def _local_head_verdict(handoffs: list[DashboardHandoff], pr_number: int, head_sha: str) -> str | None:
    """A revise or block verdict the local reviewer handoff recorded for this PR and head, else None.

    A handoff naming another PR, or a head other than the current one, is ignored, so a stale handoff
    cannot block a later head. When several handoffs name this PR and head, the newest by ``created_at``
    decides (a later approving handoff supersedes an earlier revise), whatever order the caller passes.
    """
    for handoff in sorted(handoffs, key=lambda h: h.created_at, reverse=True):
        details = handoff.details
        if handoff.pr_number != pr_number or not _same_commit(str(details.get("review_head_sha") or ""), head_sha):
            continue
        verdict = details.get("verdict")
        return verdict if verdict in _BLOCKING_SOVA_VERDICTS else None
    return None


def resolve_head_verdict(
    review_data: PRReviewData | None,
    *,
    pr_number: int,
    head_sha: str,
    trusted_login: str,
    handoffs: list[DashboardHandoff],
) -> str:
    """SOVA's verdict for the current PR head: approve, revise, block, none or unknown.

    GitHub wins when it holds a marker for this head. Otherwise (none, or unreachable) a local reviewer
    handoff for the same PR and head can still say revise or block, which covers a verdict whose post
    failed. One answer feeds compute_pr_state() and so the dashboard and the PR monitor alike.
    """
    github = _github_head_verdict(review_data, head_sha, trusted_login)
    if github not in ("none", "unknown"):
        return github
    return _local_head_verdict(handoffs, pr_number, head_sha) or github


def compute_pr_state(
    *,
    is_draft: bool,
    review_decision: str,
    ci_status: str,
    mergeable: str,
    merge_state: str = "",
    latest_reviews: list[dict] | None = None,
    all_threads_resolved: bool | None = False,
    superseded_by_new_commit: bool = False,
    sova_verdict: str | None = None,
) -> str:
    """Derive a single computed state from PR signals.

    ``sova_verdict`` is resolve_head_verdict()'s answer: a revise or block for the current head keeps
    the PR out of every approved state.
    """
    if is_draft:
        return ComputedPRState.DRAFT
    if mergeable == "CONFLICTING" or merge_state == "DIRTY":
        return ComputedPRState.CONFLICTED
    if ci_status == "pending":
        return ComputedPRState.CI_RUNNING
    if ci_status == "failed":
        return ComputedPRState.CI_FAILED
    if sova_verdict in _BLOCKING_SOVA_VERDICTS:
        return ComputedPRState.SOVA_CHANGES
    github_blocked = merge_state in _MERGE_STATES_UNMERGEABLE
    if review_decision == "CHANGES_REQUESTED":
        if latest_reviews:
            cr_reviews = _extract_cr_reviews(latest_reviews)
            unblock_state = _should_unblock_bot_reviews(
                cr_reviews, all_threads_resolved, ci_status, mergeable, superseded_by_new_commit
            )
            if unblock_state:
                return unblock_state
        return ComputedPRState.CHANGES_REQUESTED
    if (
        review_decision == "APPROVED"
        and ci_status in ("passed", "none")
        and mergeable == "MERGEABLE"
        and not github_blocked
    ):
        return ComputedPRState.APPROVED_CI_GREEN
    if review_decision == "APPROVED":
        return ComputedPRState.APPROVED
    if latest_reviews:
        cr_reviews = _extract_cr_reviews(latest_reviews)
        if cr_reviews:
            unblock_state = _should_unblock_bot_reviews(
                cr_reviews, all_threads_resolved, ci_status, mergeable, superseded_by_new_commit
            )
            if unblock_state:
                return unblock_state
            return ComputedPRState.CHANGES_REQUESTED
        if all_threads_resolved and ci_status == "passed" and mergeable == "MERGEABLE" and not github_blocked:
            return ComputedPRState.APPROVED_CI_GREEN
        if all_threads_resolved:
            return ComputedPRState.APPROVED
        return ComputedPRState.REVIEW_ADDRESSED
    if ci_status == "passed" and mergeable == "MERGEABLE" and not github_blocked:
        return ComputedPRState.APPROVED_CI_GREEN
    return ComputedPRState.AWAITING_REVIEW


def _age_seconds(created: str, now: float) -> int:
    """Compute age in seconds from an ISO 8601 timestamp."""
    if not created:
        return 0
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return int(now - dt.timestamp())
    except (ValueError, TypeError):
        return 0


def _extract_review_logins(latest_reviews: list[dict] | None) -> list[str]:
    """Extract unique reviewer logins from latest reviews."""
    logins: set[str] = set()
    for rev in latest_reviews or []:
        login = (rev.get("author") or {}).get("login") or ""
        if login:
            logins.add(login)
    return sorted(logins)


def _extract_latest_approval_at(latest_reviews: list[dict] | None) -> str | None:
    """Return the most recent human-approved review's submittedAt timestamp, or None.

    Excludes bot approvals (CodeRabbit or any other bot): a bot's approval must
    never be mistaken for a human sign-off. Mirrors the login-based bot check
    in sova/supervisor/gates/review_completed.py:_has_human_approval().
    """
    best: str | None = None
    for rev in latest_reviews or []:
        if rev.get("state") != "APPROVED" or _is_bot_review(rev):
            continue
        ts = rev.get("submittedAt") or ""
        if ts and (best is None or ts > best):
            best = ts
    return best


def _extract_pr_labels(raw: dict) -> list[str]:
    """Extract label names from raw PR data."""
    return [lbl.get("name", "") for lbl in (raw.get("labels") or [])]


def _extract_pr_assignees(raw: dict) -> list[str]:
    """Extract non-empty assignee logins from raw PR data."""
    return [a.get("login", "") for a in (raw.get("assignees") or []) if a.get("login")]


def _count_pr_commits(raw: dict) -> int:
    """Count commits on the PR, tolerating a non-list `commits` field."""
    commits_node = raw.get("commits") or []
    return len(commits_node) if isinstance(commits_node, list) else 0


def _thread_resolution(raw: dict) -> tuple[int | None, int | None, bool | None]:
    """Return (total, resolved, all_resolved) review-thread counts, all None when uncounted."""
    thread_counts = raw.get("_thread_counts")
    if thread_counts is None:
        return None, None, None
    thread_total, thread_resolved = thread_counts
    return thread_total, thread_resolved, thread_total == 0 or thread_resolved >= thread_total


def _enrich_pr(raw: dict, now: float) -> dict:
    """Transform a raw gh pr list entry into a PR tracker dict."""
    ci_status = _summarize_ci(raw.get("statusCheckRollup"))
    review_decision = raw.get("reviewDecision") or ""
    is_draft = bool(raw.get("isDraft"))
    mergeable = raw.get("mergeable") or ""
    merge_state = raw.get("mergeStateStatus") or ""
    latest_reviews = raw.get("latestReviews") or None
    thread_total, thread_resolved, all_threads_resolved = _thread_resolution(raw)
    superseded_by_new_commit = bool(raw.get("_superseded_by_new_commit", False))

    computed = compute_pr_state(
        is_draft=is_draft,
        review_decision=review_decision,
        ci_status=ci_status,
        mergeable=mergeable,
        merge_state=merge_state,
        latest_reviews=latest_reviews,
        all_threads_resolved=all_threads_resolved,
        superseded_by_new_commit=superseded_by_new_commit,
        sova_verdict=raw.get("_sova_verdict"),
    )

    author = raw.get("author") or {}
    labels = _extract_pr_labels(raw)
    pr_assignees = _extract_pr_assignees(raw)
    commit_count = _count_pr_commits(raw)
    linked_issues = _extract_all_linked_issues(raw)

    return {
        "number": raw["number"],
        "title": raw.get("title", ""),
        "branch": raw.get("headRefName", ""),
        "head_sha": raw.get("headRefOid", "") or "",
        "url": raw.get("url", ""),
        "state": raw.get("state", "OPEN"),
        "computed_state": computed,
        "state_label": state_label(computed),
        "state_color": state_color(computed),
        "review_decision": review_decision,
        "ci_status": ci_status,
        "mergeable": mergeable,
        "merge_state": merge_state,
        "is_draft": is_draft,
        "author": author.get("login", ""),
        "linked_issue": linked_issues[0] if linked_issues else None,
        "linked_issues": linked_issues,
        "age_seconds": _age_seconds(raw.get("createdAt") or "", now),
        "updated_at": raw.get("updatedAt") or "",
        "labels": labels,
        "thread_total": thread_total,
        "thread_resolved": thread_resolved,
        "review_logins": _extract_review_logins(latest_reviews),
        "latest_reviews": latest_reviews or [],
        "latest_approval_at": _extract_latest_approval_at(latest_reviews),
        "additions": raw.get("additions") or 0,
        "deletions": raw.get("deletions") or 0,
        "changed_files": raw.get("changedFiles") or 0,
        "assignees": pr_assignees,
        "commit_count": commit_count,
    }


def get_unresolved_thread_count(pr_data: dict) -> int | None:
    """Return the number of unresolved review threads from enriched PR data.

    Extracts from the cached PR data (thread_total, thread_resolved) without
    making additional API calls. Returns 0 when no threads exist or the keys
    are simply absent (e.g. a synthetic test dict or an older cache shape),
    and None only when a key is explicitly present with value None (fetch
    succeeded but the count is genuinely unknown), so unknown is never
    conflated with zero and vice versa.
    """
    if "thread_total" not in pr_data or "thread_resolved" not in pr_data:
        return 0
    total = pr_data.get("thread_total")
    resolved = pr_data.get("thread_resolved")
    if total is None or resolved is None:
        return None
    return max(0, total - resolved)


async def list_open_prs_with_state(project_dir: Path | None = None, *, raise_on_error: bool = False) -> list[dict]:
    """List all open PRs with computed state. Cached per-repo for 120s.

    When raise_on_error=True, config/retrieval failures propagate instead of
    returning [] (so callers can distinguish "no PRs" from "retrieval failed").
    """
    from sova.config.context import resolve_project_dir
    from sova.config.loader import load_config
    from sova.dashboard.services.agent_pool import get_default_project_dir
    from sova.git.pr import PRLookupError, get_pr_review_data, list_open_prs
    from sova.ipc.handoff import read_all_handoff_files

    project_dir = resolve_project_dir(project_dir, default_project_dir=get_default_project_dir())

    try:
        cfg = load_config(project_dir)
    except Exception:  # noqa: BLE001 (logged then optionally re-raised; the caller decides)
        log.warning("pr_service.config_load_failed", project_dir=str(project_dir), exc_info=True)
        if raise_on_error:
            raise
        return []

    if not cfg.github_repo:
        return []

    repo = cfg.github_repo
    cache_key = (repo, cfg.github_user)
    now = time.monotonic()
    cached = _pr_cache.get(cache_key)
    if cached and (now - cached[0]) < _PR_CACHE_TTL:
        return cached[1]
    if _pr_fetch_failed_until.get(cache_key, 0.0) > now:
        if raise_on_error:
            raise PRLookupError(f"Open PR listing for {repo!r} failed recently; not retrying yet")
        return []
    # setdefault, not get: the key must exist in the dict *before* the await
    # below so a concurrent invalidate_pr_cache() can see and bump it.
    generation = _pr_cache_generation.setdefault(cache_key, 0)
    # Always raise here so a failed fetch is never cached: a cached [] would read as "no open PRs"
    # to a raise_on_error caller for the rest of the TTL.
    try:
        raw_prs = await list_open_prs(repo=repo, github_user=cfg.github_user, raise_on_error=True)
    except PRLookupError:
        _pr_fetch_failed_until[cache_key] = time.monotonic() + _PR_FETCH_FAILURE_PAUSE
        if raise_on_error:
            raise
        return []
    _pr_fetch_failed_until.pop(cache_key, None)

    pr_numbers = [p["number"] for p in raw_prs]
    try:
        review_data = await get_pr_review_data(pr_numbers, repo=repo, github_user=cfg.github_user)
    except (RuntimeError, OSError):
        log.warning("pr_service.thread_counts_failed", exc_info=True)
        review_data = {}
    trusted_login = cfg.github_user or await get_active_gh_user() or ""
    handoffs = read_all_handoff_files(project_dir)
    for pr in raw_prs:
        rd = review_data.get(pr["number"])
        if rd is None:
            pr["_thread_counts"] = None
            pr["_superseded_by_new_commit"] = False
        else:
            pr["_thread_counts"] = (rd.thread_total, rd.thread_resolved)
            pr["_superseded_by_new_commit"] = rd.bot_cr_superseded
        pr["_sova_verdict"] = resolve_head_verdict(
            rd,
            pr_number=pr["number"],
            head_sha=pr.get("headRefOid") or "",
            trusted_login=trusted_login,
            handoffs=handoffs,
        )

    wall_now = time.time()
    result = [_enrich_pr(pr, wall_now) for pr in raw_prs]
    result.sort(key=lambda p: p["number"], reverse=True)

    # Only cache if no invalidation landed while this fetch was in flight;
    # otherwise the fetch predates the invalidation trigger and caching it
    # would resurrect stale data for the rest of the TTL.
    if _pr_cache_generation.get(cache_key, 0) == generation:
        _pr_cache[cache_key] = (now, result)
    log.info("pr_service.refreshed", repo=repo, count=len(result))

    task = asyncio.ensure_future(_record_state_transitions(result, repo=repo, project_dir=project_dir))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)

    return result


def known_open_pr_numbers(fetched_prs: list[dict] | None, cfg: ProjectConfig | None) -> set[int] | None:
    """The open PR numbers, or ``None`` when they cannot be known.

    That is a failed PR fetch, no configured repo, or a list as long as ``OPEN_PR_LIST_LIMIT``
    (``gh pr list --limit`` may have cut an open PR off).
    """
    if fetched_prs is None or cfg is None or not cfg.github_repo:
        return None
    if len(fetched_prs) >= OPEN_PR_LIST_LIMIT:
        return None
    return {pr["number"] for pr in fetched_prs}


_CONFIRM_CONCURRENCY = 4
_CONFIRM_BACKOFF = 30.0  # seconds without lookups for a repo after one came back UNKNOWN
_confirm_backoff_until: dict[str, float] = {}


async def confirm_open_pr_numbers(
    open_numbers: set[int] | None, candidates: Iterable[int], cfg: ProjectConfig | None
) -> set[int] | None:
    """Add candidate PRs the open list lacks but a direct lookup does not confirm as closed.

    The open list is cached, so a PR a still-running developer run just created can be missing from it.
    A PR numbered below the highest listed one existed when the list was fetched, so its absence is
    conclusive and costs no call; only a higher number can be newer than the list. Lookups run a few at a
    time, and after an ``UNKNOWN`` (an outage or rate limit) the repo is not asked again for a while.
    Only ``NOT_OPEN`` leaves a candidate out; ``OPEN`` and ``UNKNOWN`` add it.
    """
    if open_numbers is None or cfg is None:
        return open_numbers
    newest_listed = max(open_numbers, default=0)
    unsettled = {pr for pr in candidates if pr and pr not in open_numbers and pr > newest_listed}
    if not unsettled:
        return open_numbers
    now = time.monotonic()
    if now < _confirm_backoff_until.get(cfg.github_repo, 0.0):
        return open_numbers | unsettled
    ordered = sorted(unsettled)
    semaphore = asyncio.Semaphore(_CONFIRM_CONCURRENCY)

    async def _lookup(pr: int) -> PROpenState:
        async with semaphore:
            return await get_pr_open_state(pr, repo=cfg.github_repo, github_user=cfg.github_user)

    states = await asyncio.gather(*(_lookup(pr) for pr in ordered))
    if PROpenState.UNKNOWN in states:
        _confirm_backoff_until[cfg.github_repo] = time.monotonic() + _CONFIRM_BACKOFF
    confirmed = {pr for pr, state in zip(ordered, states, strict=True) if state is not PROpenState.NOT_OPEN}
    return open_numbers | confirmed


async def _record_state_transitions(prs: list[dict], *, repo: str, project_dir: Path) -> None:
    """Detect state changes and write PREvent rows (fire-and-forget)."""
    from sova.db.models import PREvent
    from sova.db.session import get_session

    events_to_write: list[dict] = []
    for pr in prs:
        pr_num = pr["number"]
        state = pr["computed_state"]
        prev = _last_known_states.get(pr_num)
        _last_known_states[pr_num] = state

        if prev is None:
            continue
        if state == prev:
            continue

        event_type = _COMPUTED_TO_EVENT.get(state)
        if not event_type:
            continue

        events_to_write.append(
            {
                "pr_number": pr_num,
                "repo": repo,
                "event_type": event_type,
                "timestamp": (
                    datetime.fromisoformat(pr["updated_at"]) if pr.get("updated_at") else datetime.now(timezone.utc)
                ),
                "actor": pr.get("author", ""),
                "metadata_json": {"computed_state": state, "prev_state": prev},
            }
        )

    if not events_to_write:
        return

    try:
        for ev in events_to_write:
            try:
                async with await get_session(project_dir) as session:
                    session.add(PREvent(**ev))
                    await session.commit()
            except (OSError, RuntimeError, SQLAlchemyError):
                log.debug("pr_service.event_write_conflict", pr=ev.get("pr_number"), exc_info=True)
    except (OSError, RuntimeError, SQLAlchemyError):
        log.debug("pr_service.event_record_failed", exc_info=True)
