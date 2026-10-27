"""Fold a pre-push hook fix into the branch commit it belongs to.

Used by ``ValidateStep`` after its hook-fix LLM call edits the worktree (or,
for a commit-message-only violation, rewords a commit itself): rather than
appending a new "fix pre-push hook violations" commit, the fix is squashed
into the commit it actually belongs to via ``git commit --fixup`` plus a
non-interactive ``git rebase --autosquash``, scoped to commits not yet
reachable from the base branch.

Every outcome other than "the fix is folded and no marker commit survives"
unwinds back to the fix as a staged diff. A leftover ``fixup!``/``squash!``
subject is worse than the separate commit this replaces: no
conventional-commit hook accepts it, and it would reach ``git push`` in the
step that was supposed to make the branch pushable.

The fold target can be a commit already pushed to this branch's own remote
tip (``resolve_fixup_target`` only excludes commits reachable from the base
branch, not from ``origin/<this branch>``), so folding routinely rewrites
commits ``ValidateStep`` runs before the step that pushes them. That is
intended: this branch is SOVA's own, every pipeline that reaches this module
already force-pushes with ``--force-with-lease`` afterward, and
``PushStep._log_dropped_commits`` announces exactly what the rewrite dropped.
The lease only has to protect against a concurrent writer moving origin
since the last fetch, not against this agent rewriting its own history.
"""

from __future__ import annotations

import os
from pathlib import Path

from sova.core.pr_diff_guard import get_merge_base
from sova.core.steps.commit import is_agent_artifact
from sova.git.safety import unstage_suspicious_files
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step._commit_fold")

_AUTOSQUASH_PREFIXES = ("fixup! ", "squash! ", "amend! ")


def _noninteractive_rebase_env() -> dict[str, str]:
    """Build the env every non-interactive ``git rebase`` call in this module shares.

    ``GIT_SEQUENCE_EDITOR`` covers the todo list; ``GIT_EDITOR`` covers the
    commit message editor a ``squash!`` commit still opens, which would
    otherwise block on the operator's interactive editor until the step
    timeout. Snapshotting ``os.environ`` fresh on each call (rather than
    once at import time) keeps this correct under a test that mutates the
    environment after import.
    """
    return {**os.environ, "GIT_SEQUENCE_EDITOR": "true", "GIT_EDITOR": "true"}


async def _changed_paths(cwd: Path | str | None) -> list[str]:
    """Return every staged, unstaged, and untracked path, relative to the repo root.

    Uses NUL-separated ``-z`` output with ``core.quotePath=false`` pinned for
    this call, rather than plain newline-separated ``--porcelain`` text: with
    the default ``core.quotePath=true``, git renders any non-ASCII or
    special-character path as a double-quoted, C-escaped string (e.g.
    ``"h\\303\\251llo.txt"``), and reading that quoted text as a literal path
    makes every later ``git log -- <path>`` lookup match nothing, silently
    folding the fix into ``HEAD`` instead of the commit that actually owns
    it. For a rename/copy, ``-z`` emits the new path followed by a second
    NUL-separated field holding the old path; only the new path is kept,
    since that is what branch history is matched against.
    """
    result = await run(
        "git", "-c", "core.quotePath=false", "status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=cwd
    )
    if not result.success:
        return []
    fields = result.stdout.split("\0")
    paths = []
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if not field:
            continue
        status, path = field[:2], field[3:]
        if path:
            paths.append(path)
        if "R" in status or "C" in status:
            # The next NUL-separated field is the rename/copy's old path,
            # which is not itself a changed path to match against. A copy
            # ("C", with status.renames=copies configured) carries this
            # second field exactly like a rename does.
            i += 1
    return paths


async def _head_sha(cwd: Path | str | None) -> str | None:
    result = await run("git", "rev-parse", "HEAD", cwd=cwd)
    return result.stdout.strip() if result.success else None


async def _is_on_base(cwd: Path | str | None, sha: str, base_ref: str) -> bool:
    """Return True only when *sha* is provably reachable from *base_ref*.

    Fails closed: a git error (no such ref, not a repo) is reported as "on
    base" so no caller ever rewrites a commit it could not place, which is
    the one thing the fold must never do.
    """
    result = await run("git", "merge-base", "--is-ancestor", sha, base_ref, cwd=cwd)
    if result.success:
        return True
    # Exit code 1 is the definitive "not an ancestor"; anything else (128 for
    # a missing ref) means the question went unanswered.
    return result.returncode != 1


