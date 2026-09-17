"""Pull request management via gh CLI."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse

from sova.utils.gh import resolve_gh_env
from sova.utils.logging import get_logger
from sova.utils.shell import ShellResult, run

log = get_logger(component="git.pr")

_FIND_PR_CACHE_TTL = 90.0  # seconds
_find_pr_cache: dict[tuple[str, str, str], tuple[float, PRInfo | None]] = {}
_SEARCH_FAILED = object()


def _track_gh_rate_limit(result: ShellResult, github_user: str = "") -> None:
    """Record rate limit state from a gh CLI call into the global tracker."""
    from sova.supervisor.github_quota import track_rate_limit

    track_rate_limit(result, github_user)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class CheckStatus(StrEnum):
    """GitHub Actions check run status."""

    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


class CheckConclusion(StrEnum):
    """GitHub Actions check run conclusion."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    NEUTRAL = "NEUTRAL"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    SKIPPED = "SKIPPED"


@dataclass
class PRInfo:
    """Minimal PR info returned after creation."""

    number: int
    url: str
    branch: str = ""
    author_login: str = ""

    @classmethod
    def from_gh_json(cls, pr: dict) -> "PRInfo":
        """Build PRInfo from a gh CLI JSON dict."""
        number = pr.get("number")
        if number is None:
            raise ValueError(f"PR JSON missing required 'number' field: {pr!r}")
        author = pr.get("author")
        author_login = author.get("login", "") if isinstance(author, dict) else ""
        return cls(
            number=number,
            url=pr.get("url", ""),
            branch=pr.get("headRefName", "") or "",
            author_login=author_login,
        )


@dataclass
class PRStatus:
    """Current status of a pull request."""

    number: int
    state: str
    mergeable: str
    review_decision: str
    url: str
    title: str

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"

    @property
    def is_mergeable(self) -> bool:
        return self.mergeable == "MERGEABLE"

    @property
    def is_approved(self) -> bool:
        return self.review_decision == "APPROVED"


@dataclass
class CICheck:
    """A single CI check result."""

    name: str
    status: CheckStatus
    conclusion: CheckConclusion | None
    details_url: str

    @property
    def is_completed(self) -> bool:
        return self.status == CheckStatus.COMPLETED

    @property
    def is_passed(self) -> bool:
        return self.is_completed and self.conclusion == CheckConclusion.SUCCESS

    @property
    def is_failed(self) -> bool:
        return self.is_completed and self.conclusion in (
            CheckConclusion.FAILURE,
            CheckConclusion.TIMED_OUT,
        )


# ---------------------------------------------------------------------------
# PR operations
# ---------------------------------------------------------------------------


async def create_pr(
    *,
    title: str,
    body: str,
    base: str,
    head: str,
    repo: str,
    github_user: str = "",
) -> PRInfo:
    """Create a pull request via gh CLI and return its info."""
    log.info("git.create_pr", title=title[:80], base=base, head=head)

    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body",
        body,
        "--base",
        base,
        "--head",
        head,
        env=env,
    )
    _track_gh_rate_limit(result, github_user)

    if not result.success:
        raise RuntimeError(f"Failed to create PR: {result.stderr[:200]}")

    pr_url = result.stdout.strip()
    try:
        pr_number = int(pr_url.rstrip("/").split("/")[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"Failed to parse PR number from: {pr_url}") from exc

    return PRInfo(number=pr_number, url=pr_url)


async def assign_pr(pr_number: int, *, assignee: str, repo: str, github_user: str = "") -> None:
    """Assign a pull request to a user via gh CLI."""
    log.info("git.assign_pr", pr=pr_number, assignee=assignee)
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "edit",
        str(pr_number),
        "--repo",
        repo,
        "--add-assignee",
        assignee,
        env=env,
    )
    _track_gh_rate_limit(result, github_user)
    if not result.success:
        log.warning("git.assign_pr.failed", pr=pr_number, stderr=result.stderr[:200])


