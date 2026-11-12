"""Size-capping for text injected into an LLM prompt.

One helper, ``trim_payload()``, so every capped payload (hook output, CI logs,
check output, spec context) is cut the same way, marked the same way, and logged
with its before and after size. Callers redact before trimming: a cut made first
could split a secret in a way the redaction pattern no longer matches.
"""

from __future__ import annotations

from typing import Literal

from sova.utils.logging import get_logger

log = get_logger(component="llm.payload")

TRUNCATION_MARKER = "...(truncated)"

KeepDirection = Literal["tail", "head"]


def trim_payload(text: str, limit: int, *, label: str, keep: KeepDirection = "tail") -> str:
    """Trim *text* to *limit* preserved characters, marking the cut.

    ``keep="tail"`` (default) keeps the end, where errors and stack traces live,
    and prefixes the marker. ``keep="head"`` keeps the start (a spec's Solution
    section) and suffixes the marker. The marker is added on top of *limit*, not
    counted against it. Text at or under *limit* (and empty text) is returned
    untouched with no log.

    The cut can land mid-line or mid-token; it is deliberately not realigned,
    so the result size stays predictable. Lengths count characters, so a
    multibyte code point is never split.

    Trimming an already-trimmed value never stacks a second marker: the existing
    marker is set aside, the payload is measured without it, and a payload that
    now fits is returned as it came in.

    *label* names the payload in the ``llm.payload.trimmed`` log event.
    """
    marker = f"{TRUNCATION_MARKER}\n" if keep == "tail" else f"\n{TRUNCATION_MARKER}"
    body = text.removeprefix(marker) if keep == "tail" else text.removesuffix(marker)
    limit = max(limit, 0)
    if len(body) <= limit:
        return text

    kept = body[len(body) - limit :] if keep == "tail" else body[:limit]
    log.info("llm.payload.trimmed", label=label, keep=keep, original_chars=len(body), kept_chars=len(kept), limit=limit)
    return f"{marker}{kept}" if keep == "tail" else f"{kept}{marker}"
