"""Resolve a managed project's conventional-commit types and scopes.

SOVA's own lists apply only to the SOVA repository; the ``commit.types`` and ``commit.scopes`` field defaults are
neutral, so for any other project a key no layer sets is resolved per project. The resolution order is:

1. SOVA's own lists, only when the project is the SOVA repository.
2. The ``Valid types:`` / ``Valid scopes:`` line in the project's AGENTS.md, then CLAUDE.md.
3. The ``type(scope):`` subjects in recent ``git log``.

Each list resolves independently. This module never reads or writes the settings database: the loader
decides which keys are unset and persists the result (``sova/config/loader.py``).
"""

from __future__ import annotations

import re
import threading
import time
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sova.config.models import (
    _SCOPE_RE,
    _TYPE_RE,
    DEFAULT_COMMIT_SCOPES,
    DEFAULT_COMMIT_TYPES,
    FALLBACK_COMMIT_TYPES,
)
from sova.utils.env import scrub_orchestrator_env
from sova.utils.files import read_text_or_none
from sova.utils.logging import get_logger
from sova.utils.shell import ShellResult, run_sync

log = get_logger(component="config.commit_lists")

# Where a resolved list came from. "unresolved" means git history could not be read and nothing
# else supplied the list: the value is the fallback, but nobody looked at the project's own history.
# "empty" means the history was read but holds too little to derive from (no commits, or fewer than
# MIN_CONVENTIONAL_SUBJECTS conventional ones): also the fallback, but a guess made with no evidence, so it is
# never persisted and the list keeps resolving until there is enough history to derive from.
Kind = Literal["types", "scopes"]
Source = Literal["sova", "docs", "git", "fallback", "empty", "unresolved"]

# Types adopted from git history; a stray word before a colon ("wip:", "update:") is not a type.
CONVENTIONAL_TYPES: tuple[str, ...] = (
    "feat",
    "fix",
    "refactor",
    "test",
    "docs",
    "chore",
    "ci",
    "perf",
    "build",
    "style",
)

# Types used when a project has none of its own: the conventional set without "ci", which needs a scopeless form.
FALLBACK_TYPES: tuple[str, ...] = FALLBACK_COMMIT_TYPES

GIT_LOG_LIMIT = 200
GIT_LOG_TIMEOUT_S = 5
MIN_SCOPE_OCCURRENCES = 2
# Conventional subjects the history needs before it counts as evidence of the project's convention: a young
# repository with three "feat(x):" commits says nothing about whether it will ever write "fix" or "docs".
MIN_CONVENTIONAL_SUBJECTS = 20
# Types a dependency bot or a chore uses; a scope used only by these (``chore(deps)``) never leads the list.
_MAINTENANCE_TYPES = frozenset({"chore", "build", "ci"})
_BOT_AUTHOR_SUFFIX = "[bot]"
_DOC_FILES = ("AGENTS.md", "CLAUDE.md")

# Matches only the label: the values are the rest of the line, sliced off after the match, so no
# whitespace run is shared between two quantifiers (which backtracks super-linearly).
_DOC_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?valid\s+(?P<kind>types|scopes)(?:\*\*|__)?\s*:",
    re.IGNORECASE,
)
_BOLD_MARKERS = ("**", "__")
_SUBJECT_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)\s]+)\))?!?:\s")
# Applied to a stripped subject, so it needs no leading or trailing whitespace run.
_SQUASH_SUFFIX_RE = re.compile(r"\(#\d+\)$")

_SHAPES = {"types": _TYPE_RE, "scopes": _SCOPE_RE}


@dataclass(frozen=True)
class ResolvedList:
    """One resolved list and where it came from."""

    values: list[str]
    source: Source


def normalize_entries(raw: list[str], kind: Kind) -> list[str]:
    """Strip, lowercase and de-duplicate *raw* in order, dropping entries that cannot be a type or scope."""
    shape = _SHAPES[kind]
    seen: dict[str, None] = {}
    for entry in raw:
        cleaned = entry.strip().strip("`'\"").strip().lower()
        if cleaned and shape.match(cleaned):
            seen.setdefault(cleaned, None)
    return list(seen)


def is_sova_repository(project_dir: Path) -> bool:
    """True when *project_dir* is a SOVA checkout, a linked worktree of one included.

    Decided by the files in the directory (a ``pyproject.toml`` declaring the ``sova`` project and
    ``sova/config/models.py``), never by the installed package's location: that differs for a worktree.
    """
    if not (project_dir / "sova" / "config" / "models.py").is_file():
        return False
    text = read_text_or_none(project_dir / "pyproject.toml")
    if text is None:
        return False
    try:
        project = tomllib.loads(text).get("project", {})
    except tomllib.TOMLDecodeError:
        return False
    return isinstance(project, dict) and project.get("name") == "sova"