async def _resolve_base_ref(cwd: Path | str | None, base_branch: str) -> str | None:
    """Resolve the ref this module diffs/rebases against, tolerating a missing remote.

    Tries ``origin/<base>`` (the common case), then the local ``<base>``
    branch, then the current branch's own ``@{upstream}``, returning the
    first that resolves to a real commit. A worktree with no fetched
    ``origin/<base>`` (a stacked base branch, a locally-initialized repo, a
    remote named something other than ``origin``) previously made every
    caller in this module fail closed to "nothing can be folded", turning a
    routine hook-fix into a hard ``ValidateStep`` failure. Returns ``None``
    only when none of the three resolve, e.g. a fully local, remoteless repo
    whose branch name matches none of this chain.
    """
    for candidate in (f"origin/{base_branch}", base_branch, "@{upstream}"):
        result = await run("git", "rev-parse", "--verify", "--quiet", candidate, cwd=cwd)
        if result.success:
            return candidate
    return None


async def resolve_fixup_target(
    cwd: Path | str | None, base_branch: str, changed_paths: list[str] | None = None
) -> str | None:
    """Pick the single commit a staged fix should fold into.

    Returns the most recent commit in ``<base_ref>..HEAD`` that last touched
    every changed path (*changed_paths*, re-read from ``git status`` when
    omitted), where ``base_ref`` is resolved by :func:`_resolve_base_ref`
    (``origin/<base>``, falling back to the local branch or ``@{upstream}``).
    Falls back to ``HEAD`` when the changed paths span more than one such
    commit, when any path was never touched by a branch commit, when the
    resolved commit is (unexpectedly) reachable from the base branch (a
    fixup must never target a commit already on the base), when the resolved
    commit's subject is shared by another commit in range (``--autosquash``
    matches a ``fixup!`` marker by subject text, so a duplicate makes the
    target ambiguous and HEAD is the one commit that can never be confused
    for another), or when no base ref could be resolved at all. Returns
    ``None`` when ``HEAD`` itself cannot be resolved (not a git repo, or no
    commits yet), and equally when ``HEAD`` is itself reachable from the base
    ref: a branch with no commits of its own has no commit the fix may
    legally be folded into, and targeting the base commit anyway leaves an
    unsquashable ``fixup!`` commit behind (its target is outside the rebase
    range) whose subject no conventional-commit hook accepts.
    """
    head = await _head_sha(cwd)
    if head is None:
        return None

    base_ref = await _resolve_base_ref(cwd, base_branch)
    if base_ref is None:
        log.warning("step._commit_fold.base_ref_unresolved", base_branch=base_branch)
        return head

    if await _is_on_base(cwd, head, base_ref):
        log.warning("step._commit_fold.no_branch_commit", base_branch=base_branch)
        return None

    changed = await _changed_paths(cwd) if changed_paths is None else changed_paths
    if not changed:
        return head

    commits: set[str] = set()
    for path in changed:
        log_result = await run("git", "log", "--format=%H", f"{base_ref}..HEAD", "--", path, cwd=cwd)
        lines = [ln for ln in log_result.stdout.splitlines() if ln.strip()] if log_result.success else []
        if not lines:
            return head
        commits.add(lines[0])

    if len(commits) != 1:
        return head
    target = commits.pop()

    if await _is_on_base(cwd, target, base_ref):
        return head

    subjects_result = await run("git", "log", "--format=%s", f"{base_ref}..HEAD", cwd=cwd)
    if subjects_result.success:
        target_subject = await run("git", "log", "--format=%s", "-1", target, cwd=cwd)
        subjects = subjects_result.stdout.splitlines()
        if target_subject.success and subjects.count(target_subject.stdout.strip()) > 1:
            log.warning("step._commit_fold.duplicate_subject_target", target=target)
            return head

    return target


async def _staged_paths(cwd: Path | str | None) -> list[str]:
    result = await run("git", "diff", "--cached", "--name-only", cwd=cwd)
    return [p for p in result.stdout.splitlines() if p.strip()] if result.success else []


