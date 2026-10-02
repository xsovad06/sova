"""Shared detection of network-outage text in failure messages.

Leaf module: imports nothing from ``sova``, so the shell layer, the adapters,
the dashboard services and the supervisor can all use it without any import
cycle. Mirrors the dependency-free contract of ``sova/llm/errors.py``.

The contract is "does this text name a transport failure", never "did
something fail": callers decide what to do with a positive answer. Three
consumers today: ``ShellResult.is_network_unreachable``
(``sova/utils/shell.py``), the ``network_unreachable`` bucket in
``classify_failure_cause()`` (``sova/dashboard/services/reliability_service.py``),
and network self-heal eligibility, which reads persisted
``TaskRun.error_message``.
"""

from __future__ import annotations

import re

# Phrases only a DNS or transport failure produces. Every entry here was either
# observed verbatim in a real outage (see the table in the module tests) or is a
# standard resolver/socket string with no non-network meaning.
_UNAMBIGUOUS_PATTERNS: tuple[str, ...] = (
    "error connecting to api.github.com",
    "could not resolve host",
    "could not resolve hostname",
    "temporary failure in name resolution",
    "nodename nor servname provided",
    "name or service not known",
    "ssh: connect to host",
    "network is unreachable",
    "no route to host",
    "can't reach the api server",
    "cannot reach the api server",
)

# Node/libuv and getaddrinfo errno identifiers, which the Claude CLI surfaces
# directly. Matched with word boundaries rather than as plain substrings:
# Python's own FileNotFoundError lowercases to "fil-enotfound-error", so a bare
# substring test reports a missing CLI binary (a local misconfiguration that
# never self-heals on reconnect) as a network outage.
_ERRNO_RE = re.compile(r"\b(?:enotfound|eai_again|econnrefused|econnreset|etimedout|ehostunreach)\b")

# Phrases a network failure produces, but so do unrelated local failures: a lint
# step reporting "operation timed out", a test asserting "connection refused"
# against a local fixture. These count only when the same message independently
# names a remote endpoint. Same corroboration discipline that
# reliability_service.py applies to its _LLM_CONTEXT_MARKERS, for the same
# reason: a category that fires on generic text is worse than no category.
_AMBIGUOUS_PATTERNS: tuple[str, ...] = (
    "operation timed out",
    "connection timed out",
    "connection refused",
    "connection reset by peer",
    "failed to connect to",
    "could not read from remote repository",
    # Covers SOVA's own phrasing for a verification it could not complete
    # ("GitHub was unreachable while checking PR #687"), which must round-trip
    # through this predicate: the messages this codebase writes are re-read
    # here to decide self-heal eligibility and address-cycle accounting.
    "unreachable",
)

# Deliberately excludes a bare "http": it appears in unrelated prose and URLs
# throughout this codebase's error text. Each entry names a remote host or a
# transport endpoint specifically.
_REMOTE_CORROBORATORS: tuple[str, ...] = (
    "github",
    "anthropic",
    "githubstatus.com",
    "port 22",
    "port 443",
    "https://",
    "git@",
)


def looks_like_network_outage(text: str | None) -> bool:
    """Return True when ``text`` names a network/DNS transport failure.

    Missing, empty, or unmatched input returns False: the question is "does
    this name an outage", so an unrecognized failure is never assumed to be
    one. Ambiguous phrases require a remote endpoint in the same message.
    """
    if not text:
        return False

    # The Claude CLI emits Unicode punctuation ("Can't reach the API server —
    # check your internet or DNS"), so a curly apostrophe must match the same
    # pattern a straight one does.
    lower = text.lower().replace("’", "'")

    if any(pattern in lower for pattern in _UNAMBIGUOUS_PATTERNS):
        return True

    if _ERRNO_RE.search(lower):
        return True

    if any(pattern in lower for pattern in _AMBIGUOUS_PATTERNS):
        return any(corroborator in lower for corroborator in _REMOTE_CORROBORATORS)

    return False
