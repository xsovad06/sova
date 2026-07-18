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


def strip_code_fences(text: str) -> str:
    """Strip leading/trailing markdown code fences if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return stripped.strip()


def _is_atx_heading(line: str) -> bool:
    """Return True if *line* is a valid ATX heading (requires space after ``#``)."""
    if not line.startswith("#"):
        return False
    hashes = line.lstrip("#")
    # Bare "#" with nothing after is a valid (empty) heading
    if not hashes:
        return True
    # ATX spec: one or more '#' followed by a space
    return hashes[0] == " "


def strip_preamble(text: str) -> str:
    """Strip LLM reasoning preamble before the first markdown heading.

    LLMs sometimes prepend reasoning text like "Now I have all the context
    needed. Let me produce the enriched issue body." before the actual output.
    This function removes everything before the first markdown heading (lines
    starting with ``#``), respecting fenced code blocks.

    Returns the original text unchanged if no heading is found outside a code
    block.
    """
    lines = text.split("\n")
    in_fence = False
    first_heading_idx = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and _is_atx_heading(line):
            first_heading_idx = i
            break

    if first_heading_idx is None or first_heading_idx == 0:
        return text

    return "\n".join(lines[first_heading_idx:])
