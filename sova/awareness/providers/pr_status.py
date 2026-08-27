"""PRStatusProvider: cross-project PR aggregation via gh CLI.

Iterates all registered SOVA projects, fetches open and recently merged
PRs, and categorizes them by actionability for the configured user.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sova.awareness import register_provider
from sova.awareness.base import AwarenessItem, AwarenessProvider, ItemCategory
from sova.config.loader import load_config
from sova.config.registry import list_projects
from sova.utils.gh import resolve_gh_env
from sova.utils.logging import get_logger
from sova.utils.shell import run

_log = get_logger(component="awareness.pr_status")

_MAX_CONCURRENT = 5
_STALE_DAYS = 7
_MERGED_LOOKBACK_HOURS = 24
_FETCH_TIMEOUT = 30.0
_PR_FIELDS = "number,title,url,author,updatedAt,reviewDecision,isDraft,statusCheckRollup,labels,reviewRequests"


class PRStatusProvider(AwarenessProvider):
    """Cross-project PR status aggregation."""

    name = "pr_status"
    display_name = "PR Status"

    async def is_configured(self) -> bool:
        return bool(self.config.pr_github_user)

    async def fetch_items(
        self,
        since: datetime | None = None,
    ) -> list[AwarenessItem]:
        user = self.config.pr_github_user
        if not user:
            return []

        registry = list_projects()
        if not registry:
            return []

        targets = _resolve_targets(registry)
        if not targets:
            return []

        sem = asyncio.Semaphore(_MAX_CONCURRENT)
        tasks = [_safe_fetch(slug, repo, gh_user, user, since, sem) for slug, repo, gh_user in targets]
        results = await asyncio.gather(*tasks)

        items: list[AwarenessItem] = []
        for result in results:
            if result:
                items.extend(result)
        return items


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def _resolve_targets(registry: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return (slug, github_repo, github_user) for projects with GitHub repos."""
    targets: list[tuple[str, str, str]] = []
    for slug, path_str in registry.items():
        try:
            path = Path(path_str)
            if not path.is_dir():
                continue
            cfg = load_config(path)
            if not cfg.github_repo:
                continue
            targets.append((slug, cfg.github_repo, cfg.github_user))
        except Exception:
            _log.warning("pr_status.config_load_failed", slug=slug, exc_info=True)
    return targets


# ---------------------------------------------------------------------------
# Per-project fetch
# ---------------------------------------------------------------------------


async def _safe_fetch(
    slug: str,
    repo: str,
    gh_user: str,
    target_user: str,
    since: datetime | None,
    sem: asyncio.Semaphore,
) -> list[AwarenessItem]:
    """Fetch PRs for one project, returning [] on any failure."""
    async with sem:
        try:
            return await asyncio.wait_for(
                _fetch_project_prs(slug, repo, gh_user, target_user, since),
                timeout=_FETCH_TIMEOUT,
            )
        except TimeoutError:
            _log.warning("pr_status.fetch_timeout", slug=slug, repo=repo)
            return []
        except Exception:
            _log.warning("pr_status.fetch_failed", slug=slug, repo=repo, exc_info=True)
            return []


async def _fetch_project_prs(
    slug: str,
    repo: str,
    gh_user: str,
    target_user: str,
    since: datetime | None,
) -> list[AwarenessItem]:
    """Fetch and categorize PRs for a single project."""
    env = await resolve_gh_env(gh_user)

    open_prs, review_requested_numbers, merged_prs = await asyncio.gather(
        _fetch_open_prs(repo, env),
        _fetch_review_requested(repo, target_user, env),
        _fetch_merged_prs(repo, env, limit=10),
    )

    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=_STALE_DAYS)

    items: list[AwarenessItem] = []
    for pr in open_prs:
        item = _classify_pr(slug, repo, pr, target_user, review_requested_numbers, stale_cutoff)
        if item:
            items.append(item)

    merged_since = since or (now - timedelta(hours=_MERGED_LOOKBACK_HOURS))
    for pr in merged_prs:
        item = _build_merged_item(slug, repo, pr, merged_since, target_user)
        if item:
            items.append(item)

    return items


# ---------------------------------------------------------------------------
# gh CLI calls
# ---------------------------------------------------------------------------


async def _fetch_open_prs(repo: str, env: dict[str, str]) -> list[dict]:
    """Fetch all open PRs for a repo."""
    result = await run(
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--json",
        _PR_FIELDS,
        "--limit",
        "100",
        env=env,
    )
    if not result.success:
        _log.warning("pr_status.list_open_failed", repo=repo, stderr=result.stderr[:200])
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        _log.warning("pr_status.list_open_parse_failed", repo=repo)
        return []


async def _fetch_review_requested(
    repo: str,
    user: str,
    env: dict[str, str],
) -> set[int]:
    """Return PR numbers where review is requested from user."""
    result = await run(
        "gh",
        "search",
        "prs",
        "--repo",
        repo,
        "--state",
        "open",
        "--review-requested",
        user,
        "--json",
        "number",
        "--limit",
        "100",
        env=env,
    )
    if not result.success:
        _log.debug("pr_status.review_requested_failed", repo=repo, stderr=result.stderr[:200])
        return set()
    try:
        data = json.loads(result.stdout)
        return {pr["number"] for pr in data}
    except (json.JSONDecodeError, KeyError, TypeError):
        return set()