class PRLookupError(RuntimeError):
    """Raised by :func:`find_pr_for_issue_checked` when the lookup could not be confirmed.

    Distinct from "no PR found": every underlying search (body and branch, or
    a quota-forced skip) failed, so the negative result is not trustworthy.
    """


async def _find_pr_for_issue_impl(
    issue_num: str, *, repo: str, env: dict[str, str], github_user: str
) -> tuple[PRInfo | None, bool]:
    """Search for an open PR linked to an issue. Returns (result, failed).

    ``failed=True`` means the search could not be confirmed one way or the
    other (shell/API failure, or a quota-forced skip) and the accompanying
    ``None`` must not be read as "confirmed no PR".
    """
    body_result = await _search_prs_by_body(issue_num, repo=repo, env=env, github_user=github_user)
    if isinstance(body_result, PRInfo):
        return body_result, False

    from sova.supervisor.github_quota import get_github_quota_tracker

    if get_github_quota_tracker(github_user).should_skip():
        return None, True

    branch_result = await _search_prs_by_branch(issue_num, repo=repo, env=env, github_user=github_user)
    if isinstance(branch_result, PRInfo):
        return branch_result, False

    # Confirmed empty only when at least one search actually succeeded
    body_ok = body_result is not _SEARCH_FAILED
    branch_ok = branch_result is not _SEARCH_FAILED
    if body_ok or branch_ok:
        return None, False
    return None, True


async def _find_pr_for_issue_cached(
    issue_id: str, *, repo: str, github_user: str, raise_on_failure: bool
) -> PRInfo | None:
    """Shared cache/lookup path for find_pr_for_issue() and find_pr_for_issue_checked().

    Only a confirmed result (found or confirmed-empty) is cached; a failed
    lookup is never cached, so the next call re-checks rather than treating
    the failure as a stable 90s negative result.
    """
    issue_num = issue_id.lstrip("#").strip()
    cache_key = (repo, github_user, issue_num)
    now = time.monotonic()
    cached = _find_pr_cache.get(cache_key)
    if cached and (now - cached[0]) < _FIND_PR_CACHE_TTL:
        return cached[1]

    log.info("git.find_pr_for_issue", issue=issue_id, repo=repo)
    env = await resolve_gh_env(github_user)
    result, failed = await _find_pr_for_issue_impl(issue_num, repo=repo, env=env, github_user=github_user)
    if failed:
        if raise_on_failure:
            raise PRLookupError(f"Could not confirm PR lookup for issue {issue_id!r} in repo {repo!r}")
        return None

    _find_pr_cache[cache_key] = (now, result)
    return result


async def find_pr_for_issue(issue_id: str, *, repo: str, github_user: str = "") -> PRInfo | None:
    """Find an open PR linked to an issue via gh CLI search.

    Searches for PRs whose body contains 'Closes #N' (or variants) and
    verifies the match to avoid false positives from free-text search.
    Falls back to branch name search for JIRA issues where the body
    contains 'RHCLOUD-N' instead of '#N'.

    Returns ``None`` both when no PR is found and when the lookup itself
    failed; use :func:`find_pr_for_issue_checked` when the caller must
    distinguish the two.
    """
    return await _find_pr_for_issue_cached(issue_id, repo=repo, github_user=github_user, raise_on_failure=False)


async def find_pr_for_issue_checked(issue_id: str, *, repo: str, github_user: str = "") -> PRInfo | None:
    """Like :func:`find_pr_for_issue`, but raises :class:`PRLookupError` instead of
    silently returning ``None`` when the lookup could not be confirmed. Shares
    the same 90-second TTL cache, so no new uncached GitHub API call is added.
    """
    return await _find_pr_for_issue_cached(issue_id, repo=repo, github_user=github_user, raise_on_failure=True)


async def _search_prs_by_body(
    issue_num: str, *, repo: str, env: dict[str, str], github_user: str = ""
) -> PRInfo | None | object:
    """Return PRInfo on match, None on successful empty search, _SEARCH_FAILED on API error."""
    result = await run(
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--search",
        f"#{issue_num} in:body",
        "--json",
        "number,url,body,headRefName,author",
        "--limit",
        "5",
        env=env,
    )
    _track_gh_rate_limit(result, github_user)
    if not result.success:
        log.warning("git.find_pr_for_issue.body_search_failed", stderr=result.stderr[:200])
        return _SEARCH_FAILED

    return _match_pr_results(result.stdout, issue_num)


async def _search_prs_by_branch(
    issue_num: str, *, repo: str, env: dict[str, str], github_user: str = ""
) -> PRInfo | None | object:
    """Return PRInfo on match, None on successful empty search, _SEARCH_FAILED if all lookups fail."""
    any_succeeded = False

    async def _lookup(prefix: str) -> PRInfo | None | object:
        nonlocal any_succeeded
        result = await run(
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--head",
            f"{prefix}{issue_num}",
            "--json",
            "number,url,body,headRefName,author",
            "--limit",
            "1",
            env=env,
        )
        _track_gh_rate_limit(result, github_user)
        if not result.success:
            return _SEARCH_FAILED
        try:
            prs = json.loads(result.stdout)
        except json.JSONDecodeError:
            return _SEARCH_FAILED
        any_succeeded = True
        if prs:
            return PRInfo.from_gh_json(prs[0])
        return None

    results = await asyncio.gather(*(_lookup(prefix) for prefix in ("feat/issue-", "fix/issue-", "issue-")))
    found = next((r for r in results if isinstance(r, PRInfo)), None)
    if found:
        return found
    if not any_succeeded:
        return _SEARCH_FAILED
    return None


def _match_pr_results(stdout: str, issue_num: str) -> PRInfo | None:
    try:
        prs = json.loads(stdout)
    except json.JSONDecodeError:
        return None

    link_pattern = re.compile(rf"(?:closes|fixes|resolves)\s+#?{re.escape(issue_num)}\b", re.IGNORECASE)
    branch_pattern = f"issue-{issue_num}"

    for pr in prs:
        body = pr.get("body", "") or ""
        head = pr.get("headRefName", "") or ""
        if link_pattern.search(body) or branch_pattern in head:
            return PRInfo.from_gh_json(pr)

    return None


async def list_open_prs(*, repo: str, github_user: str = "", author: str | None = None) -> list[dict]:
    """List open PRs (up to 100) with metadata via a single gh CLI call."""
    env = await resolve_gh_env(github_user)
    cmd: list[str] = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--json",
        "number,title,headRefName,headRefOid,url,reviewDecision,isDraft,author,"
        "labels,createdAt,updatedAt,body,state,statusCheckRollup,mergeable,"
        "latestReviews,closingIssuesReferences,"
        "additions,deletions,changedFiles,assignees",
        "--limit",
        "100",
    ]
    if author:
        cmd.extend(["--author", author])
    result = await run(*cmd, env=env)
    _track_gh_rate_limit(result, github_user)
    if not result.success:
        log.warning("git.list_open_prs.failed", stderr=result.stderr[:200])
        return []

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("git.list_open_prs.parse_failed", stdout=result.stdout[:200], exc_info=True)
        return []


@dataclass
class PRReviewData:
    """Per-PR review signals batched from a single GraphQL call.

    ``bot_cr_commit_sha`` is the commit SHA of the most recently submitted bot
    CHANGES_REQUESTED review with a known commit oid (empty if none). Kept for
    backward compatibility / display purposes; it is NOT sufficient on its own
    to decide supersession when multiple bots have outstanding CR reviews.

    ``bot_cr_superseded`` is the authoritative signal: True only when every
    distinct bot with a currently-outstanding CHANGES_REQUESTED review has a
    known (non-empty) commit oid that differs from ``head_sha``. If any bot's
    latest CR review has an unknown/empty oid, this is False regardless of the
    other bots (fail closed on the unknown one), since GitHub does not
    auto-dismiss bot reviews on push and an unresolvable bot's status relative
    to head cannot be determined.
    """

    thread_total: int
    thread_resolved: int
    head_sha: str
    bot_cr_commit_sha: str = ""
    bot_cr_superseded: bool = False


async def _fetch_pr_review_data(
    pr_numbers: list[int],
    *,
    repo: str,
    github_user: str = "",
) -> dict[int, PRReviewData | None] | None:
    """Single batched GraphQL call for review threads, head SHA, and bot CR reviews.

    Returns None on total API/parse failure (every PR unknown). A per-PR value
    of None means that PR's alias was missing or null in the response (e.g. a
    PR force-deleted mid-query), distinct from a genuinely-empty result.
    """
    if not pr_numbers:
        return {}

    owner, name = repo.split("/", 1)
    aliases = []
    for pr_num in pr_numbers:
        aliases.append(
            f"pr{pr_num}: pullRequest(number:{pr_num}) {{"
            f" headRefOid"
            f" reviewThreads(first:100) {{ totalCount pageInfo {{ hasNextPage }} nodes {{ isResolved }} }}"
            f" reviews: latestOpinionatedReviews(last:10) {{"
            f" pageInfo {{ hasPreviousPage }}"
            f" nodes {{ state commit {{ oid }} author {{ login }} submittedAt }} }} }}"
        )

    query = f'{{ repository(owner:"{owner}", name:"{name}") {{ {" ".join(aliases)} }} }}'
    env = await resolve_gh_env(github_user)
    result = await run("gh", "api", "graphql", "-f", f"query={query}", env=env)
    _track_gh_rate_limit(result, github_user)

    if not result.success:
        log.warning("git.review_data.failed", stderr=result.stderr[:200])
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("git.review_data.parse_failed", exc_info=True)
        return None

    from sova.adapters.external_reviews import DEFAULT_CODERABBIT_AUTHORS

    repo_data = (data.get("data") or {}).get("repository") or {}
    out: dict[int, PRReviewData | None] = {}
    for pr_num in pr_numbers:
        pr_data = repo_data.get(f"pr{pr_num}")
        if pr_data is None:
            out[pr_num] = None
            continue
        threads = pr_data.get("reviewThreads") or {}
        if (threads.get("pageInfo") or {}).get("hasNextPage", False):
            # totalCount covers the full connection but nodes are capped at 100,
            # so a resolved-count diffed against totalCount would silently
            # miscount past that point. Fail closed: treat the whole PR as
            # undetermined rather than report a number that may be wrong.
            log.warning("git.review_data.threads_truncated", pr_number=pr_num)
            out[pr_num] = None
            continue
        total = threads.get("totalCount", 0)
        resolved = sum(1 for n in threads.get("nodes", []) if n.get("isResolved"))
        head_sha = pr_data.get("headRefOid") or ""

        reviews_conn = pr_data.get("reviews") or {}
        reviews_truncated = (reviews_conn.get("pageInfo") or {}).get("hasPreviousPage", False)
        if reviews_truncated:
            log.warning("git.review_data.reviews_truncated", pr_number=pr_num)

        # latestOpinionatedReviews already yields at most one node per author
        # (their latest APPROVED/CHANGES_REQUESTED review), so this grouping
        # is defense-in-depth rather than a dedup necessity.
        bot_latest_by_login: dict[str, tuple[str, str]] = {}
        for rev in reviews_conn.get("nodes", []) or []:
            # latestOpinionatedReviews returns each author's latest APPROVED or
            # CHANGES_REQUESTED review; a bot whose latest opinion is now an
            # approval must not be treated as still requesting changes.
            if rev.get("state") != "CHANGES_REQUESTED":
                continue
            login = ((rev.get("author") or {}).get("login") or "").lower()
            # [bot] suffix, or a known non-suffixed bot account (e.g. CodeRabbit's classic login).
            if not (login.endswith("[bot]") or login in DEFAULT_CODERABBIT_AUTHORS):
                continue
            sha = (rev.get("commit") or {}).get("oid") or ""
            ts = rev.get("submittedAt") or ""
            prev = bot_latest_by_login.get(login)
            if prev is None or ts > prev[0]:
                bot_latest_by_login[login] = (ts, sha)

        bot_cr_shas = [sha for _, sha in bot_latest_by_login.values()]
        bot_cr_sha = ""
        latest_ts = ""
        for ts, sha in bot_latest_by_login.values():
            if sha and (not bot_cr_sha or ts > latest_ts):
                bot_cr_sha = sha
                latest_ts = ts

        # Superseded only if EVERY bot with an outstanding CR review has a
        # known oid and it differs from head; any unknown oid fails closed.
        # A truncated reviews window (hasPreviousPage) means an older bot CR
        # review may have been pushed out of the last-10 slice while it is
        # still outstanding, so supersession cannot be confirmed either.
        all_known = bool(bot_cr_shas) and all(bool(sha) for sha in bot_cr_shas)
        bot_cr_superseded = not reviews_truncated and all_known and all(sha != head_sha for sha in bot_cr_shas)

        out[pr_num] = PRReviewData(
            thread_total=total,
            thread_resolved=resolved,
            head_sha=head_sha,
            bot_cr_commit_sha=bot_cr_sha,
            bot_cr_superseded=bot_cr_superseded,
        )
    return out