def _parse_doc_line(line: str) -> tuple[Kind, list[str]] | None:
    """The kind and entries of one ``Valid types:``/``Valid scopes:`` line, or None for any other line."""
    match = _DOC_LINE_RE.match(line)
    if match is None:
        return None
    kind: Kind = "types" if match.group("kind").lower() == "types" else "scopes"
    values = line[match.end() :]
    for marker in _BOLD_MARKERS:
        values = values.removeprefix(marker)
    values = values.strip().rstrip(".")
    return kind, normalize_entries(values.split(","), kind)


def parse_docs_lists(project_dir: Path) -> dict[Kind, list[str]]:
    """Read ``Valid types:``/``Valid scopes:`` from AGENTS.md, then CLAUDE.md; the first non-empty list wins."""
    found: dict[Kind, list[str]] = {}
    for name in _DOC_FILES:
        text = read_text_or_none(project_dir / name)
        if text is None:
            continue
        for line in text.splitlines():
            parsed = _parse_doc_line(line)
            if parsed is not None and parsed[1]:
                found.setdefault(parsed[0], parsed[1])
    return found


# Variables that make git read a repository other than the one at `cwd` (a git hook exports GIT_DIR). Left in
# the child environment they would turn another repository's history into this project's commit conventions.
_GIT_LOCATION_VARS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
)


def _run_git(project_dir: Path, args: list[str]) -> ShellResult:
    """Run git in *project_dir* with the scrubbed environment, no inherited repository location, and a bounded wait."""
    env = {key: value for key, value in scrub_orchestrator_env().items() if key not in _GIT_LOCATION_VARS}
    return run_sync("git", *args, cwd=project_dir, env=env, timeout=GIT_LOG_TIMEOUT_S)


def _has_git_ancestor(project_dir: Path) -> bool:
    """True when *project_dir* or a parent holds a ``.git`` (a directory, or a file in a linked worktree).

    A cheap filesystem walk, so a plain directory never spawns git on every config load. It only says a
    repository exists somewhere above: :func:`_git_subjects` still checks the repository tracks this project.
    """
    return any((candidate / ".git").exists() for candidate in (project_dir, *project_dir.parents))


def _git_subjects(project_dir: Path) -> list[str] | None:
    """The last commit subjects, or None when git history could not be read (an empty history is ``[]``).

    Git finds the enclosing repository itself, so a project in a subdirectory of a repository (a monorepo
    package) reads the whole history; a directory with no ``.git`` above it is unresolved without running git.
    "No repository" and "no commits" are told apart by exit status (``rev-parse``), never by git's localized
    stderr text. An enclosing repository that tracks nothing under the project directory (a dotfiles repository
    in ``$HOME``, one that ignores the directory) is not the project's history: unresolved. Commits by a bot
    (author ending ``[bot]``: Dependabot, Renovate) are left out, as they are not the project's own convention.
    """
    if not _has_git_ancestor(project_dir):
        return None
    try:
        head = _run_git(project_dir, ["rev-parse", "--verify", "-q", "HEAD"])
        if head.returncode != 0:
            inside = _run_git(project_dir, ["rev-parse", "--is-inside-work-tree"])
            # Inside a work tree with no resolvable HEAD: a repository with no commits yet.
            if inside.returncode == 0 and inside.stdout.strip() == "true":
                return []
            log.warning("commit_lists.git_log_failed", project_dir=str(project_dir), stderr=inside.stderr.strip()[:200])
            return None
        tracked = _run_git(project_dir, ["ls-files", "--", "."])
        if tracked.returncode != 0 or not tracked.stdout.strip():
            log.warning(
                "commit_lists.git_log_failed",
                project_dir=str(project_dir),
                reason="no file under the project directory is tracked by the enclosing repository",
                stderr=tracked.stderr.strip()[:200],
            )
            return None
        result = _run_git(project_dir, ["log", f"-n{GIT_LOG_LIMIT}", "--no-merges", "--format=%an%x00%s"])
    except OSError:
        log.warning("commit_lists.git_log_failed", project_dir=str(project_dir), exc_info=True)
        return None
    if result.returncode != 0:
        log.warning("commit_lists.git_log_failed", project_dir=str(project_dir), stderr=result.stderr.strip()[:200])
        return None
    subjects: list[str] = []
    for line in result.stdout.splitlines():
        author, _, subject = line.partition("\x00")
        if subject.strip() and not author.strip().lower().endswith(_BOT_AUTHOR_SUFFIX):
            subjects.append(subject)
    return subjects


def _conventional_matches(subjects: list[str]) -> list[tuple[str, str | None]]:
    """``(type, scope or None)`` of each ``type(scope):`` subject; reverts and unknown types are skipped."""
    matches: list[tuple[str, str | None]] = []
    for subject in subjects:
        subject = _SQUASH_SUFFIX_RE.sub("", subject.strip()).rstrip()
        if subject.lower().startswith("revert"):
            continue
        match = _SUBJECT_RE.match(subject)
        if match is None or match.group("type") not in CONVENTIONAL_TYPES:
            continue
        scope = match.group("scope")
        matches.append((match.group("type"), scope.lower() if scope else None))
    return matches