async def _restore_staged_paths(cwd: Path | str | None, paths: list[str]) -> None:
    """Unstage everything, then re-stage exactly *paths* (the pre-``add`` index state).

    Called on every abort path after ``git add -A`` has already run: without
    this, the abort leaves the whole worktree (including the legitimate fix)
    staged, and the operator sees ``validate_output()``'s generic "staged
    but uncommitted changes" summary instead of the real cause.
    """
    await run("git", "reset", cwd=cwd)
    if paths:
        await run("git", "add", "--", *paths, cwd=cwd)


async def _fold_uncommitted_changes(cwd: Path | str | None, base_branch: str) -> str | None:
    """Stage any uncommitted diff and fold it into its resolved target commit.

    Returns the pre-fixup ``HEAD`` sha when a ``fixup!`` commit was created,
    so a failed autosquash can be unwound back to exactly this state, or
    ``None`` when nothing was committed.
    """
    changed = [p for p in await _changed_paths(cwd) if not is_agent_artifact(p)]
    if not changed:
        return None  # Clean tree, or only agent/tool artifacts: nothing to fold.

    target = await resolve_fixup_target(cwd, base_branch, changed)
    if target is None:
        return None

    pre_fixup_head = await _head_sha(cwd)
    if pre_fixup_head is None:
        return None

    pre_add_staged = await _staged_paths(cwd)

    # Staged explicitly by path, not `git add -A`: the latter sweeps in
    # whatever agent/tool artifact (`.claude/`, `.agents/`, ...) sits
    # alongside the fix, folding tooling noise into a real branch commit and
    # pushing it with the PR. `CommitStep` and `ValidateStep`'s own gates
    # already exclude these paths from "the agent's own work"; this is the
    # one place in the fold pipeline that previously disagreed with them.
    add_result = await run("git", "add", "--", *changed, cwd=cwd)
    if not add_result.success:
        log.warning("step._commit_fold.add_failed", stderr=add_result.stderr[:200])
        return None

    # Same guard git_ops.commit() applies, for the same reason: `git add -A`
    # sweeps in whatever else the fix agent left in the worktree, and a
    # credential or dependency tree folded into a branch commit is pushed
    # with it. Fail closed when the index cannot be verified.
    scan = await unstage_suspicious_files(cwd)
    bad = scan.unstaged + scan.still_staged
    if not scan.checked or bad:
        log.warning(
            "step._commit_fold.suspicious_files",
            checked=scan.checked,
            paths=list(bad[:10]),
        )
        await _restore_staged_paths(cwd, pre_add_staged)
        return None

    # Autosquash removes this transient marker before the pre-push hook runs;
    # a project's own commit-msg/pre-commit hook must not block it first.
    fixup_result = await run("git", "commit", "--no-verify", "--fixup", target, cwd=cwd)
    if not fixup_result.success:
        log.warning("step._commit_fold.fixup_commit_failed", stderr=fixup_result.stderr[:200])
        await _restore_staged_paths(cwd, pre_add_staged)
        return None
    return pre_fixup_head


async def _has_pending_autosquash_commits(cwd: Path | str | None, base_branch: str) -> bool | None:
    """Check for a fixup!/squash!/amend! commit the agent reworded by itself.

    Returns ``None`` when the check itself could not run (the ``git log``
    call failed, or no base ref could be resolved at all): the rest of the
    module treats ``None`` as "markers may survive", fail-closed like
    :func:`_is_on_base`, rather than letting an unresolvable question read as
    "nothing pending" and silently let a marker commit through.
    """
    base_ref = await _resolve_base_ref(cwd, base_branch)
    if base_ref is None:
        return None
    result = await run("git", "log", "--format=%s", f"{base_ref}..HEAD", cwd=cwd)
    if not result.success:
        return None
    return any(subject.startswith(_AUTOSQUASH_PREFIXES) for subject in result.stdout.splitlines())


