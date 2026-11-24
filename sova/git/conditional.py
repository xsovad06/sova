"""Conditional GitHub REST reads: has this list changed since the last look?

``ConditionalProbe`` keeps the ETag of each endpoint it has read and asks again with ``If-None-Match``. GitHub
answers an unchanged list with 304 Not Modified, which does not count against the rate limit, so a short probe
interval costs nothing while nothing changes.

The answer has four states, never a boolean: a failed probe must not look like "unchanged" (it would hide a
change) or like "changed" (it would refetch every list on an outage). A failed probe keeps the stored ETag, so
the next one asks the same question again.

A 200's new ETag stays pending until the caller ``commit()``s it, once it has acted on the change (invalidated the
caches). A caller cancelled or failing in between never commits, so the next probe answers CHANGED again instead
of a 304 that would hide the change for good.

``gh api`` exits non-zero on a 304 (``gh: HTTP 304``), so the outcome is read from the HTTP status line that
``--include`` prints, never from the exit code alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from sova.utils.gh import resolve_gh_env
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="git.conditional")

# A probe is a single small read: one that takes longer than this is a failure, retried at the next interval.
_PROBE_TIMEOUT_SECONDS = 20.0

_STATUS_LINE_RE = re.compile(r"^HTTP/[\d.]+\s+(\d{3})", re.MULTILINE)
# The value is stripped after the match: a lazy group between two whitespace runs backtracks super-linearly.
_ETAG_RE = re.compile(r"^etag:(.*)$", re.IGNORECASE | re.MULTILINE)
_RATE_LIMIT_RE = re.compile(r"rate limit|x-ratelimit-remaining:\s*0\b", re.IGNORECASE)


class ProbeOutcome(StrEnum):
    """What one conditional read learned."""

    UNCHANGED = "unchanged"  # 304: the list is what it was at the stored ETag
    CHANGED = "changed"  # 200: new content (or the first read, with no ETag yet)
    RATE_LIMITED = "rate_limited"  # 403 or 429 for the rate limit: back off instead of asking again soon
    FAILED = "failed"  # anything else: unknown, so neither unchanged nor changed


def _split_response(stdout: str) -> tuple[str, str]:
    """The last response head ``gh api --include`` printed, and the body after it.

    A head runs to the first blank line. An interim or redirect response prints a head of its own before the
    final one, so every leading head is skipped while the text after it starts with another status line.
    """
    rest = stdout.replace("\r\n", "\n")
    head, _sep, body = rest.partition("\n\n")
    while body.startswith("HTTP/"):
        head, _sep, body = body.partition("\n\n")
    return head, body


@dataclass(frozen=True)
class _Pending:
    """A 200 the caller has not acted on yet: its ETag, and its body for a caller that reads what changed."""

    etag: str
    body: str


class ConditionalProbe:
    """ETags per endpoint for one project, and the conditional read that uses them."""

    def __init__(self) -> None:
        self._etags: dict[str, str] = {}
        self._pending: dict[str, _Pending] = {}

    def body(self, endpoint: str) -> str | None:
        """The body of *endpoint*'s latest 200 until it is committed; None after any other answer."""
        pending = self._pending.get(endpoint)
        return pending.body if pending is not None else None

    def commit(self, endpoint: str) -> None:
        """Store the ETag of *endpoint*'s latest 200, now that the change it reported has been acted on."""
        pending = self._pending.pop(endpoint, None)
        if pending is not None and pending.etag:
            self._etags[endpoint] = pending.etag

    async def check(self, endpoint: str, *, github_user: str = "") -> ProbeOutcome:
        """Ask whether *endpoint* (a ``gh api`` path such as ``repos/o/r/pulls?...``) changed since the last read.

        A CHANGED answer's ETag is pending: every check until ``commit()`` still sends the previous one.
        """
        self._pending.pop(endpoint, None)
        cmd = ["gh", "api", "--include", "--method", "GET"]
        etag = self._etags.get(endpoint)
        if etag:
            cmd.extend(["--header", f"If-None-Match: {etag}"])
        cmd.append(endpoint)
        try:
            env = await resolve_gh_env(github_user)
            result = await run(*cmd, env=env, timeout=_PROBE_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 (a probe that could not run is a failed probe, retried next interval)
            log.warning("git.conditional.probe_error", endpoint=endpoint, exc_info=True)
            return ProbeOutcome.FAILED
        head, body = _split_response(result.stdout)
        statuses = _STATUS_LINE_RE.findall(head)
        status = int(statuses[-1]) if statuses else None
        if status == 304:
            return ProbeOutcome.UNCHANGED
        if status == 200:
            match = _ETAG_RE.search(head)
            self._pending[endpoint] = _Pending(etag=match.group(1).strip() if match else "", body=body)
            return ProbeOutcome.CHANGED
        if status in (403, 429) and (status == 429 or _RATE_LIMIT_RE.search(f"{head}\n{result.stderr}")):
            log.warning("git.conditional.rate_limited", endpoint=endpoint, status=status)
            return ProbeOutcome.RATE_LIMITED
        log.warning(
            "git.conditional.probe_failed",
            endpoint=endpoint,
            status=status,
            returncode=result.returncode,
            timed_out=result.timed_out,
            stderr=result.stderr[:200],
        )
        return ProbeOutcome.FAILED
