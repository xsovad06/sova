"""Version and build provenance helpers.

Single-sources the package version from installed distribution metadata
(``pyproject.toml`` via ``importlib.metadata``) instead of a hardcoded
string, so ``sova/__init__.py`` and ``pyproject.toml`` cannot drift apart.
"""

from __future__ import annotations

import importlib.metadata
import subprocess
from functools import lru_cache
from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="utils.version")

_GIT_SHA_TIMEOUT_SECONDS = 2


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
    source_dir = Path(__file__).resolve().parents[2]
    if not (source_dir / ".git").exists():
        return None

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=source_dir,
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
