"""GitHub CLI authentication helpers for per-project user isolation."""

from __future__ import annotations

import os

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
