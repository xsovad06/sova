"""Markdown utilities -- section extraction and structural helpers."""

from __future__ import annotations

import re


def _strip_fenced_blocks(text: str) -> str:
    """Replace fenced code block contents with spaces, preserving byte offsets.

    Each character inside a fenced block is replaced with a space (newlines kept)
    so that character positions in the masked text map 1:1 to the original.
    """

    def _blank(m: re.Match[str]) -> str:
        return "".join(" " if c != "\n" else "\n" for c in m.group())

    return re.sub(r"^```[^\n]*\n.*?^```", _blank, text, flags=re.MULTILINE | re.DOTALL)


def extract_section(text: str, heading: str) -> str:
    """Extract the content of a markdown section (between ## heading and next ## or EOF).

    Ignores ``## `` lines inside fenced code blocks so they are not treated as
    section boundaries.
    """
    # Build a "mask" with code fences blanked out for boundary detection
    masked = _strip_fenced_blocks(text)

    pattern = rf"^## {re.escape(heading)}\s*$"
    match = re.search(pattern, masked, re.MULTILINE)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"^## ", masked[start:], re.MULTILINE)
    # Use positions found in the masked text to slice the original text
    section = text[start : start + next_heading.start()] if next_heading else text[start:]
    return section.strip()
