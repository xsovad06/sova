"""File reading utilities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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


def read_json_or_none(path: Path) -> Any | None:
    """Read and parse a file's JSON content, returning None if missing, unreadable, or invalid.

    Builds on ``read_text_or_none()``, additionally guarding against
    ``json.JSONDecodeError`` so a truncated or malformed JSON file never
    aborts a caller iterating over many.

    A file whose content is the JSON literal ``null`` also returns None, so a
    caller cannot tell that apart from a read failure. Use this only where
    "no usable value" is the whole answer. A caller that must log which
    failure it hit, tell absence from corruption, or refuse to read an
    undecodable file as empty (``dependency_health_service``'s manifest scanners,
    ``rtk.py``, ``permissions.py``) keeps its own ``try``/``except`` on purpose.
    """
    text = read_text_or_none(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