def derive_from_subjects(subjects: list[str]) -> dict[Kind, list[str]]:
    """Types and scopes used by the ``type(scope):`` subjects, most frequent first.

    A trailing ``(#N)`` is ignored, a revert is skipped, and a scope must appear at least
    ``MIN_SCOPE_OCCURRENCES`` times so a typo is not adopted. Scopes are ranked by their use outside
    chore, build and ci subjects first, so a dependency-bump scope (``chore(deps)``) never leads the list:
    the first scope is the fallback for a change no path places.
    """
    types: Counter[str] = Counter()
    scopes: Counter[str] = Counter()
    substantive: Counter[str] = Counter()
    for kind, scope in _conventional_matches(subjects):
        types[kind] += 1
        if scope:
            scopes[scope] += 1
            if kind not in _MAINTENANCE_TYPES:
                substantive[scope] += 1
    ranked = sorted(
        (s for s, n in scopes.items() if n >= MIN_SCOPE_OCCURRENCES), key=lambda s: (-substantive[s], -scopes[s])
    )
    return {
        "types": [t for t, _ in types.most_common()],
        "scopes": normalize_entries(ranked, "scopes"),
    }


def resolve_commit_lists(project_dir: Path, kinds: tuple[Kind, ...] = ("types", "scopes")) -> dict[Kind, ResolvedList]:
    """Resolve each of *kinds* that no configuration layer sets, per the order in the module docstring."""
    if is_sova_repository(project_dir):
        defaults = {"types": list(DEFAULT_COMMIT_TYPES), "scopes": list(DEFAULT_COMMIT_SCOPES)}
        return {kind: ResolvedList(defaults[kind], "sova") for kind in kinds}

    docs = parse_docs_lists(project_dir)
    resolved = {kind: ResolvedList(docs[kind], "docs") for kind in kinds if kind in docs}
    pending = [kind for kind in kinds if kind not in resolved]
    if not pending:
        return resolved

    subjects = _git_subjects(project_dir)
    enough = subjects is not None and len(_conventional_matches(subjects)) >= MIN_CONVENTIONAL_SUBJECTS
    derived = derive_from_subjects(subjects) if subjects is not None and enough else {}
    for kind in pending:
        values = derived.get(kind)
        source: Source
        if subjects is None:
            source = "unresolved"
        elif not enough:
            source = "empty"
        elif values:
            source = "git"
        else:
            source = "fallback"
        if kind == "types":
            # History can add perf, build, ci or style, but never remove fix, docs or test.
            values = [*(values or []), *(t for t in FALLBACK_TYPES if t not in (values or []))]
        resolved[kind] = ResolvedList(values or [], source)
    return resolved


# `load_config()` resolves an unset list on every uncached load, and the git call is synchronous, so the
# result is kept for a while per project. The entry carries the docs' mtimes, so an edited AGENTS.md or CLAUDE.md
# is seen at once; new commits are seen after the TTL, or at once through `clear_resolution_cache()`
# (`invalidate_config_cache()` calls it). `persist_commit_lists()` and `sova doctor` never use it. The key is
# `(project_dir, kinds)` and the stamp is compared, so an edited doc replaces its entry instead of adding one.
# `load_config()` adds `docs_stamp()` to its own cache fingerprint, so the config cache sees an edit too.
_RESOLUTION_TTL_S = 60.0
_resolution_cache: dict[
    tuple[str, tuple[Kind, ...]], tuple[float, tuple[int | None, ...], dict[Kind, ResolvedList]]
] = {}
_resolution_lock = threading.Lock()


def docs_stamp(project_dir: Path) -> tuple[int | None, ...]:
    """The mtime of each doc the resolver reads (None when absent), so an edit changes the cache key."""
    stamp: list[int | None] = []
    for name in _DOC_FILES:
        try:
            stamp.append((project_dir / name).stat().st_mtime_ns)
        except OSError:
            stamp.append(None)
    return tuple(stamp)


def resolve_commit_lists_cached(
    project_dir: Path, kinds: tuple[Kind, ...] = ("types", "scopes")
) -> dict[Kind, ResolvedList]:
    """:func:`resolve_commit_lists`, memoized for a short while so a config reload does not rerun git."""
    key = (str(project_dir), kinds)
    stamp = docs_stamp(project_dir)
    now = time.monotonic()
    with _resolution_lock:
        hit = _resolution_cache.get(key)
        if hit is not None and hit[1] == stamp and now - hit[0] < _RESOLUTION_TTL_S:
            return dict(hit[2])
    resolved = resolve_commit_lists(project_dir, kinds)
    with _resolution_lock:
        _resolution_cache[key] = (time.monotonic(), stamp, resolved)
    return dict(resolved)


def clear_resolution_cache(project_dir: Path | None = None) -> None:
    """Forget memoized resolutions for *project_dir* (every project when None)."""
    with _resolution_lock:
        if project_dir is None:
            _resolution_cache.clear()
            return
        prefix = str(project_dir)
        for key in [k for k in _resolution_cache if k[0] == prefix]:
            del _resolution_cache[key]