async def get_pr_review_data(
    pr_numbers: list[int],
    *,
    repo: str,
    github_user: str = "",
) -> dict[int, PRReviewData | None]:
    """Batch-fetch review threads, head SHA, and bot CR review SHA for multiple PRs.

    Returns {pr_number: PRReviewData}, with None for any PR whose data could
    not be determined (missing from the response, or the whole call failed).
    """
    if not pr_numbers:
        return {}
    data = await _fetch_pr_review_data(pr_numbers, repo=repo, github_user=github_user)
    if data is None:
        return dict.fromkeys(pr_numbers)
    return data


async def get_review_thread_counts(
    pr_numbers: list[int],
    *,
    repo: str,
    github_user: str = "",
) -> dict[int, tuple[int, int] | None]:
    """Batch-fetch review thread counts (total, resolved) for multiple PRs.

    Returns {pr_number: (total_threads, resolved_threads)}, or None for any PR
    whose count could not be determined (missing from the response, or the
    whole call failed): never a silent (0, 0). Uses a single GraphQL call.
    """
    if not pr_numbers:
        return {}
    data = await get_pr_review_data(pr_numbers, repo=repo, github_user=github_user)
    return {n: (v.thread_total, v.thread_resolved) if v is not None else None for n, v in data.items()}


async def get_pr_branch(pr_number: int, *, repo: str, github_user: str = "") -> str:
    """Get the head branch name of a PR. Returns empty string on failure."""
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "headRefName",
        env=env,
    )
    _track_gh_rate_limit(result, github_user)
    if not result.success:
        return ""
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""
    return data.get("headRefName", "") or ""


async def get_pr_head_sha(pr_number: int, *, repo: str, github_user: str = "") -> str:
    """Get the current head commit SHA of a PR. Returns empty string on failure."""
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "headRefOid",
        env=env,
    )
    _track_gh_rate_limit(result, github_user)
    if not result.success:
        return ""
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""
    return data.get("headRefOid", "") or ""


async def get_pr_branch_and_head_sha(pr_number: int, *, repo: str, github_user: str = "") -> tuple[str, str]:
    """Get the head branch name and head commit SHA of a PR in a single API call.

    Returns (branch, head_sha), each an empty string on failure or a missing field.
    """
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "headRefName,headRefOid",
        env=env,
    )
    _track_gh_rate_limit(result, github_user)
    if not result.success:
        return "", ""
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "", ""
    return data.get("headRefName", "") or "", data.get("headRefOid", "") or ""