async def _fetch_merged_prs(
    repo: str,
    env: dict[str, str],
    limit: int = 10,
) -> list[dict]:
    """Fetch recently merged PRs."""
    result = await run(
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "merged",
        "--json",
        "number,title,url,author,mergedAt",
        "--limit",
        str(limit),
        env=env,
    )
    if not result.success:
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _determine_pr_classification(
    is_own: bool,
    review_requested: bool,
    ci_status: str,
    review_decision: str,
    number: int,
    author: str,
) -> tuple[ItemCategory, int, str, str] | None:
    """Return (category, urgency, action_hint, body) for a PR, or None to skip."""
    if review_requested:
        return (ItemCategory.NEEDS_ATTENTION, 2, "Review PR", f"Review requested from you on PR #{number} by {author}")
    if is_own and ci_status == "failed":
        return (ItemCategory.NEEDS_ATTENTION, 1, "Fix CI", f"CI failing on PR #{number}")
    if is_own and review_decision == "CHANGES_REQUESTED":
        return (ItemCategory.NEEDS_ATTENTION, 1, "Address review", f"Changes requested on PR #{number}")
    if is_own and review_decision == "APPROVED" and ci_status == "passed":
        return (ItemCategory.NEEDS_ATTENTION, 1, "Merge PR", f"PR #{number} approved and CI passing")
    if is_own and ci_status == "passed":
        return (ItemCategory.INFORMATIONAL, 0, "", f"PR #{number} CI passing, awaiting review")
    if is_own:
        return (ItemCategory.INFORMATIONAL, 0, "", f"PR #{number} open (CI: {ci_status})")
    return None


def _classify_pr(
    slug: str,
    repo: str,
    pr: dict,
    target_user: str,
    review_requested_numbers: set[int],
    stale_cutoff: datetime,
) -> AwarenessItem | None:
    """Classify an open PR into an AwarenessItem."""
    number = pr.get("number")
    if not number or number <= 0:
        return None

    title = pr.get("title", "")
    url = pr.get("url", "")
    author = _get_author(pr)
    is_draft = pr.get("isDraft", False)
    updated_at = _parse_gh_timestamp(pr.get("updatedAt"))
    review_decision = pr.get("reviewDecision") or ""
    stale = updated_at is not None and updated_at < stale_cutoff

    ci_status = _summarize_ci(pr.get("statusCheckRollup") or [])
    is_own = author.lower() == target_user.lower()

    if is_draft:
        return None

    classification = _determine_pr_classification(
        is_own,
        number in review_requested_numbers,
        ci_status,
        review_decision,
        number,
        author,
    )
    if classification is None:
        return None

    category, urgency, action_hint, body = classification
    return AwarenessItem(
        id=f"pr:{slug}:{number}",
        provider="pr_status",
        category=category,
        title=title,
        body=body,
        source_url=url,
        timestamp=updated_at,
        urgency=urgency,
        action_hint=action_hint,
        metadata={
            "repo": repo,
            "pr_number": number,
            "author": author,
            "ci_status": ci_status,
            "review_decision": review_decision,
            "stale": stale,
            "project": slug,
        },
    )


def _build_merged_item(
    slug: str,
    repo: str,
    pr: dict,
    since: datetime,
    target_user: str = "",
) -> AwarenessItem | None:
    """Build an INFORMATIONAL item for a recently merged PR authored by target_user."""
    number = pr.get("number")
    if not number or number <= 0:
        return None

    author = _get_author(pr)
    if target_user and author.lower() != target_user.lower():
        return None

    merged_at = _parse_gh_timestamp(pr.get("mergedAt"))
    if not merged_at or merged_at < since:
        return None

    return AwarenessItem(
        id=f"pr:{slug}:{number}:merged",
        provider="pr_status",
        category=ItemCategory.INFORMATIONAL,
        title=pr.get("title", ""),
        body=f"PR #{number} merged",
        source_url=pr.get("url", ""),
        timestamp=merged_at,
        urgency=0,
        metadata={
            "repo": repo,
            "pr_number": number,
            "author": _get_author(pr),
            "merged": True,
            "project": slug,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_author(pr: dict) -> str:
    """Extract author login from a PR dict."""
    author = pr.get("author")
    if isinstance(author, dict):
        return author.get("login", "")
    return ""


def _classify_status_context(ctx: dict) -> str:
    """Classify a StatusContext entry into a CI state string."""
    sc_state = (ctx.get("state") or "").upper()
    if sc_state == "SUCCESS":
        return "passed"
    if sc_state in ("FAILURE", "ERROR"):
        return "failed"
    return "pending"


def _classify_check_run(ctx: dict) -> str:
    """Classify a CheckRun entry into a CI state string."""
    conclusion = (ctx.get("conclusion") or "").upper()
    status = (ctx.get("status") or "").upper()
    if conclusion in ("FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "STALE"):
        return "failed"
    if conclusion == "SUCCESS":
        return "passed"
    if conclusion in ("SKIPPED", "NEUTRAL", "CANCELLED"):
        return "skipped"
    if status == "COMPLETED":
        return "passed"
    return "pending"


def _summarize_ci(rollup: list[dict]) -> str:
    """Summarize statusCheckRollup into a single CI status string.

    Handles both CheckRun (status/conclusion) and StatusContext (state) entries.
    Returns 'passed', 'failed', 'pending', or 'none'.
    """
    if not rollup:
        return "none"
    states: set[str] = set()
    for ctx in rollup:
        if ctx.get("__typename") == "StatusContext":
            state = _classify_status_context(ctx)
        else:
            state = _classify_check_run(ctx)
        if state:
            states.add(state)

    if "failed" in states:
        return "failed"
    if "pending" in states:
        return "pending"
    if "passed" in states:
        return "passed"
    if "skipped" in states:
        return "passed"
    return "none"


def _parse_gh_timestamp(value: str | None) -> datetime | None:
    """Parse a GitHub API timestamp (ISO 8601 with Z suffix)."""
    if not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Self-register
# ---------------------------------------------------------------------------

register_provider("pr_status", PRStatusProvider)
