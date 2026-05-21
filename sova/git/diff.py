"""Unified diff parsing utilities."""

from __future__ import annotations

import re

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_diff_lines(diff: str) -> dict[str, set[int]]:
    """Parse a unified diff and return valid line positions for inline PR comments.

    Returns a mapping of file path -> set of new-side line numbers that appear
    in the diff. These are the only lines where GitHub accepts inline review
    comments (context lines and additions on the RIGHT side).
    """
    result: dict[str, set[int]] = {}
    current_file: str | None = None
    line_num = 0

    for raw_line in diff.splitlines():
        if raw_line.startswith("diff --git "):
            current_file = None
            continue

        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:]
            result.setdefault(current_file, set())
            continue

        if raw_line.startswith("+++ /dev/null"):
            current_file = None
            continue

        m = _HUNK_RE.match(raw_line)
        if m:
            line_num = int(m.group(1))
            continue

        if current_file is None:
            continue

        if raw_line.startswith("+"):
            result[current_file].add(line_num)
            line_num += 1
        elif raw_line.startswith("-"):
            pass
        elif not raw_line.startswith("\\"):
            if raw_line.startswith("index ") or raw_line.startswith("--- "):
                continue
            result[current_file].add(line_num)
            line_num += 1

    return result