async def has_unresolved_marker_commits(cwd: Path | str | None, base_branch: str) -> bool:
    """Return True when a fixup!/squash!/amend! commit is confirmed still on the branch.

    Public wrapper so ``ValidateStep`` can tell, after calling
    :func:`fold_fix_into_branch`, whether a ``False`` result means "nothing
    needed folding" or "a marker commit survived and the branch is not
    pushable", and fail the attempt in the latter case instead of retrying
    the hook on an unpushable branch. Deliberately *not* fail-closed the way
    :func:`_has_pending_autosquash_commits` is for :func:`_fold`'s own
    internal decisions: failing this gate aborts the whole validate step,
    a far more disruptive consequence than that function's own
    "try harder to clean up, then log and retry" path, so only a
    *confirmed* marker commit trips it. An unresolvable check (e.g. no
    ``origin/<base>`` tracking ref yet on a brand-new worktree, a condition
    this module elsewhere handles routinely) or a git/filesystem error
    severe enough to raise both return ``False``, leaving the step to
    retry the hook exactly as it did before this check existed.
    """
    try:
        return await _has_pending_autosquash_commits(cwd, base_branch) is True
    except OSError:
        log.warning("step._commit_fold.unavailable", exc_info=True)
        return False


async def _marker_commits(cwd: Path | str | None, base_branch: str) -> list[str]:
    """Return the SHAs of every surviving marker commit, oldest first."""
    base_ref = await _resolve_base_ref(cwd, base_branch)
    if base_ref is None:
        return []
    result = await run("git", "log", "--format=%H %s", f"{base_ref}..HEAD", cwd=cwd)
    if not result.success:
        return []
    shas = []
    for line in result.stdout.splitlines():
        sha, _, subject = line.partition(" ")
        if subject.startswith(_AUTOSQUASH_PREFIXES):
            shas.append(sha)
    return list(reversed(shas))  # `git log` is newest-first.


async def _surviving_marker_subjects(cwd: Path | str | None, base_branch: str) -> list[str]:
    base_ref = await _resolve_base_ref(cwd, base_branch)
    if base_ref is None:
        return []
    result = await run("git", "log", "--format=%s", f"{base_ref}..HEAD", cwd=cwd)
    if not result.success:
        return []
    return [s for s in result.stdout.splitlines() if s.startswith(_AUTOSQUASH_PREFIXES)]


async def _is_empty_commit(cwd: Path | str | None, sha: str) -> bool:
    result = await run("git", "diff", "--quiet", f"{sha}^", sha, cwd=cwd)
    return result.returncode == 0


_MAX_MARKER_DROP_ITERATIONS = 10


async def _drop_stranded_empty_markers(cwd: Path | str | None, base_branch: str) -> bool:
    """Drop any surviving marker commit that carries no actual diff.

    A ``fixup!``/``squash!``/``amend!`` commit whose target sits outside the
    rebase range is replayed verbatim by ``--autosquash`` rather than being
    squashed away. When that commit is empty (the agent's own stray reword
    marker, created with ``--allow-empty`` and aimed at an unreachable
    target), there is nothing to lose by dropping it outright:
    ``git rebase --onto <marker>^ <marker>`` replays everything after it
    onto its own parent, removing exactly that one commit. A non-empty
    marker is left alone (see :func:`_fold`): rewriting or dropping real
    content without knowing the intended message risks losing work.
    """
    dropped_any = False
    for _ in range(_MAX_MARKER_DROP_ITERATIONS):
        markers = await _marker_commits(cwd, base_branch)
        if not markers:
            break
        # Check every surviving marker, not just the oldest: an empty,
        # droppable marker that happens to sit newer than a non-empty one
        # was previously never reached, since the loop bailed out as soon as
        # markers[0] (oldest) turned out non-empty.
        sha = None
        for candidate in markers:
            if await _is_empty_commit(cwd, candidate):
                sha = candidate
                break
        if sha is None:
            break
        # --no-autosquash: a user's rebase.autoSquash=true must not change
        # what this drop does, and still opens the commit message editor for
        # any squash!/amend! marker it would otherwise replay, which the
        # same non-interactive env below neutralizes.
        result = await run(
            "git",
            "rebase",
            "--no-autosquash",
            "--onto",
            f"{sha}^",
            sha,
            cwd=cwd,
            env=_noninteractive_rebase_env(),
        )
        if not result.success:
            await run("git", "rebase", "--abort", cwd=cwd)
            break
        dropped_any = True
    return dropped_any


