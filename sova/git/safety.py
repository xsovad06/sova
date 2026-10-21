"""Shared suspicious-staged-file guard.

Leaf module (no intra-sova imports beyond utils) so both ``sova/git/branch.py``
(``commit()``, which raises on a match) and ``sova/git/worktree.py``
(``preserve_partial_work()``, which logs and continues) can share one
implementation instead of drifting apart (issue #1260 review).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="git.safety")

SUSPICIOUS_PATHS = frozenset(
    {
        ".venv",
        ".env",
        ".env.local",
        "credentials.json",
        ".secrets",
        "node_modules",
        ".DS_Store",
        "__pycache__",
        ".mcp.json",
    }
)

# Chunk size for batched `git reset HEAD --` calls: even a conservative 128KB
# ARG_MAX budget holds thousands of short relative paths at this size, so a
# gitignore-less project whose `git add -A` swept a whole dependency tree
# into the suspicious set still unstages in a handful of subprocesses rather
# than one per offending file.
_RESET_CHUNK_SIZE = 200


@dataclass(frozen=True)
class SuspiciousUnstageResult:
    """Outcome of scanning the index for suspicious staged paths.

    ``checked=False`` means the staged-file query itself failed (e.g. a
    concurrent git process holding ``index.lock``), so nothing could be
    verified: callers must treat this the same as "unsafe", never as "clean".
    ``still_staged`` holds paths the reset attempt itself failed to remove
    from the index, verified by re-querying the index rather than trusting
    the reset command's own exit code.
    """

    checked: bool
    unstaged: tuple[str, ...] = field(default_factory=tuple)
    still_staged: tuple[str, ...] = field(default_factory=tuple)


def _is_suspicious(path: str) -> bool:
    return any(part in SUSPICIOUS_PATHS for part in Path(path.strip()).parts)


async def unstage_suspicious_files(cwd: Path | None) -> SuspiciousUnstageResult:
    """Find staged paths under :data:`SUSPICIOUS_PATHS` and unstage them.

    Returns which paths were verified removed from the index versus which
    remain staged despite the attempt (query failure, reset failure, or a
    reset that silently no-ops), so a caller can decide how strictly to fail.
    """
    staged = await run("git", "diff", "--cached", "--name-only", cwd=cwd)
    if not staged.success:
        return SuspiciousUnstageResult(checked=False)

    bad = sorted({f.strip() for f in staged.stdout.strip().splitlines() if _is_suspicious(f)})
    if not bad:
        return SuspiciousUnstageResult(checked=True)

    for i in range(0, len(bad), _RESET_CHUNK_SIZE):
        chunk = bad[i : i + _RESET_CHUNK_SIZE]
        await run("git", "reset", "HEAD", "--", *chunk, cwd=cwd)

    recheck = await run("git", "diff", "--cached", "--name-only", cwd=cwd)
    if not recheck.success:
        # Can't verify the reset worked either: treat every candidate as
        # still staged rather than trusting the attempt.
        return SuspiciousUnstageResult(checked=True, still_staged=tuple(bad))

    remaining = set(recheck.stdout.strip().splitlines())
    still_staged = tuple(f for f in bad if f in remaining)
    unstaged = tuple(f for f in bad if f not in remaining)
    return SuspiciousUnstageResult(checked=True, unstaged=unstaged, still_staged=still_staged)