async def get_pr_status(pr_number: int, *, repo: str, github_user: str = "") -> PRStatus:
    """Get the current status of a pull request."""
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "number,state,mergeable,reviewDecision,url,title",
        env=env,
    )
    _track_gh_rate_limit(result, github_user)

    if not result.success:
        raise RuntimeError(f"Failed to get PR #{pr_number}: {result.stderr[:200]}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse PR #{pr_number} response: {result.stdout[:200]}") from exc

    return PRStatus(
        number=data["number"],
        state=data.get("state", ""),
        mergeable=data.get("mergeable", ""),
        review_decision=data.get("reviewDecision", "") or "",
        url=data.get("url", ""),
        title=data.get("title", ""),
    )


async def get_pr_body(pr_number: int, *, repo: str, github_user: str = "") -> str:
    """Fetch a pull request's current body via gh CLI."""
    env = await resolve_gh_env(github_user)
    result = await run("gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "body", env=env)
    _track_gh_rate_limit(result, github_user)

    if not result.success:
        raise RuntimeError(f"Failed to get body for PR #{pr_number}: {result.stderr[:200]}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse PR #{pr_number} response: {result.stdout[:200]}") from exc

    return data.get("body", "") or ""


async def update_pr_body(pr_number: int, *, body: str, repo: str, github_user: str = "") -> None:
    """Overwrite a pull request's body via gh CLI."""
    log.info("git.update_pr_body", pr=pr_number)
    env = await resolve_gh_env(github_user)
    result = await run("gh", "pr", "edit", str(pr_number), "--repo", repo, "--body", body, env=env)
    _track_gh_rate_limit(result, github_user)

    if not result.success:
        raise RuntimeError(f"Failed to update PR #{pr_number} body: {result.stderr[:200]}")


_GH_STATE_MAP: dict[str, tuple[CheckStatus, CheckConclusion | None]] = {
    "SUCCESS": (CheckStatus.COMPLETED, CheckConclusion.SUCCESS),
    "FAILURE": (CheckStatus.COMPLETED, CheckConclusion.FAILURE),
    "TIMED_OUT": (CheckStatus.COMPLETED, CheckConclusion.TIMED_OUT),
    "ERROR": (CheckStatus.COMPLETED, CheckConclusion.FAILURE),
    "STARTUP_FAILURE": (CheckStatus.COMPLETED, CheckConclusion.FAILURE),
    "PENDING": (CheckStatus.IN_PROGRESS, None),
    "QUEUED": (CheckStatus.QUEUED, None),
    "IN_PROGRESS": (CheckStatus.IN_PROGRESS, None),
    "SKIPPING": (CheckStatus.COMPLETED, CheckConclusion.SKIPPED),
    "SKIPPED": (CheckStatus.COMPLETED, CheckConclusion.SKIPPED),
    "CANCELLED": (CheckStatus.COMPLETED, CheckConclusion.CANCELLED),
    "NEUTRAL": (CheckStatus.COMPLETED, CheckConclusion.NEUTRAL),
    "EXPECTED": (CheckStatus.COMPLETED, CheckConclusion.NEUTRAL),
    "STALE": (CheckStatus.COMPLETED, CheckConclusion.NEUTRAL),
}


async def get_pr_diff(pr_number: int, *, repo: str, github_user: str = "") -> str:
    """Fetch the full diff of a pull request via gh CLI."""
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "diff",
        str(pr_number),
        "--repo",
        repo,
        env=env,
    )
    _track_gh_rate_limit(result, github_user)
    if not result.success:
        raise RuntimeError(f"Failed to get diff for PR #{pr_number}: {result.stderr[:200]}")
    return result.stdout


