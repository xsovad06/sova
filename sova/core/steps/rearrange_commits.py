"""Step: Rearrange commits. Reorganize branch history into clean, logical commits.

For Claude, invokes /rearrange-commits in the working directory so review fixes
are folded into logical commits. For Codex, SOVA amends the PR tip from its host
process because the coding agent cannot write shared Git metadata.

Used in the address-review pipeline in place of CommitStep.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from sova.core.agent_dispatch import dispatch_command
from sova.core.context import ExecutionContext
from sova.core.steps._commit_fold import _changed_paths, _head_sha, _resolve_base_ref
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.core.steps.commit import is_agent_artifact
from sova.git.artifact_exclusions import EXCLUDED_PATHSPECS as _EXCLUDED_PATHSPECS
from sova.git.artifact_exclusions import IGNORABLE_UNTRACKED_RE as _IGNORABLE_UNTRACKED_RE
from sova.git.safety import unstage_suspicious_files
from sova.utils.env import scrub_check_env
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.rearrange_commits")

# (HEAD sha, symbolic ref) of the worktree; the ref is None on a detached HEAD.
_Position = tuple[str, str | None]

# Git state files that mean an operation is still running, with the command that aborts it. Order matters:
# a `git am` session also uses rebase-apply, which `rebase --abort` refuses to abort, so it is checked first.
_IN_PROGRESS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rebase-apply/applying", ("am", "--abort")),
    ("rebase-merge", ("rebase", "--abort")),
    ("rebase-apply", ("rebase", "--abort")),
    ("MERGE_HEAD", ("merge", "--abort")),
    ("CHERRY_PICK_HEAD", ("cherry-pick", "--abort")),
    ("REVERT_HEAD", ("revert", "--abort")),
    ("sequencer", ("cherry-pick", "--abort")),
)

# Recovery refs live under this prefix, one pair per restore (`<prefix>/<tag>/attempt` and `/edits`). The tag
# names the worktree, the start commit and the moment, because refs are shared by every linked worktree of a
# repository: a fixed name would let a second worktree's restore overwrite the first one's recovery target.
_RECOVERY_REF_PREFIX = "refs/sova/rearrange"

_RESTORE_FAILED = "step.rearrange_commits.restore_failed"


@dataclass(frozen=True)
class _Restore:
    """Outcome of `_restore_head`: *restored* is True when restored, None when nothing needed it, False on failure.

    *recovery* is the shell command that finishes a failed restore by hand, and *saved* names the recovery
    refs a restore wrote (the agent's HEAD, the tracked-file edits an abort would have discarded).
    """

    restored: bool | None
    recovery: str = ""
    saved: tuple[str, ...] = ()


class RearrangeCommitsStep(BaseStep):
    name = "rearrange_commits"
    TASK_TYPE = "rearrange_commits"
    requires_tools = True

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.rearrange_commits", branch=ctx.branch_name, base=ctx.base_branch)

        if ctx.config.agent.runtime == "codex":
            return await self._amend_from_host(ctx)

        cwd = ctx.working_dir
        base = ctx.base_branch
        start = await self._position(cwd)
        if start is None:
            # Without a known commit and branch there is nothing to restore to, so never start the agent.
            return StepResult(
                success=False,
                summary="Could not read the worktree's HEAD or branch before rearranging; nothing was changed",
                error="unreadable worktree position",
            )
        try:
            running = await self._operations_in_progress(cwd)
        except OSError as exc:
            return StepResult(
                success=False, summary="Could not inspect the worktree before rearranging", error=str(exc)
            )
        if running:
            # An operation the operator (or an earlier step) left running is not this step's to abort, and the
            # restore would otherwise discard its conflict state. Leave it in place and stop.
            names = ", ".join(marker for marker, _ in running)
            return StepResult(
                success=False,
                summary=f"A git operation is already in progress ({names}); finish or abort it, then retry",
                error="git operation in progress before rearranging",
            )
        try:
            return await self._dispatch_and_check(ctx, cwd, start)
        except asyncio.CancelledError:
            # The engine's hard step timeout and SIGTERM both cancel the step, during the agent or during the
            # output checks after it. Restore first, or the engine's partial-work commit lands on a branch the
            # command left reset to its base. A branch that is intact (the rearrangement finished) is kept.
            restore = await self._restore_uninterrupted(cwd, start, base)
            if restore.restored is False:
                # The engine would commit the whole PR onto the branch that is still reset. Tell it not to.
                ctx.partial_work_blocked = self._failure_summary("Rearrange restore failed", restore)
                log.error("step.rearrange_commits.cancelled_unrestored", detail=ctx.partial_work_blocked)
            raise

    async def _dispatch_and_check(self, ctx: ExecutionContext, cwd: Path, start: _Position) -> StepResult:
        """Run /rearrange-commits, then fail the attempt from the pre-step state when it left the branch broken."""
        base = ctx.base_branch
        try:
            result = await dispatch_command(
                self,
                "/rearrange-commits",
                model=ctx.resolved_model or ctx.config.agent.model,
                fallback_model=ctx.get_cli_fallback_model(),
                task_type=ctx.routing_task_type(self.TASK_TYPE),
                cwd=cwd,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
                timeout=ctx.config.agent.step_timeout,
                ctx=ctx,
            )
        except RuntimeError as exc:
            restore = await self._restore_head(cwd, start, base)
            return StepResult(
                success=False,
                summary=self._failure_summary("Commit reorganization failed", restore),
                error=str(exc),
            )
        ctx.add_usage(result)

        # An agent can stop after the reset and still exit 0. When it moved HEAD and the gate would
        # fail, fail the attempt from the pre-step state rather than leaving the branch reset.
        if await self._position(cwd) != start:
            gate = await self.validate_output(ctx)
            if not gate.passed:
                restore = await self._restore_head(cwd, start, base)
                return StepResult(
                    success=False,
                    summary=self._failure_summary("Commit reorganization incomplete", restore),
                    error=gate.reason,
                    cost_usd=result.cost_usd,
                )
        return StepResult(
            success=True,
            summary="Commits reorganized into clean logical units",
            cost_usd=result.cost_usd,
        )

    @staticmethod
    def _failure_summary(summary: str, restore: _Restore) -> str:
        saved = f" (recovery refs: {', '.join(restore.saved)})" if restore.saved else ""
        if restore.restored is True:
            return f"{summary}; branch restored to its pre-rearrange commit{saved}"
        if restore.restored is False:
            # The command names the worktree and re-attaches the branch before resetting: run in the primary
            # checkout, a bare reset would move main, and on the backup branch it would move the safety ref.
            return f"{summary}; branch left moved, restore it with: {restore.recovery}{saved}"
        return summary

    @staticmethod
    def _recovery_command(
        cwd: Path, start: _Position, abort: tuple[str, ...] | None = None, edits_unsaved: bool = False
    ) -> str:
        """The shell command that puts the worktree's branch back at *start*, safe to run from anywhere.

        Every argument goes through ``shlex.join``, so a worktree path with spaces or a ref name holding
        ``$`` or ``;`` reaches git literally. *edits_unsaved* means the abort was refused because the
        worktree's tracked edits could not be saved: the operator copies them aside before running it.
        """
        start_head, start_ref = start
        steps: list[tuple[str, ...]] = [abort] if abort else []
        if start_ref is not None:
            steps += [("update-ref", start_ref, start_head), ("symbolic-ref", "HEAD", start_ref)]
        else:
            steps.append(("update-ref", "--no-deref", "HEAD", start_head))
        steps.append(("reset", "-q", start_head))
        command = " && ".join(shlex.join(["git", "-C", str(cwd), *step]) for step in steps)
        if edits_unsaved:
            where = shlex.quote(str(cwd))
            return f"first copy your tracked-file edits in {where} aside (they could not be saved), then {command}"
        return command

    @staticmethod
    async def _head(cwd: Path) -> str | None:
        """Return the worktree's HEAD sha, or None when it cannot be read (a missing worktree raises OSError)."""
        try:
            return await _head_sha(cwd) or None
        except OSError:
            return None

    @classmethod
    async def _position(cls, cwd: Path) -> _Position | None:
        """Return ``(HEAD sha, symbolic ref)``, or None when either cannot be told.

        ``symbolic-ref -q`` exits 1 for a detached HEAD and anything else for a real failure (a timeout, a
        locked repository): only the first is a position, so a failure is never recorded as "detached".
        """
        head = await cls._head(cwd)
        if head is None:
            return None
        try:
            ref = await run("git", "symbolic-ref", "-q", "HEAD", cwd=cwd)
        except OSError:
            return None
        if ref.success:
            return head, ref.stdout.strip() or None
        return (head, None) if ref.returncode == 1 else None

    @staticmethod
    async def _operations_in_progress(cwd: Path) -> list[tuple[str, tuple[str, ...]]]:
        """The am, rebase, merge, cherry-pick or revert sessions running in the worktree, with their abort command."""
        running: list[tuple[str, tuple[str, ...]]] = []
        for marker, command in _IN_PROGRESS:
            path = await run("git", "rev-parse", "--git-path", marker, cwd=cwd)
            if not path.success:
                continue
            target = Path(path.stdout.strip())
            if (target if target.is_absolute() else cwd / target).exists():
                running.append((marker, command))
        return running

    @classmethod
    async def _abort_in_progress(
        cls, cwd: Path, edits_ref: str = f"{_RECOVERY_REF_PREFIX}/edits"
    ) -> tuple[bool | None, tuple[str, ...] | None, bool]:
        """Abort an am, rebase, merge, cherry-pick or revert the agent left running.

        Returns ``(aborted, failed_command, edits_unsaved)``: aborted is None when nothing was running and
        False on a failure, failed_command is the abort that failed or was refused, and edits_unsaved is
        True when the abort was refused because the tracked edits it would discard could not be saved to
        *edits_ref* (an abort that cannot be undone never runs without a copy of what it resets).
        """
        running = await cls._operations_in_progress(cwd)
        if not running:
            return None, None, False
        if not await cls._save_operation_edits(cwd, edits_ref):
            return False, running[0][1], True
        for marker, command in running:
            abort = await run("git", *command, cwd=cwd)
            if not abort.success:
                log.warning("step.rearrange_commits.abort_failed", operation=marker, stderr=abort.stderr[:500])
                return False, command, False
        return True, None, False

    @staticmethod
    async def _is_broken(cwd: Path, current: _Position, start: _Position, base_branch: str) -> bool:
        """True when the branch needs restoring: off its recorded ref, or at or behind the base.

        A branch that still carries commits ahead of the base is the agent's rearranged history (or the
        original one), so a gate failure there is left for the retry rather than thrown away. A base that
        cannot be resolved counts as broken, since nothing proves the history is intact.
        """
        if current[1] != start[1]:
            return True
        base_ref = await _resolve_base_ref(cwd, base_branch)
        if base_ref is None:
            return True
        ancestor = await run("git", "merge-base", "--is-ancestor", "HEAD", base_ref, cwd=cwd)
        return ancestor.returncode != 1

    @classmethod
    async def _restore_uninterrupted(cls, cwd: Path, start: _Position, base_branch: str) -> _Restore:
        """Run `_restore_head` to completion from a cancelled step, whatever else cancels or raises meanwhile.

        ``asyncio.shield`` only protects the inner task: a second cancel (SIGTERM after the hard timeout)
        raises at the await while the restore keeps running detached, racing the engine's partial-work
        staging. So keep waiting for the task and swallow the repeat cancels; the caller re-raises the original.
        """
        task = asyncio.ensure_future(cls._restore_head(cwd, start, base_branch))
        while not task.done():
            try:
                await asyncio.shield(task)
            # A repeat cancel is swallowed on purpose: the caller re-raises the original cancel.
            except asyncio.CancelledError:  # NOSONAR(S7497)
                continue
            except Exception:  # noqa: BLE001 (the task's failure is read below; the cancel must still propagate)
                break
        if task.cancelled() or task.exception() is not None:
            log.error(_RESTORE_FAILED, start_head=start[0], error="restore task failed")
            return _Restore(False, cls._recovery_command(cwd, start))
        return task.result()

    @classmethod
    async def _restore_head(cls, cwd: Path, start: _Position, base_branch: str) -> _Restore:
        """Move the branch back to where it was before /rearrange-commits died part way.

        The command commits a WIP snapshot and soft-resets to the fork point before
        recommitting, so an agent that dies in between (a network error, a timeout, a
        cancellation) leaves the branch on a base commit with all the PR's work
        uncommitted, and every later run sees a branch that diverged from its remote.
        A rebase, merge, cherry-pick, revert or am session the agent left running is aborted first:
        that resets tracked files to the commit the operation started from (the WIP snapshot), so an
        edit made mid-operation is first saved to ``<_RECOVERY_REF_PREFIX>/<tag>/edits``, and the abort is
        refused when that save fails. A branch that
        is intact (no operation was running, still on its ref, with commits ahead of the base) is left
        alone, since the rearranged history is worth keeping. Otherwise the agent's HEAD is saved to
        ``<_RECOVERY_REF_PREFIX>/<tag>/attempt``, and when HEAD is no longer on the recorded branch (the agent
        checked out the backup branch, or detached) that branch is moved back and re-attached explicitly,
        so the reset never moves the backup branch. Only the abort touches working tree files;
        re-attaching and the final mixed reset leave them alone, and the backup branch is kept.

        Fails closed: an OSError from git (an unusable worktree) is a failed restore, never raised.
        """
        try:
            return await cls._restore_head_unguarded(cwd, start, base_branch)
        except OSError as exc:
            log.error(_RESTORE_FAILED, start_head=start[0], error=str(exc))
            return _Restore(False, cls._recovery_command(cwd, start))

    @staticmethod
    async def _save_operation_edits(cwd: Path, edits_ref: str) -> bool:
        """Save tracked-file edits to *edits_ref* before an abort hard-resets them.

        An agent that edited a file after an interactive rebase stopped (splitting a commit, say) would lose
        that edit to `rebase --abort`. The snapshot is built in a throwaway copy of the index (`add -u`, then
        `write-tree` and `commit-tree`), so it touches neither the real index nor the stash stack every
        worktree shares, and it works while the index holds unresolved conflicts, where `git stash create`
        refuses. Returns True when the edits are saved or there are none, False when saving failed.
        """
        index = await run("git", "rev-parse", "--git-path", "index", cwd=cwd)
        head_tree = await run("git", "rev-parse", "HEAD^{tree}", cwd=cwd)
        if not (index.success and head_tree.success):
            log.warning("step.rearrange_commits.edits_save_failed", stderr=(index.stderr or head_tree.stderr)[:500])
            return False
        source = Path(index.stdout.strip())
        with tempfile.TemporaryDirectory(prefix="sova-rearrange-") as scratch:
            scratch_index = Path(scratch) / "index"
            try:
                shutil.copyfile(source if source.is_absolute() else cwd / source, scratch_index)
            except OSError as exc:
                log.warning("step.rearrange_commits.edits_save_failed", error=str(exc))
                return False
            # Identity and signing are set here only: a worktree with no user.name must still save the edits.
            env = {
                **os.environ,
                "GIT_INDEX_FILE": str(scratch_index),
                "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME", "sova"),
                "GIT_AUTHOR_EMAIL": os.environ.get("GIT_AUTHOR_EMAIL", "sova@localhost"),
                "GIT_COMMITTER_NAME": os.environ.get("GIT_COMMITTER_NAME", "sova"),
                "GIT_COMMITTER_EMAIL": os.environ.get("GIT_COMMITTER_EMAIL", "sova@localhost"),
            }
            staged = await run("git", "add", "-u", cwd=cwd, env=env)
            tree = await run("git", "write-tree", cwd=cwd, env=env) if staged.success else staged
            if not tree.success:
                log.warning("step.rearrange_commits.edits_save_failed", stderr=tree.stderr[:500])
                return False
            if tree.stdout.strip() == head_tree.stdout.strip():
                return True
            commit = await run(
                "git",
                "-c",
                "commit.gpgsign=false",
                "commit-tree",
                tree.stdout.strip(),
                "-p",
                "HEAD",
                "-m",
                "rearrange-commits: edits before abort",
                cwd=cwd,
                env=env,
            )
        saved = await run("git", "update-ref", edits_ref, commit.stdout.strip(), cwd=cwd) if commit.success else commit
        if not saved.success:
            log.warning("step.rearrange_commits.edits_save_failed", stderr=saved.stderr[:500])
            return False
        log.info("step.rearrange_commits.edits_saved", ref=edits_ref, commit=commit.stdout.strip())
        return True

    @staticmethod
    async def _save_attempt(cwd: Path, head: str, attempt_ref: str) -> bool:
        """Save the agent's HEAD to *attempt_ref* so a rearrangement that did finish stays recoverable."""
        saved = await run("git", "update-ref", attempt_ref, head, cwd=cwd)
        if saved.success:
            log.info("step.rearrange_commits.attempt_saved", ref=attempt_ref, head=head)
        else:
            log.warning("step.rearrange_commits.attempt_save_failed", head=head, stderr=saved.stderr[:500])
        return saved.success

    @staticmethod
    def _recovery_refs(cwd: Path, start_head: str) -> tuple[str, str]:
        """The ``(attempt, edits)`` ref names for one restore, unique to the worktree, start commit and moment."""
        worktree = hashlib.sha1(str(cwd).encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        base = f"{_RECOVERY_REF_PREFIX}/{worktree}-{start_head[:12]}-{time.time_ns()}"
        return f"{base}/attempt", f"{base}/edits"

    @staticmethod
    def _restore_steps(start: _Position, current_ref: str | None) -> list[tuple[str, ...]]:
        """The git commands that put HEAD back on *start*, re-attaching its branch first when HEAD left it."""
        start_head, start_ref = start
        steps: list[tuple[str, ...]] = []
        if current_ref != start_ref:
            if start_ref is not None:
                steps += [("update-ref", start_ref, start_head), ("symbolic-ref", "HEAD", start_ref)]
            else:
                steps.append(("update-ref", "--no-deref", "HEAD", start_head))
        steps.append(("reset", "-q", start_head))
        return steps

    @classmethod
    async def _restore_head_unguarded(cls, cwd: Path, start: _Position, base_branch: str) -> _Restore:
        start_head, _ = start
        attempt_ref, edits_ref = cls._recovery_refs(cwd, start_head)
        aborted, failed_abort, edits_unsaved = await cls._abort_in_progress(cwd, edits_ref)
        if aborted is False:
            return _Restore(False, cls._recovery_command(cwd, start, failed_abort, edits_unsaved))
        current = await cls._position(cwd)
        if current is None:
            log.warning(_RESTORE_FAILED, start_head=start_head, error="position unreadable after the agent stopped")
            return _Restore(False, cls._recovery_command(cwd, start))
        # An abort that had edits to save wrote edits_ref; one with none to save left no ref behind.
        saved = (edits_ref,) if aborted and await cls._ref_exists(cwd, edits_ref) else ()
        if current == start:
            return _Restore(True if aborted else None, saved=saved)
        current_head, current_ref = current
        if not aborted and not await cls._is_broken(cwd, current, start, base_branch):
            log.info("step.rearrange_commits.restore_skipped", head=current_head, start_head=start_head)
            return _Restore(None)
        if await cls._save_attempt(cwd, current_head, attempt_ref):
            saved = (attempt_ref, *saved)
        steps = cls._restore_steps(start, current_ref)
        for step in steps:
            outcome = await run("git", *step, cwd=cwd)
            if not outcome.success:
                log.warning(
                    _RESTORE_FAILED,
                    start_head=start_head,
                    command=step[0],
                    stderr=outcome.stderr[:500],
                )
                return _Restore(False, cls._recovery_command(cwd, start), saved)
        log.info("step.rearrange_commits.restored", start_head=start_head, moved_from=current_head, refs=saved)
        return _Restore(True, saved=saved)

    @staticmethod
    async def _ref_exists(cwd: Path, ref: str) -> bool:
        return (await run("git", "rev-parse", "--verify", "-q", ref, cwd=cwd)).success

    @staticmethod
    async def _amend_from_host(ctx: ExecutionContext) -> StepResult:
        """Fold Codex's file edits into the PR tip without giving it Git metadata access."""
        cwd = ctx.working_dir
        status = await run("git", "status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=cwd)
        if not status.success:
            return StepResult(success=False, summary="Could not inspect worktree", error=status.stderr)

        changed = [path for path in await _changed_paths(cwd) if not is_agent_artifact(path)]
        if not changed:
            return StepResult(success=True, summary="No file changes to fold into the PR")

        base_ref = await _resolve_base_ref(cwd, ctx.base_branch)
        if base_ref is None:
            return StepResult(success=False, summary="Could not resolve PR base", error=ctx.base_branch)
        on_base = await run("git", "merge-base", "--is-ancestor", "HEAD", base_ref, cwd=cwd)
        if on_base.returncode != 1:
            return StepResult(success=False, summary="Refusing to amend a base commit", error=on_base.stderr)

        staged = await run("git", "diff", "--cached", "--name-only", "-z", cwd=cwd)
        if not staged.success or any(is_agent_artifact(path) for path in staged.stdout.split("\0") if path):
            return StepResult(success=False, summary="Unexpected staged files", error=staged.stderr)

        added = await run("git", "add", "--", *changed, cwd=cwd)
        if not added.success:
            return StepResult(success=False, summary="Could not stage review fixes", error=added.stderr)
        scan = await unstage_suspicious_files(cwd)
        if not scan.checked or scan.unstaged or scan.still_staged:
            return StepResult(success=False, summary="Unsafe files found in staged changes")

        amended = await run(
            "git", "commit", "--amend", "--no-edit", cwd=cwd, env=scrub_check_env(config=ctx.config, quiet=True)
        )
        if not amended.success:
            return StepResult(success=False, summary="Could not amend PR commit", error=amended.stderr)
        return StepResult(success=True, summary="Review fixes folded into the PR tip")

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: branch must have at least one commit ahead of base with no uncommitted changes."""
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_commits = bool(log_result.success and log_result.stdout.strip())
        if not has_commits:
            return GateCheckResult(passed=False, reason="No commits ahead of base after rearranging")

        diff_result = await run("git", "diff", "--stat", "HEAD", "--", ".", *_EXCLUDED_PATHSPECS, cwd=ctx.working_dir)
        staged = await run("git", "diff", "--cached", "--stat", "--", ".", *_EXCLUDED_PATHSPECS, cwd=ctx.working_dir)
        has_uncommitted = bool(
            (diff_result.success and diff_result.stdout.strip()) or (staged.success and staged.stdout.strip())
        )
        if has_uncommitted:
            return GateCheckResult(passed=False, reason="Uncommitted changes remain after rearranging")

        # --untracked-files=all forces one line per untracked file rather than
        # collapsing a wholly-untracked directory (e.g. a fresh `.agents/skills/`
        # mirror in a project that doesn't track it) into a single parent-level
        # line that _IGNORABLE_UNTRACKED_RE's per-directory prefixes can't match.
        status_result = await run("git", "status", "--porcelain", "--untracked-files=all", cwd=ctx.working_dir)
        if not status_result.success:
            return GateCheckResult(passed=False, reason="git status failed")
        untracked_lines = [
            line[3:].strip()
            for line in status_result.stdout.splitlines()
            if line.startswith("??") and not _IGNORABLE_UNTRACKED_RE.search(line[3:].strip())
        ]
        if untracked_lines:
            return GateCheckResult(
                passed=False,
                reason=f"Untracked files remain after rearranging: {', '.join(untracked_lines[:5])}",
            )

        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps
