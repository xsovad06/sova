"""Markdown utilities: section extraction and structural helpers."""

from __future__ import annotations

import re

_FENCE_OPEN_RE = re.compile(r"^(`{3,}|~{3,})")


def mask_fenced_blocks(text: str) -> str:
    """Replace fenced code block contents with spaces, preserving byte offsets.

    Each character inside a fenced block is replaced with a space (newlines kept)
    so that character positions in the masked text map 1:1 to the original.
    Invariant depended on by every caller: ``len(_strip_fenced_blocks(text)) ==
    len(text)`` always, since masking never adds, removes, or reorders lines or
    characters, only overwrites some of them with spaces. Callers that find an
    offset on the masked copy may use it to slice the original text directly.

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


# Kept for callers that imported the pre-rename private name.
_strip_fenced_blocks = mask_fenced_blocks


def extract_section(text: str, heading: str, *, ignore_case: bool = False) -> str:
    """Extract the content of a markdown section (between ## heading and next ## or EOF).

    Ignores ``## `` lines inside fenced code blocks so they are not treated as
    section boundaries. Whitespace between ``##`` and *heading* is tolerated
    unconditionally (e.g. ``##  Acceptance Criteria``); ``ignore_case=True``
    additionally matches *heading* case-insensitively, for a heading whose
    exact casing in source documents is not guaranteed. The separator uses
    ``[ \\t]+`` rather than ``\\s+``: under ``re.MULTILINE``, ``\\s`` also
    matches a newline, which would let a bare ``##`` line borrow the next
    line's text as its heading.
    """
    # Build a "mask" with code fences blanked out for boundary detection
    masked = mask_fenced_blocks(text)

    pattern = rf"^##[ \t]+{re.escape(heading)}[ \t\r]*$"
    flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
    match = re.search(pattern, masked, flags)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"^##[ \t]", masked[start:], re.MULTILINE)
    # Use positions found in the masked text to slice the original text
    section = text[start : start + next_heading.start()] if next_heading else text[start:]
    return section.strip()


def remove_section(text: str, heading: str) -> str:
    """Remove a ``## heading`` section (through the next ``## `` heading or EOF).

    Returns *text* stripped and otherwise unchanged if the heading is absent,
    matching the normalization of the found-and-removed path below so a caller
    cannot observe different whitespace handling depending on whether a section
    was actually removed. Ignores ``## `` lines inside fenced code blocks so
    they are not treated as section boundaries.
    """
    masked = _strip_fenced_blocks(text)
    pattern = rf"^## {re.escape(heading)}\s*$"
    match = re.search(pattern, masked, re.MULTILINE)
    if not match:
        return text.strip()
    next_heading = re.search(r"^## ", masked[match.end() :], re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(masked)
    return (text[: match.start()] + text[end:]).strip()


def upsert_section(text: str, heading: str, content: str) -> str:
    """Insert or replace a ``## heading`` section with *content*.

    If the heading already exists, its body (up to the next ``## `` heading or
    EOF) is replaced in place, making repeated calls idempotent. Otherwise the
    section is appended to the end. Ignores ``## `` lines inside fenced code
    blocks so they are not treated as section boundaries. Tolerates the same
    whitespace between ``##`` and *heading* as :func:`extract_section`, so the
    two helpers always agree on what counts as the same heading.
    """
    masked = mask_fenced_blocks(text)
    pattern = rf"^##[ \t]+{re.escape(heading)}[ \t\r]*$"
    match = re.search(pattern, masked, re.MULTILINE)
    new_section = f"## {heading}\n\n{content.strip()}\n"

    if not match:
        sep = "\n\n" if text.strip() else ""
        return f"{text.rstrip()}{sep}{new_section}".strip() + "\n"

    next_heading = re.search(r"^##[ \t]", masked[match.end() :], re.MULTILINE)
    if next_heading:
        # Preserve the blank line that separated this section from the next
        # heading, rather than splicing the new content directly against it.
        end = match.end() + next_heading.start()
        return (text[: match.start()] + new_section + "\n" + text[end:].lstrip("\n")).strip() + "\n"
    return (text[: match.start()] + new_section).strip() + "\n"


def _strip_glued_closing_fence(line: str, width: int) -> str:
    """Drop a closing fence run glued to the end of *line*, leaving an inline code span's delimiters alone.

    The run must be at least *width* backticks, the opening fence's length. A line with an even number of such
    runs ends in an inline code span that already closed itself, so its last run is not a fence. When the text
    before the trailing run leaves a span open (an odd number of backtick runs), the span's own closing delimiter
    is part of that trailing run: ``use `x````` under a three-backtick opener is ``use `x`` plus a fence, so
    only the fence is dropped.
    """
    runs = re.findall(rf"`{{{width},}}", line)
    if len(runs) % 2 == 0 or not line.endswith("`" * width):
        return line
    body = line.rstrip("`")
    trailing = len(line) - len(body)
    body_runs = re.findall(r"`+", body)
    if len(body_runs) % 2 == 1 and trailing - len(body_runs[-1]) >= width:
        return body + body_runs[-1]
    return body


# A language tag in front of the content of a one-line fence: glued to a bracket (json{"a": 1}) or, for
# the JSON family, separated from it by blanks. Any other word before a space stays content, so a one-line
# fenced sentence such as "see [docs]" keeps its first word.
_ONE_LINE_INFO_STRING_RE = re.compile(r"[A-Za-z][\w+-]*(?=[\[{])|(?i:jsonc?|json5)[ \t]+(?=[\[{])")


def strip_code_fences(text: str) -> str:
    """Strip leading/trailing markdown code fences if present.

    A last line that is only backticks (three or more) closes the wrapper whatever width the opener had, as
    the strip always did: LLMs mismatch fence lengths, and a stray closing line would otherwise reach harden,
    spec and PR-body output. The closing run has to be at least as long as the opening one only where that
    disambiguates, for a fence glued to the end of a content line (a four-backtick wrapper loses all four).
    """
    stripped = text.strip()
    opening = re.match(r"`{3,}", stripped)
    if opening is None:
        return stripped
    width = len(opening.group())
    if "\n" not in stripped:
        # Opening and closing fence on the same line (e.g. "```foo```"):
        # the line-based split below would discard the content entirely.
        body = _strip_glued_closing_fence(stripped[width:], width).strip()
        return _ONE_LINE_INFO_STRING_RE.sub("", body, count=1) if _ONE_LINE_INFO_STRING_RE.match(body) else body
    lines = stripped.split("\n")[1:]
    if lines and re.fullmatch(r"[ \t]*`{3,}[ \t]*", lines[-1]):
        lines = lines[:-1]
    elif lines:
        # Closing fence glued to the last content line (e.g. '{"a": 1}```').
        lines[-1] = _strip_glued_closing_fence(lines[-1], width)
    return "\n".join(lines).strip()


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
