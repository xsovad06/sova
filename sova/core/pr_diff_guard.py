"""Pure git/text analysis guard against silent reverts and dependency pin downgrades.

Deterministic by design: no LLM call anywhere in this module. A squash-capable
command (address-pr, rearrange-commits) rewrites a branch's history, which can
silently discard a sibling PR's change that landed on the base since the fork
point (see `.claude/rules/bash-patterns.md` "silent-revert-guard" and
`.claude/rules/architecture.md` "Silent Reverts"). This module is the
git-diff-level counterpart used by pipeline validators and command recipes,
not a replacement for the repo-structural `invariants/silent-revert-guard.sh`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sova.utils.logging import get_logger
from sova.utils.shell import run as run_shell

log = get_logger(component="core.pr_diff_guard")

# Bumped from the module's original 15s: a large repo or a slow link can
# legitimately take longer than that to fetch the base branch, and a timeout
# here degrades the whole guard to "inconclusive" rather than a real verdict.
# Not config-derived (no `sova.toml` knob exists for this specific fetch);
# raise this constant directly if a project still finds it too tight.
_FETCH_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class GuardResult:
    """Outcome of a guard check.

    ``ok=False`` always carries a human-readable ``reason``. A guard that
    could not reach a verdict (unresolvable merge-base, git failure) also
    returns ``ok=False``, but with ``inconclusive=True``: per the project's
    established "could not verify" vs "verified bad" distinction, the
    caller must not record this the same way as a confirmed revert (see
    `agent_db.py::_check_pr_diff_guard`, which maps it to the run's
    ``network_blocked``/"interrupted" outcome instead of "failed").
    """

    ok: bool
    reason: str | None = None
    files: frozenset[str] = frozenset()
    inconclusive: bool = False
    # The branch's fork point from the base, as resolved against ``pre_run_sha``
    # and ``head_ref`` respectively. Exposed so a caller running a second guard
    # (e.g. pin-downgrade detection) can diff from the same fork points rather
    # than from a stale pre-run SHA that predates a rebase onto a newer base
    # commit, and can tell "already present before this run" apart from
    # "introduced by this run" by diffing each side from its own fork point.
    pre_merge_base: str | None = None
    post_merge_base: str | None = None


@dataclass(frozen=True)
class PinDowngrade:
    """A dependency pin that moved backward in the diff."""

    file: str
    package: str
    from_version: str
    to_version: str


async def get_merge_base(repo_dir: Path | str, base_ref: str, head_ref: str = "HEAD") -> str | None:
    """Return the fork point SHA between ``base_ref`` and ``head_ref``, or None if unresolvable."""
    result = await run_shell("git", "merge-base", base_ref, head_ref, cwd=repo_dir)
    if not result.success:
        log.debug("pr_diff_guard.merge_base_failed", base_ref=base_ref, head_ref=head_ref, stderr=result.stderr[:200])
        return None
    return result.stdout.strip() or None


async def diff_file_set(repo_dir: Path | str, from_ref: str, to_ref: str) -> frozenset[str] | None:
    """Return the set of files that differ between two refs, or None on git failure."""
    result = await run_shell("git", "diff", "--name-only", from_ref, to_ref, cwd=repo_dir)
    if not result.success:
        log.debug("pr_diff_guard.diff_failed", from_ref=from_ref, to_ref=to_ref, stderr=result.stderr[:200])
        return None
    return frozenset(line.strip() for line in result.stdout.splitlines() if line.strip())


def detect_reverted_files(
    pre_run_files: frozenset[str],
    post_run_files: frozenset[str],
    base_changed_files: frozenset[str],
) -> frozenset[str]:
    """Return candidate files the command touched that are not this PR's own business.

    Flags a file only when it is both new to the diff (not in
    ``pre_run_files``, so the PR never intended to touch it) and one the base
    branch changed since the fork (``base_changed_files``). A file the PR
    already owned, or one the base left untouched, is never flagged: the
    first is an expected, already-reviewed change, and the second has no
    sibling-PR content to have silently discarded.

    This is a cheap pre-filter on path sets alone, not a confirmed revert: the
    branch may have made its own new edit to a file the base also happened to
    change. ``check_silent_revert`` confirms each candidate's content before
    reporting it.
    """
    return (post_run_files - pre_run_files) & base_changed_files


async def _blob_sha(repo_dir: Path | str, ref: str, path: str) -> str | None:
    """Return the git blob SHA for ``path`` at ``ref``, or None if unresolvable."""
    result = await run_shell("git", "rev-parse", f"{ref}:{path}", cwd=repo_dir)
    if not result.success:
        return None
    return result.stdout.strip() or None


async def _confirm_content_reverted(
    repo_dir: Path | str, path: str, head_ref: str, pre_merge_base: str, base_ref: str
) -> tuple[bool, bool]:
    """Confirm a set-identity candidate actually carries reverted content.

    Returns ``(reverted, inconclusive)``. Only the branch head's blob being
    byte-identical to the pre-sibling blob, while the base's own blob
    differs, is the "forked-before-the-fix content survived the rebase"
    signature this guard exists to catch. A branch that made its own new
    edit to a file the base also touched produces a different HEAD blob and
    is not a revert (``False, False``).

    The branch head having no blob at all (``head_blob is None``) while the
    file existed pre-run (``pre_blob`` resolved) means the branch legitimately
    deleted a path the base also changed: a deliberate deletion is not "the
    old content survived", so this is reported ``inconclusive`` rather than a
    confirmed revert. An unresolvable ``pre_blob`` (the path did not exist at
    the fork point at all, e.g. the base itself added it after the fork) means
    there is no pre-sibling content for the branch to have reverted: the
    branch head having content here is the branch's own edit of the base's
    new file (typically after rebasing onto it), so this is ``False, False``,
    not a confirmed revert. An unresolvable ``base_blob`` stays fail-closed
    as a confirmed revert (``True, False``).
    """
    head_blob = await _blob_sha(repo_dir, head_ref, path)
    if head_blob is None:
        return False, True
    pre_blob = await _blob_sha(repo_dir, pre_merge_base, path)
    if pre_blob is None:
        return False, False
    base_blob = await _blob_sha(repo_dir, base_ref, path)
    if base_blob is None:
        return True, False
    return head_blob == pre_blob and head_blob != base_blob, False


async def _commit_present_locally(repo_dir: Path | str, sha: str) -> bool:
    """Check whether ``sha`` resolves to a commit object already in the local repo."""
    result = await run_shell("git", "cat-file", "-e", f"{sha}^{{commit}}", cwd=repo_dir)
    return result.success


async def check_silent_revert(
    repo_dir: Path | str,
    base_branch: str,
    pre_run_sha: str,
    head_ref: str = "HEAD",
) -> GuardResult:
    """Check that a squash-capable command did not silently revert a sibling PR's change.

    Fetches ``base_branch`` first: a stale local ``origin/<base_branch>`` ref
    would silently miss a sibling PR that landed on the base very recently,
    which is exactly the scenario this guard exists to catch. Computes the
    PR's own file set both before (``pre_run_sha``) and after (``head_ref``)
    the command ran, each against its own fork point (the command may have
    rebased onto a newer base commit in between), then flags any file that
    is new to the diff and was also changed by the base since that fork
    point.

    ``pre_run_sha`` is a baseline recorded before this call, often the PR
    branch's remote head at command start: a commit pushed from elsewhere
    (the GitHub web UI, another machine) between that capture and this
    worktree's own last fetch of the branch may not exist in this repo yet.
    That is "cannot verify", not "verified bad", so a missing baseline (after
    trying to fetch ``head_ref`` to pull it in) skips the check with
    ``ok=True`` rather than failing closed.
    """
    base_ref = f"origin/{base_branch}"

    fetch_result = await run_shell("git", "fetch", "origin", base_branch, cwd=repo_dir, timeout=_FETCH_TIMEOUT_SECONDS)
    if not fetch_result.success:
        log.debug("pr_diff_guard.fetch_base_failed", base_branch=base_branch, stderr=fetch_result.stderr[:200])
        return GuardResult(ok=False, reason=f"could not fetch {base_ref} to verify the diff", inconclusive=True)

    if not await _commit_present_locally(repo_dir, pre_run_sha):
        if head_ref != "HEAD":
            await run_shell("git", "fetch", "origin", head_ref, cwd=repo_dir, timeout=_FETCH_TIMEOUT_SECONDS)
        if not await _commit_present_locally(repo_dir, pre_run_sha):
            log.info("pr_diff_guard.baseline_unavailable", pre_run_sha=pre_run_sha[:12])
            return GuardResult(ok=True)

    pre_merge_base = await get_merge_base(repo_dir, base_ref, pre_run_sha)
    if pre_merge_base is None:
        return GuardResult(
            ok=False,
            reason=f"could not resolve fork point for pre-run commit {pre_run_sha[:12]}",
            inconclusive=True,
        )

    post_merge_base = await get_merge_base(repo_dir, base_ref, head_ref)
    if post_merge_base is None:
        return GuardResult(ok=False, reason=f"could not resolve fork point for {head_ref}", inconclusive=True)

    pre_run_files = await diff_file_set(repo_dir, pre_merge_base, pre_run_sha)
    if pre_run_files is None:
        return GuardResult(
            ok=False,
            reason=f"could not diff pre-run commit {pre_run_sha[:12]} against its fork point",
            inconclusive=True,
        )

    post_run_files = await diff_file_set(repo_dir, post_merge_base, head_ref)
    if post_run_files is None:
        return GuardResult(ok=False, reason=f"could not diff {head_ref} against its fork point", inconclusive=True)

    # Diffed from pre_merge_base, not post_merge_base: address-pr's normal job
    # is to rebase onto the current base, which makes post_merge_base equal to
    # the base tip and this diff empty. The sibling's change lives between the
    # pre-run fork point and the current base tip.
    base_changed_files = await diff_file_set(repo_dir, pre_merge_base, base_ref)
    if base_changed_files is None:
        return GuardResult(ok=False, reason=f"could not diff {base_ref} against its fork point", inconclusive=True)

    candidates = detect_reverted_files(pre_run_files, post_run_files, base_changed_files)
    confirmed: list[str] = []
    deleted: list[str] = []
    for path in sorted(candidates):
        reverted, inconclusive = await _confirm_content_reverted(repo_dir, path, head_ref, pre_merge_base, base_ref)
        if inconclusive:
            deleted.append(path)
        elif reverted:
            confirmed.append(path)

    # A confirmed revert always wins over an inconclusive deletion: evaluating
    # every candidate (rather than returning on the first inconclusive one)
    # means a run that both legitimately deleted one file and silently
    # reverted another is reported as the revert it actually is, not
    # downgraded to an ambiguous "branch deleted X" because the deletion
    # happened to sort first.
    if confirmed:
        return GuardResult(
            ok=False,
            reason=(
                f"possible silent revert: {', '.join(confirmed)} "
                f"{'was' if len(confirmed) == 1 else 'were'} not part of this PR but "
                f"changed by {base_branch} since the fork point"
            ),
            files=frozenset(confirmed),
            pre_merge_base=pre_merge_base,
            post_merge_base=post_merge_base,
        )

    if deleted:
        return GuardResult(
            ok=False,
            reason=(f"branch deleted {', '.join(deleted)}, which {base_branch} also changed"),
            inconclusive=True,
            pre_merge_base=pre_merge_base,
            post_merge_base=post_merge_base,
        )

    return GuardResult(ok=True, pre_merge_base=pre_merge_base, post_merge_base=post_merge_base)


# Dependency files eligible for pin-downgrade detection, mapped to their parser.
_PYTHON_REQUIREMENT_RE = re.compile(
    r"^(?P<marker>[+-])\s*[\"']?(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*(?P<version>[A-Za-z0-9.!+*-]+)"
)
_NPM_LOCK_RE = re.compile(r'^(?P<marker>[+-])\s*"version":\s*"(?P<version>[^"]+)"')
_PACKAGE_JSON_DEP_RE = re.compile(r'^(?P<marker>[+-])\s*"(?P<name>[^"]+)":\s*"\^?~?(?P<version>\d[^"]*)"')

_PYTHON_DEP_FILES = ("requirements.txt", "requirements-dev.txt", "pyproject.toml")
_NPM_DEP_FILES = ("package.json", "package-lock.json")

# package.json keys that happen to match _PACKAGE_JSON_DEP_RE's shape (a quoted
# key followed by a quoted value starting with a digit) but name the package's
# own metadata, not a dependency pin. A project lowering its own version, or
# narrowing an engines range, is not a dependency pin downgrade.
_PACKAGE_JSON_METADATA_KEYS = frozenset(
    {"name", "version", "engines", "node", "npm", "main", "module", "license", "private", "packageManager"}
)


def _is_python_dep_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in _PYTHON_DEP_FILES or name.startswith("requirements")


def _is_npm_dep_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in _NPM_DEP_FILES


# Matches the line that opens a lockfile package entry, e.g.
# `"node_modules/requests": {` (npm v2/v3 "packages" object, keyed by path) or
# `"requests": {` (v1 "dependencies" object, keyed by bare name). Present on
# "+", "-", or unchanged context lines alike, since only tracking the key is
# needed here, not whether the entry itself changed.
_LOCKFILE_KEY_OPEN_RE = re.compile(r'^[ +-]\s*"(?P<key>[^"]+)":\s*\{\s*$')

# Container keys that open an object holding package entries, rather than a
# package entry themselves. A v2/v3 lockfile's root self-entry is keyed ""
# inside "packages" (`"packages": { "": { "version": ... } }`), and the empty
# key never matches `_LOCKFILE_KEY_OPEN_RE` (its capture group requires at
# least one character), so a version line nested directly under one of these
# containers would otherwise still be tagged with the container's own key
# and reported as a downgrade of a nonexistent "packages" or "dependencies"
# package. A real per-package entry always re-tags `current_key` to its own
# name before its version line, so this only ever filters a container's
# direct, unnamed version line.
_LOCKFILE_CONTAINER_KEYS = frozenset(
    {"packages", "dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "engines"}
)


def _lockfile_package_name(key: str) -> str:
    """Derive a reportable package name from a lockfile object key.

    A v2/v3 "packages" key is a path (``node_modules/<name>``, possibly
    nested as ``node_modules/a/node_modules/<name>``); a v1 "dependencies"
    key is already the bare name. Either way, the segment after the last
    "node_modules/" is the package name.
    """
    return key.rsplit("node_modules/", 1)[-1]


def _parse_pin_lines_positional(
    diff_text: str, pattern: re.Pattern[str]
) -> list[tuple[str | None, str | None, str | None]]:
    """Pair removed/added versions by their enclosing lockfile package key.

    ``_NPM_LOCK_RE`` has no ``name`` group: a lockfile's "version" line never
    carries the package name on its own line. Pairing by raw line adjacency
    alone mispairs an ordinary dependency replacement: when package A's
    entry is removed and package B's entry is added at the same diff
    position (e.g. `npm install` swapping one dependency for another), git
    groups the whole removed block before the whole added block, so A's
    "-version" line sits immediately before B's "+version" line with nothing
    in common between them.

    Each version line is instead tagged with the most recently seen
    enclosing object key (``_LOCKFILE_KEY_OPEN_RE``), and a removal is
    paired with the next addition only when both share that key; a removal
    whose key never recurs (the whole entry was deleted, or replaced by a
    differently-keyed entry) is discarded rather than paired with an
    unrelated addition. When no key line precedes a version line at all (a
    hand-built diff fragment with no surrounding object), the key is
    ``None`` for every line and pairing falls back to pure adjacency,
    matching the pre-existing behavior for that case.
    """
    pairs: list[tuple[str | None, str | None, str | None]] = []
    pending: dict[str | None, str] = {}
    current_key: str | None = None
    for line in diff_text.splitlines():
        key_match = _LOCKFILE_KEY_OPEN_RE.match(line)
        if key_match:
            current_key = key_match.group("key")
            continue
        m = pattern.match(line)
        if not m:
            continue
        if current_key in _LOCKFILE_CONTAINER_KEYS:
            continue
        version = m.group("version")
        if m.group("marker") == "-":
            pending[current_key] = version
        else:
            removed = pending.pop(current_key, None)
            if removed is not None:
                name = _lockfile_package_name(current_key) if current_key is not None else None
                pairs.append((name, removed, version))
    return pairs


def _parse_pin_lines_named(diff_text: str, pattern: re.Pattern[str]) -> list[tuple[str | None, str | None, str | None]]:
    """Pair removed/added versions by captured package name, per occurrence.

    A single (removed, added) slot per name would let a package pinned in
    two places in one file (``dependencies`` and ``devDependencies`` in
    package.json, or a project's own dependencies and an optional-dependency
    group in pyproject.toml) have the second occurrence silently overwrite
    the first: a removal from one section would pair with an addition from
    the other, masking or fabricating a downgrade. Each occurrence is
    instead collected in diff order and paired positionally per name: the
    Nth removal of a name pairs with the Nth addition of that same name; an
    unmatched occurrence (unequal counts) is dropped.
    """
    removed_by_name: dict[str, list[str]] = {}
    added_by_name: dict[str, list[str]] = {}
    name_order: list[str] = []
    for line in diff_text.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        groups = m.groupdict()
        name = groups["name"]
        version = groups["version"]
        if name not in removed_by_name and name not in added_by_name:
            name_order.append(name)
        bucket = removed_by_name if groups["marker"] == "-" else added_by_name
        bucket.setdefault(name, []).append(version)

    pairs: list[tuple[str | None, str | None, str | None]] = []
    for name in name_order:
        for removed, added in zip(removed_by_name.get(name, []), added_by_name.get(name, [])):
            pairs.append((name, removed, added))
    return pairs


def _parse_pin_lines(
    path: str, diff_text: str, pattern: re.Pattern[str]
) -> list[tuple[str | None, str | None, str | None]]:
    """Return a list of (package, removed_version, added_version) from a unified diff body.

    ``_NPM_LOCK_RE`` has no ``name`` group: a lockfile's "version" line never
    carries the package name itself. Grouping those lines by name would
    collapse every version change in the file into one dict key (all but the
    last pair silently discarded), so a file with no name capture is instead
    paired positionally (see ``_parse_pin_lines_positional``).

    ``_PACKAGE_JSON_DEP_RE`` also matches package.json's own metadata fields
    (``version``, ``engines.node``, ...), which share its "quoted key, quoted
    digit-leading value" shape but are not dependency pins; those are dropped
    here rather than reported as downgrades.
    """
    if "name" not in pattern.groupindex:
        pins = _parse_pin_lines_positional(diff_text, pattern)
    else:
        pins = _parse_pin_lines_named(diff_text, pattern)
    if path.endswith("package.json"):
        pins = [(name, removed, added) for name, removed, added in pins if name not in _PACKAGE_JSON_METADATA_KEYS]
    return pins


def _compare_versions(removed: str, added: str) -> bool | None:
    """Return True if ``added`` is older than ``removed``, None if unparseable."""
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:
        InvalidVersion = None  # noqa: N806 (mirrors the import name, both None means "not importable")
        Version = None  # noqa: N806

    if Version is not None:
        try:
            return Version(added) < Version(removed)
        except InvalidVersion:
            pass
        # A PEP 440 parse failure falls through to the coarser tuple-based comparison
        # below, not straight to "unknown": only that fallback's own unparseable
        # result means unknown.
        except Exception:  # noqa: BLE001 (unparseable version string must fall through to tuple comparison, not raise)
            pass

    def _tuple(v: str) -> tuple[int, ...] | None:
        parts = re.split(r"[.+-]", v)
        try:
            return tuple(int(p) for p in parts if p.isdigit())
        except ValueError:
            return None

    removed_t, added_t = _tuple(removed), _tuple(added)
    if removed_t is None or added_t is None or not removed_t or not added_t:
        return None
    return added_t < removed_t


def _pattern_for_dep_file(path: str) -> re.Pattern[str] | None:
    """Return the pin-matching pattern for a dependency file, or None if it isn't one."""
    if _is_python_dep_file(path):
        return _PYTHON_REQUIREMENT_RE
    if _is_npm_dep_file(path):
        return _PACKAGE_JSON_DEP_RE if path.endswith("package.json") else _NPM_LOCK_RE
    return None


