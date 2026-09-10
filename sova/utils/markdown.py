"""Markdown utilities -- section extraction and structural helpers."""

from __future__ import annotations

import re

_FENCE_OPEN_RE = re.compile(r"^(`{3,}|~{3,})")


def _strip_fenced_blocks(text: str) -> str:
    """Replace fenced code block contents with spaces, preserving byte offsets.

    Each character inside a fenced block is replaced with a space (newlines kept)
    so that character positions in the masked text map 1:1 to the original.

    Follows the GFM fenced-code-block rules: both backtick and tilde fences are
    recognized, and a closing fence must use the same character as the opener
    and be at least as long (so a fence opened with four backticks is not
    closed by an inner line of exactly three).
    """
    lines = text.split("\n")
    out_lines: list[str] = []
    fence_char: str | None = None
    fence_len = 0

    for line in lines:
        if fence_char is None:
            match = _FENCE_OPEN_RE.match(line.lstrip())
            if match:
                marker = match.group(1)
                fence_char, fence_len = marker[0], len(marker)
                out_lines.append(" " * len(line))
                continue
            out_lines.append(line)
        else:
            stripped = line.strip()
            if stripped and set(stripped) == {fence_char} and len(stripped) >= fence_len:
                fence_char, fence_len = None, 0
            out_lines.append(" " * len(line))

    return "\n".join(out_lines)


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


def upsert_section(text: str, heading: str, content: str) -> str:
    """Insert or replace a ``## heading`` section with *content*.

    If the heading already exists, its body (up to the next ``## `` heading or
    EOF) is replaced in place, making repeated calls idempotent. Otherwise the
    section is appended to the end. Ignores ``## `` lines inside fenced code
    blocks so they are not treated as section boundaries.
    """
    masked = _strip_fenced_blocks(text)
    pattern = rf"^## {re.escape(heading)}\s*$"
    match = re.search(pattern, masked, re.MULTILINE)
    new_section = f"## {heading}\n\n{content.strip()}\n"

    if not match:
        sep = "\n\n" if text.strip() else ""
        return f"{text.rstrip()}{sep}{new_section}".strip() + "\n"

    next_heading = re.search(r"^## ", masked[match.end() :], re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(text)
    return (text[: match.start()] + new_section + text[end:]).strip() + "\n"


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
