"""GitHub CLI authentication helpers for per-project user isolation."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="utils.gh")

# Preflight check: must fail open quickly so an unreachable/slow GitHub
# never blocks a run's first step for anywhere near the default 300s.
_PUSH_PERMISSION_TIMEOUT_SECONDS = 15

# Repo push permissions change rarely; cache confirmed (ALLOWED/DENIED)
# results to avoid spending the shared 5000/hr GitHub API quota re-checking
# on every SyncStep run under fleet concurrency, matching the TTL-cache
# pattern used by coderabbit_quota.py, pr_service.py, and git/pr.py's
# _find_pr_cache. UNKNOWN is never cached: it can reflect a transient
# failure, and the fail-open contract already tolerates re-checking it.
_PUSH_PERMISSION_CACHE_TTL = 300.0  # 5 minutes
_push_permission_cache: dict[tuple[str, str], tuple[float, "PushPermissionCheck"]] = {}


class PushPermission(Enum):
    """Result of a push-permission check against a GitHub repo."""

    ALLOWED = "allowed"
    DENIED = "denied"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PushPermissionCheck:
    """Outcome of :func:`check_push_permission`, including which identity was checked.

    ``checked_as`` is the github_user whose token was confirmed to be in use for
    the API call (i.e. ``resolve_gh_env`` succeeded for it). It is ``None`` when
    no user was configured, or when a user was configured but token resolution
    failed and the call ran under whatever account is ambient-active instead --
    in that case the caller must not report the configured user as the one that
    was actually checked.
    """

    permission: PushPermission
    checked_as: str | None = None


async def resolve_gh_env(github_user: str | None) -> dict[str, str] | None:
    """Build an env dict with GH_TOKEN for the configured GitHub user.

    Uses ``gh auth token --user <user>`` to retrieve the token from the
    local gh credential store, then injects it as GH_TOKEN so each
    subprocess authenticates as the correct user without mutating
    global ``gh auth`` state.

    Returns None if no user is configured (inherits parent env).
    """
    if not github_user:
        return None

    result = await run("gh", "auth", "token", "--user", github_user)
    if not result.success:
        log.warning(
            "gh.token_resolve_failed",
            user=github_user,
            stderr=result.stderr[:200],
        )
        return None

    token = result.stdout.strip()
    if not token:
        log.warning("gh.empty_token", user=github_user)
        return None

    return {**os.environ, "GH_TOKEN": token}


async def get_active_gh_user() -> str | None:
    """Return the login of the currently active ``gh auth`` account.

    Parses the JSON output of ``gh auth status --json hosts`` to find
    the account with ``active: true``. Returns None on any failure
    (gh not installed, no active account, parse error).
    """
    result = await run("gh", "auth", "status", "--json", "hosts")
    if not result.success:
        log.debug("gh.active_user_failed", stderr=result.stderr[:200])
        return None

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        log.debug("gh.active_user_parse_failed")
        return None

    try:
        for accounts in data.get("hosts", {}).values():
            for account in accounts:
                if account.get("active"):
                    return account.get("login")
    except (AttributeError, TypeError, KeyError):
        log.debug("gh.active_user_traverse_failed")
        return None

    return None


async def _get_origin_url(cwd: Path | str) -> str | None:
    """Return the configured URL of the 'origin' remote, or None if unknown."""
    result = await run("git", "remote", "get-url", "origin", cwd=cwd)
    if not result.success:
        return None
    return result.stdout.strip() or None


def _is_ssh_remote(url: str) -> bool:
    """Return True if *url* is an SSH-based git remote, not HTTPS.

    check_push_permission() verifies push access via a GitHub REST API
    token, which has no bearing on SSH key-based push authentication: the
    actual ``git push`` (sova/git/branch.py:push()) authenticates over
    whichever channel the remote uses, with no GH_TOKEN override, so an SSH
    remote's real push auth is a completely different mechanism than this
    check exercises (this repo's own CLAUDE.md documents `core.sshCommand`
    for exactly this reason). Skip the API check entirely for SSH remotes
    rather than reporting a DENIED/ALLOWED verdict that reflects an auth
    path the real push will not use.
    """
    return url.startswith("git@") or url.startswith("ssh://")


async def check_push_permission(
    repo: str, *, github_user: str | None = None, cwd: Path | str | None = None
) -> PushPermissionCheck:
    """Check whether the active gh account can push to *repo*.

    Returns DENIED only when GitHub definitively reports no push access: a
    permissions object with ``push: false``, or an HTTP 404 the account
    cannot see (indistinguishable over the API from a typo'd repo, but
    either way the run cannot open a PR). Every other outcome (gh not
    installed, network error, malformed output, a permissions object
    missing the ``push`` key) returns UNKNOWN, so a flaky or unreachable
    GitHub check never blocks a run on its own. A short, explicit timeout
    keeps that fail-open guarantee fast even when the GitHub API hangs
    instead of erroring immediately.

    When *cwd* is given and the repo's ``origin`` remote is SSH-based, the
    API call is skipped entirely (see ``_is_ssh_remote``): a REST
    permissions check cannot represent SSH push auth, so checking it would
    produce a verdict unrelated to what the real push actually uses.

    Confirmed results (ALLOWED/DENIED) are cached for a few minutes per
    (repo, checked_as) to avoid spending API quota re-checking permissions
    that change rarely, under fleet concurrency where many runs start close
    together. Keyed on ``checked_as`` rather than the requested
    ``github_user``: when token resolution for a configured user fails, the
    API call actually runs under the ambient-active account, and caching
    the result under the configured (unexercised) identity's key would let
    a later call for that same configured user reuse a verdict that was
    never actually checked for it.

    The returned ``checked_as`` tells the caller which identity's token was
    actually used, so a caller reporting a denial never blames an identity
    whose credentials were never exercised (see ``PushPermissionCheck``).
    """
    if not repo:
        return PushPermissionCheck(PushPermission.UNKNOWN)

    if cwd is not None:
        origin_url = await _get_origin_url(cwd)
        if origin_url and _is_ssh_remote(origin_url):
            log.debug("gh.push_permission_check_skipped", repo=repo, reason="ssh_remote")
            return PushPermissionCheck(PushPermission.UNKNOWN)

    try:
        env = await resolve_gh_env(github_user)
    except OSError:
        # gh not installed: resolve_gh_env's own `gh auth token` call raises
        # before this function's own try/except (guarding the API call
        # below) is even reached. Fail open like every other gh-unavailable
        # path here, rather than letting the run crash on a missing binary.
        log.debug("gh.push_permission_check_error", repo=repo, exc_info=True)
        return PushPermissionCheck(PushPermission.UNKNOWN)
    checked_as = github_user if github_user and env is not None else None

    cache_key = (repo, checked_as or "")
    now = time.monotonic()
    cached = _push_permission_cache.get(cache_key)
    if cached and (now - cached[0]) < _PUSH_PERMISSION_CACHE_TTL:
        return cached[1]

    try:
        result = await run(
            "gh",
            "api",
            f"repos/{repo}",
            "--jq",
            ".permissions.push",
            env=env,
            timeout=_PUSH_PERMISSION_TIMEOUT_SECONDS,
        )
    except OSError:
        log.debug("gh.push_permission_check_error", repo=repo, exc_info=True)
        return PushPermissionCheck(PushPermission.UNKNOWN, checked_as)

    from sova.supervisor.github_quota import track_rate_limit

    # checked_as, not github_user: when token resolution for a configured
    # github_user failed, the API call above actually ran under whatever
    # account is ambient-active, and crediting the quota hit to the
    # configured (unexercised) identity would misattribute it, the same
    # bug this PR fixed for the user-facing error message below.
    track_rate_limit(result, checked_as or "")

    if not result.success:
        stderr = result.stderr.lower()
        if "http 404" in stderr:
            log.warning("gh.push_permission_denied", repo=repo, reason="repo_not_found")
            check = PushPermissionCheck(PushPermission.DENIED, checked_as)
            _push_permission_cache[cache_key] = (now, check)
            return check
        log.debug("gh.push_permission_check_failed", repo=repo, stderr=result.stderr[:200])
        return PushPermissionCheck(PushPermission.UNKNOWN, checked_as)

    output = result.stdout.strip()
    if output == "true":
        check = PushPermissionCheck(PushPermission.ALLOWED, checked_as)
        _push_permission_cache[cache_key] = (now, check)
        return check
    if output == "false":
        log.warning("gh.push_permission_denied", repo=repo, reason="permissions_push_false")
        check = PushPermissionCheck(PushPermission.DENIED, checked_as)
        _push_permission_cache[cache_key] = (now, check)
        return check

    log.debug("gh.push_permission_unexpected_output", repo=repo, output=output[:100])
    return PushPermissionCheck(PushPermission.UNKNOWN, checked_as)


_CLOSES_RE = re.compile(r"(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE)


async def resolve_linked_issue(
    pr_number: int,
    *,
    repo: str,
    github_user: str | None = None,
) -> str | None:
    """Extract the linked issue number from a PR body (e.g. 'Closes #26').

    Returns the issue number as a string, or None if no link is found.
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
        "body",
        env=env,
    )
    if not result.success:
        log.warning("gh.pr_view_failed", pr=pr_number, stderr=result.stderr[:200])
        return None

    try:
        body = json.loads(result.stdout).get("body", "")
    except (json.JSONDecodeError, TypeError):
        log.warning("gh.pr_body_parse_failed", pr=pr_number)
        return None

    match = _CLOSES_RE.search(body)
    if match:
        issue_num = match.group(1)
        log.info("gh.resolved_linked_issue", pr=pr_number, issue=issue_num)
        return issue_num

    log.warning("gh.no_linked_issue", pr=pr_number)
    return None