def _downgrades_in_pins(path: str, pins: list[tuple[str | None, str | None, str | None]]) -> list[PinDowngrade]:
    """Filter parsed (name, removed, added) pins down to actual downgrades.

    Only flags a package that appears on both a removed and an added line:
    a pin appearing solely as added (new dependency) or solely as removed
    (deleted dependency) is never a downgrade.
    """
    downgrades: list[PinDowngrade] = []
    for name, removed, added in pins:
        if removed is None or added is None or removed == added:
            continue
        is_downgrade = _compare_versions(removed, added)
        if is_downgrade is None:
            log.info("pr_diff_guard.pin_version_unparseable", file=path, package=name, removed=removed, added=added)
            continue
        if is_downgrade:
            downgrades.append(
                PinDowngrade(file=path, package=name or "<lockfile entry>", from_version=removed, to_version=added)
            )
    return downgrades


async def _pin_downgrades_for_file(
    repo_dir: Path | str, from_ref: str, to_ref: str, path: str, pattern: re.Pattern[str]
) -> tuple[list[PinDowngrade], bool]:
    """Return ``(downgrades, inconclusive)`` for one dependency file's diff."""
    result = await run_shell("git", "diff", from_ref, to_ref, "--", path, cwd=repo_dir)
    if not result.success:
        log.warning("pr_diff_guard.pin_diff_failed", path=path, stderr=result.stderr[:200])
        return [], True
    return _downgrades_in_pins(path, _parse_pin_lines(path, result.stdout, pattern)), False


