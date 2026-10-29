"""Warn once per version of a file, then log at debug on repeats.

A file that is read on a recurring path (the dashboard's poll, the supervisor, every agent exit) and
stays broken would otherwise log a warning on every cycle. A *version* is the file's ``(mtime, size)``:
a new write is a new version and warns again, while a re-read of the same bytes does not.

The table is a plain ``dict`` owned by the caller, so each feature keeps its own and its tests can
inspect it. Keys are resolved paths, so two readers that spell one directory differently (a symlinked
checkout, or ``/tmp`` against ``/private/tmp`` on macOS) share an entry instead of each warning once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

FileVersion = tuple[float, int]


def warn_key(path: Path) -> str:
    """Return the table key for *path*: its resolved location."""
    return str(path.resolve())


def file_version(path: Path) -> FileVersion | None:
    """Return the ``(mtime, size)`` of *path*, or None when it cannot be stat'ed."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def warn_once_per_version(log: Any, event: str, path: Path, seen: dict[str, FileVersion], **fields: Any) -> None:
    """Log *event* for *path* as a warning the first time this version is seen, at debug after that.

    A file that cannot even be stat'ed has no version to dedupe on, so it always warns.
    """
    key = warn_key(path)
    version = file_version(path)
    if version is not None and seen.get(key) == version:
        log.debug(event, path=str(path), **fields)
        return
    log.warning(event, path=str(path), **fields)
    if version is not None:
        seen[key] = version


def forget_unless_unchanged(path: Path, seen: dict[str, FileVersion]) -> None:
    """Drop *path*'s entry once the file has changed since it was recorded.

    For a reader that can accept a file another reader warned about: dropping the entry of the unchanged
    version would let the other reader's next read warn again, and two readers in one process would
    alternate on every poll.
    """
    key = warn_key(path)
    if seen.get(key) != file_version(path):
        seen.pop(key, None)


def forget(path: Path, seen: dict[str, FileVersion]) -> None:
    """Drop *path*'s entry because the file read cleanly."""
    seen.pop(warn_key(path), None)


def prune_directory(directory: Path, live: list[Path], seen: dict[str, FileVersion]) -> None:
    """Drop entries for files directly in *directory* that are not in *live*.

    An entry otherwise leaves only when its own path is later read cleanly, so a file that was
    archived, cleared or deleted by hand would keep its entry for the life of the process.
    """
    live_keys = {warn_key(p) for p in live}
    directory_key = directory.resolve()
    for key in [k for k in seen if Path(k).parent == directory_key and k not in live_keys]:
        del seen[key]


def forget_under(directory: Path, seen: dict[str, FileVersion]) -> None:
    """Drop every entry for a file at or below *directory*, for a caller whose scan just read all of them cleanly."""
    directory_key = directory.resolve()
    for key in [k for k in seen if directory_key in Path(k).parents]:
        del seen[key]
