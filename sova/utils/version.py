"""Version and build provenance helpers.

Single-sources the package version from installed distribution metadata
(``pyproject.toml`` via ``importlib.metadata``) instead of a hardcoded
string, so ``sova/__init__.py`` and ``pyproject.toml`` cannot drift apart.
"""

from __future__ import annotations

import importlib.metadata
import re
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from sova.utils.files import read_text_or_none
from sova.utils.logging import get_logger

log = get_logger(component="utils.version")

_GIT_SHA_TIMEOUT_SECONDS = 2
_SOURCE_DIR = Path(__file__).resolve().parents[2]
_CODE_SHA_TTL_SECONDS = 5.0
_FULL_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


@lru_cache(maxsize=1)
def get_version() -> str:
    """Return the installed SOVA package version.

    Cached: ``sova.__version__`` resolves through here on every access (see
    ``sova/__init__.py``'s PEP 562 ``__getattr__``), and ``/api/health``
    reads it per request, so the distribution-metadata scan runs once.
    """
    try:
        return importlib.metadata.version("sova")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0+unknown"


@lru_cache(maxsize=1)
def get_git_sha() -> str | None:
    """Return the short git commit SHA for this checkout, or None if unavailable.

    Best-effort and cached for the life of the process: a wheel install with
    no ``.git`` directory, a missing git binary, or a timed-out call all
    resolve to ``None`` rather than raising, since a packaged install has no
    git history to report.

    The ``.git`` check is what makes "no git history" mean ``None`` rather
    than some other repository's SHA: ``git rev-parse`` walks up from its
    ``cwd``, and a wheel installed into a venv inside a project checkout
    (``<project>/.venv/lib/pythonX/site-packages``) would otherwise report
    that project's HEAD as SOVA's build provenance. A linked worktree is
    still covered, since its ``.git`` is a file rather than a directory.
    """
    if not (_SOURCE_DIR / ".git").exists():
        return None

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_SOURCE_DIR,
            capture_output=True,
            text=True,
            timeout=_GIT_SHA_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.debug("version.git_sha_unavailable", exc_info=True)
        return None

    if result.returncode != 0:
        return None

    return result.stdout.strip() or None


class CodeStaleness(NamedTuple):
    """The checkout HEAD recorded at startup against the current one."""

    startup_sha: str | None
    current_sha: str | None
    restart_required: bool


def _read_packed_ref(git_dir: Path, ref: str) -> str | None:
    packed = read_text_or_none(git_dir / "packed-refs")
    if packed is None:
        return None
    for line in packed.splitlines():
        sha, _, name = line.partition(" ")
        if name.strip() == ref and _FULL_SHA.fullmatch(sha):
            return sha
    return None


def _resolve_head_sha(source_dir: Path) -> str | None:
    """Return the full HEAD SHA by reading ``.git`` files directly, or None when it cannot be read.

    No subprocess, so it is cheap enough for a per-request comparison. Handles a plain checkout, a
    linked worktree (``.git`` is a ``gitdir:`` pointer file and refs live in the ``commondir``), a
    detached HEAD, and a branch that exists only in ``packed-refs``. Any unreadable or malformed file
    (a ref being rewritten by ``git pull``, non-UTF-8 bytes) yields None rather than raising.
    """
    git_path = source_dir / ".git"
    if not git_path.exists():
        return None

    git_dir = git_path
    if git_path.is_file():
        pointer = read_text_or_none(git_path)
        if pointer is None or not pointer.startswith("gitdir:"):
            return None
        git_dir = source_dir / pointer[len("gitdir:") :].strip()

    head = read_text_or_none(git_dir / "HEAD")
    if head is None:
        return None
    head = head.strip()
    if _FULL_SHA.fullmatch(head):
        return head
    if not head.startswith("ref:"):
        return None
    ref = head[len("ref:") :].strip()

    common = read_text_or_none(git_dir / "commondir")
    common_dir = git_dir / common.strip() if common and common.strip() else git_dir
    for ref_dir in dict.fromkeys((git_dir, common_dir)):
        loose = read_text_or_none(ref_dir / ref)
        if loose is not None:
            sha = loose.strip()
            return sha if _FULL_SHA.fullmatch(sha) else None
    return _read_packed_ref(common_dir, ref)


_startup_code_sha: str | None = None
_startup_code_sha_recorded = False
_current_code_sha_cache: tuple[float, str | None] | None = None


def _current_code_sha() -> str | None:
    """Return the checkout's current HEAD, re-read from ``.git`` at most every few seconds."""
    global _current_code_sha_cache
    now = time.monotonic()
    if _current_code_sha_cache is not None and now - _current_code_sha_cache[0] < _CODE_SHA_TTL_SECONDS:
        return _current_code_sha_cache[1]
    sha = _resolve_head_sha(_SOURCE_DIR)
    _current_code_sha_cache = (now, sha)
    return sha


def record_startup_code_sha() -> str | None:
    """Record the checkout HEAD the running code was loaded from; the first call wins.

    Later calls (a second ``create_app``) return the original value, so they cannot overwrite the
    true startup SHA with a newer one.
    """
    global _startup_code_sha, _startup_code_sha_recorded
    if not _startup_code_sha_recorded:
        _startup_code_sha = _current_code_sha()
        _startup_code_sha_recorded = True
        log.info("version.startup_code_sha", code_sha=_startup_code_sha)
    return _startup_code_sha


def get_code_staleness() -> CodeStaleness:
    """Compare the startup HEAD with the current one.

    ``restart_required`` is true only when both are known and differ, so a wheel install, an unreadable
    ``.git`` or a read that fails mid-checkout never reports stale. A checkout that moves and then
    returns to the startup SHA reports false again.
    """
    startup = _startup_code_sha
    current = _current_code_sha()
    return CodeStaleness(startup, current, startup is not None and current is not None and startup != current)