async def _autosquash(cwd: Path | str | None, base_branch: str) -> bool:
    """Non-interactively fold every fixup!/squash!/amend! commit into its target."""
    base_ref = await _resolve_base_ref(cwd, base_branch)
    if base_ref is None:
        log.warning("step._commit_fold.base_ref_unresolved", base_branch=base_branch)
        return False

    merge_base = await get_merge_base(cwd or Path.cwd(), base_ref)
    if merge_base is None:
        log.warning("step._commit_fold.merge_base_unresolved", base_branch=base_branch)
        return False

    # --rebase-merges: without it, a merge commit on the branch (a merge from
    # the base, or a conflict-resolution merge) is silently flattened by the
    # rebase, which has nothing to do with folding a single fixup in.
    result = await run(
        "git",
        "rebase",
        "-i",
        "--autosquash",
        "--rebase-merges",
        merge_base,
        cwd=cwd,
        env=_noninteractive_rebase_env(),
    )
    if result.success:
        return True

    log.warning("step._commit_fold.autosquash_failed", stderr=result.stderr[:300])
    await run("git", "rebase", "--abort", cwd=cwd)
    return False


async def _fold(cwd: Path | str | None, base_branch: str) -> bool:
    pre_fixup_head = await _fold_uncommitted_changes(cwd, base_branch)
    pending = await _has_pending_autosquash_commits(cwd, base_branch)
    needs_autosquash = pre_fixup_head is not None or pending is not False
    if not needs_autosquash:
        return False  # Hook failure resolved itself with no diff at all.

    squashed = await _autosquash(cwd, base_branch)
    pending_after = await _has_pending_autosquash_commits(cwd, base_branch)
    # The rebase exiting 0 is not proof the marker commits are gone: a
    # fixup! whose target sits outside the rebase range is simply replayed
    # as an ordinary commit, so the branch would keep a commit no
    # conventional-commit hook accepts. An unresolvable check (None) is
    # treated the same as "markers may survive", fail-closed.
    if squashed and pending_after is False:
        return True

    # A surviving marker with no real content (an agent-authored reword
    # marker aimed at a target outside the rebase range) can be dropped
    # outright, covering the agent-created case the unwind below never
    # reaches (it only undoes a fixup commit this module created itself).
    if await _drop_stranded_empty_markers(cwd, base_branch):
        pending_after = await _has_pending_autosquash_commits(cwd, base_branch)

    if squashed and pending_after is False and pre_fixup_head is not None:
        # Our own fixup commit was actually squashed in; the marker that
        # kept pending_after True right after the rebase was an unrelated
        # stray empty one, just dropped above, not a sign our own autosquash
        # failed. Checking this before the generic branch below matters:
        # unwinding here would reset past a completed, correct fold of a
        # real fix. (A pure marker-cleanup run with no fix of its own,
        # pre_fixup_head is None, still falls through to "nothing to
        # unwind" below: nothing of ours was ever folded in that case.)
        return True

    if pending_after is False and pre_fixup_head is None:
        return False  # Marker cleaned up (or never existed); nothing to unwind.

    if pre_fixup_head is not None:
        # Undo our own fixup commit, restoring the fix as a staged diff: the
        # caller retries the hook with the same file contents, and the
        # validate gate reports the leftover staged change honestly instead
        # of a bogus "fixup! ..." commit reaching git push.
        reset = await run("git", "reset", "--soft", pre_fixup_head, cwd=cwd)
        if not reset.success:
            log.error("step._commit_fold.unwind_failed", stderr=reset.stderr[:200])
    else:
        # No uncommitted fix to unwind to, and the surviving marker carries
        # real content that cannot be safely dropped or rewritten here:
        # leave it, but make the stranded state visible in the log so the
        # validate step's failure output can explain it.
        subjects = await _surviving_marker_subjects(cwd, base_branch)
        if subjects:
            log.error("step._commit_fold.marker_commit_stranded", subjects=subjects)
    return False


async def fold_fix_into_branch(cwd: Path | str | None, base_branch: str) -> bool:
    """Fold the hook-fix diff (and any self-reworded commit) into the branch.

    Fails open to ``False`` (nothing folded, caller proceeds to retry the
    hook with whatever state already exists) rather than raising, since this
    runs between a hook-fix LLM call and the hook retry and must never abort
    the validate step on a git/filesystem error.
    """
    try:
        return await _fold(cwd, base_branch)
    except OSError:
        log.warning("step._commit_fold.unavailable", exc_info=True)
        return False
