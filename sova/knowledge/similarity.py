"""Shared similarity and parsing helpers used by extraction and lifecycle modules."""

from __future__ import annotations

import re

_CONFIRMATION_RE = re.compile(r"\[confirmed:\s*(\d+)\]")


def titles_match(existing: str, new: str) -> bool:
    """Check if two titles refer to the same pattern (lexical fallback).

    Returns True if titles are equal (case-insensitive) or if the shorter
    title is a substring of the longer one (minimum 20 chars).
    """
    existing_lower = existing.lower().strip()
    new_lower = new.lower().strip()

    if existing_lower == new_lower:
        return True

    shorter = min(existing_lower, new_lower, key=len)
    longer = max(existing_lower, new_lower, key=len)
    if len(shorter) >= 20 and shorter in longer:
        return True

    return False


def parse_confirmation_counter(content: str) -> int:
    """Extract the [confirmed: N] counter from memory content."""
    match = _CONFIRMATION_RE.search(content)
    return int(match.group(1)) if match else 0


def set_confirmation_counter(content: str, count: int) -> str:
    """Set the [confirmed: N] counter in memory content.

    Replaces existing counter or appends one if not present.
    """
    if _CONFIRMATION_RE.search(content):
        return _CONFIRMATION_RE.sub(f"[confirmed: {count}]", content)
    return f"{content}\n\n[confirmed: {count}]"