async def check_pin_downgrades(repo_dir: Path | str, from_ref: str, to_ref: str) -> tuple[list[PinDowngrade], bool]:
    """Detect dependency pins that moved to an older version in the diff.

    Returns ``(downgrades, inconclusive)``. ``diff_file_set()`` distinguishes
    "git failed" (``None``) from "nothing changed" (an empty set); collapsing
    both into "no downgrades" would silently disable this check on a git
    failure, since an empty result and a failed one are both in the
    function's success range. ``inconclusive=True`` means the scan could not
    be trusted (the file-set diff failed, or a per-file diff failed), per the
    project's "could not verify" vs "verified bad" distinction.
    """
    changed = await diff_file_set(repo_dir, from_ref, to_ref)
    if changed is None:
        log.warning("pr_diff_guard.pin_scan_diff_failed", from_ref=from_ref, to_ref=to_ref)
        return [], True
    if not changed:
        return [], False

    downgrades: list[PinDowngrade] = []
    inconclusive = False
    for path in sorted(changed):
        pattern = _pattern_for_dep_file(path)
        if pattern is None:
            continue
        file_downgrades, file_inconclusive = await _pin_downgrades_for_file(repo_dir, from_ref, to_ref, path, pattern)
        downgrades.extend(file_downgrades)
        inconclusive = inconclusive or file_inconclusive

    return downgrades, inconclusive
