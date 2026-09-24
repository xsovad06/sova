"""File reading utilities."""

from __future__ import annotations

from pathlib import Path


def read_text_or_none(path: Path) -> str | None:
    """Read a file's UTF-8 text content, returning None if missing or unreadable.

    Guards against both OSError (missing file, permission denied) and
    UnicodeDecodeError (non-UTF-8 content), so a single bad file never aborts
    a caller iterating over many.
    """
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
