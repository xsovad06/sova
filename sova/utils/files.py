"""File reading utilities."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
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


def write_text_atomic(path: Path, content: str) -> None:
    """Write UTF-8 text so a concurrent reader sees the old file or the new one, never a partial one.

    ``Path.write_text()`` truncates the destination and then writes, so a reader that
    polls in between (the dashboard's handoff poll) sees an empty or half-written file.
    This writes a sibling temp file in the same directory and ``os.replace()``s it over
    the destination, which is atomic on one filesystem. The temp file's name starts with
    a dot so a ``glob("name-*.json")`` over the directory never lists it.

    An existing destination keeps its permission bits; a new one gets what ``write_text()``
    would have given it, ``0o666`` minus the process umask. The temp file is opened with
    ``os.open(..., 0o666)`` so the kernel applies the umask, rather than ``mkstemp()``'s
    fixed 0600 (which would hide a new handoff from another local reader of ``agent-control/``)
    or a read-and-restore of ``os.umask()`` (which briefly changes it for every other thread).
    The temp file is flushed and ``fsync``ed before the rename, so a power loss cannot leave the
    destination renamed to a zero-length file on a filesystem that reorders the two. The directory
    entry itself is not synced: after a crash the destination holds the old content or the new,
    which is the guarantee callers rely on.

    Errors propagate as ``OSError``, with the temp file removed and its descriptor closed. A process
    killed between creating the temp file and the replace leaves it behind;
    ``remove_stale_temp_files()`` sweeps those.
    """
    tmp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            # fdopen() did not take ownership of the descriptor, so close it here or it leaks.
            os.close(fd)
            raise
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copymode(path, tmp_path)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def remove_stale_temp_files(directory: Path, pattern: str, *, max_age_seconds: float = 300.0) -> int:
    """Delete files in *directory* matching *pattern* that were last modified over *max_age_seconds* ago.

    Sweeps the dot-prefixed ``.name.<token>.tmp`` files ``write_text_atomic()`` leaves when a
    process dies mid-write. The age floor keeps a writer that is mid-write right now safe.
    Best effort: a file that cannot be stat'ed or removed is skipped. Returns the number removed.
    """
    cutoff = time.time() - max_age_seconds
    removed = 0
    for tmp in directory.glob(pattern):
        try:
            if tmp.stat().st_mtime < cutoff:
                tmp.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed
