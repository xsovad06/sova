"""GitHub CLI authentication helpers for per-project user isolation."""

from __future__ import annotations

import json
import os
import re

from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="utils.gh")


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