async def get_pr_files(pr_number: int, *, repo: str, github_user: str = "") -> list[str]:
    """Fetch the list of changed file paths in a pull request."""
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "diff",
        str(pr_number),
        "--repo",
        repo,
        "--name-only",
        env=env,
    )
    _track_gh_rate_limit(result, github_user)
    if not result.success:
        raise RuntimeError(f"Failed to get files for PR #{pr_number}: {result.stderr[:200]}")
    return [f for f in result.stdout.strip().splitlines() if f.strip()]


async def get_ci_checks(pr_number: int, *, repo: str, github_user: str = "") -> list[CICheck] | None:
    """Get CI check results for a pull request.

    Returns ``None`` on fetch failures (network, auth, CLI errors) so
    callers can distinguish "no checks configured" (``[]``) from
    "unable to query GitHub" (``None``).
    """
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "checks",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "name,state,link",
        env=env,
    )
    _track_gh_rate_limit(result, github_user)

    if not result.success:
        log.warning("git.ci_checks.failed", pr=pr_number, stderr=result.stderr[:200])
        return None

    try:
        checks_data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("git.ci_checks.bad_json", stdout=result.stdout[:200])
        return None

    checks: list[CICheck] = []
    for item in checks_data:
        state_raw = item.get("state", "PENDING")
        if state_raw not in _GH_STATE_MAP:
            log.warning("git.ci_checks.unknown_state", check=item.get("name"), state=state_raw)
        status, conclusion = _GH_STATE_MAP.get(state_raw, (CheckStatus.IN_PROGRESS, None))

        checks.append(
            CICheck(
                name=item["name"],
                status=status,
                conclusion=conclusion,
                details_url=item.get("link", ""),
            )
        )

    return checks


def _parse_run_id(details_url: str) -> str | None:
    """Extract the GitHub Actions run ID from a check's details URL."""
    # Format: https://github.com/owner/repo/actions/runs/<run_id>/job/<job_id>
    parts = urlparse(details_url).path.strip("/").split("/")
    try:
        idx = parts.index("runs")
        run_id = parts[idx + 1]
        return run_id if run_id.isdigit() else None
    except (ValueError, IndexError):
        return None


async def get_ci_failure_logs(
    failed_checks: list[CICheck],
    *,
    repo: str,
    github_user: str = "",
    max_log_chars: int = 8000,
) -> str:
    """Fetch CI failure logs for the given failed checks.

    Extracts the run ID from each check's details_url and fetches the
    failed job logs via ``gh run view --log-failed``.
    """
    env = await resolve_gh_env(github_user)
    seen_runs: set[str] = set()
    sections: list[str] = []
    remaining = max_log_chars

    for check in failed_checks:
        run_id = _parse_run_id(check.details_url)
        if not run_id or run_id in seen_runs:
            continue
        seen_runs.add(run_id)

        output = await _fetch_run_log(run_id, repo, env, github_user=github_user)
        if not output:
            continue

        remaining, done = _append_log_section(sections, check.name, run_id, output, remaining)
        if done:
            break

    return "\n\n".join(sections)


async def _fetch_run_log(run_id: str, repo: str, env: dict[str, str] | None, *, github_user: str = "") -> str:
    """Fetch the failed-job log for a single run."""
    result = await run("gh", "run", "view", run_id, "--repo", repo, "--log-failed", env=env)
    _track_gh_rate_limit(result, github_user)
    if not result.success:
        log.warning("git.ci_logs.fetch_failed", run_id=run_id, stderr=result.stderr[:200])
        return ""
    return result.stdout.strip()


def _append_log_section(sections: list[str], name: str, run_id: str, output: str, remaining: int) -> tuple[int, bool]:
    """Append a log section respecting the char budget. Returns (remaining, budget_exhausted)."""
    if remaining <= 0:
        return remaining, True
    join_cost = 2 if sections else 0
    header = f"=== {name} (run {run_id}) ===\n"
    budget = remaining - len(header) - join_cost
    if budget <= 0:
        return remaining, True
    if len(output) > budget:
        output = output[-budget:]
    section = header + output
    sections.append(section)
    return remaining - len(section) - join_cost, False
