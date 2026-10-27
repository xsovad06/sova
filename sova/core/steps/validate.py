"""Step 7b: Validate. Run project pre-push hooks before pushing.

Discovers the project's pre-push hook (via core.hooksPath, .githooks/
auto-detect, or .git/hooks fallback) and runs it. If it fails, invokes
Claude to fix the issues and folds the fix into the branch commit it
belongs to (see sova.core.steps._commit_fold), then re-runs the hook.

Also checks for test regressions against the baseline snapshot captured
by CaptureBaselineStep (issue #233).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sova.core.agent_dispatch import dispatch_prompt
from sova.core.context import BUDGET_STOP_RETRY_THRESHOLD, ExecutionContext
from sova.core.steps._commit_fold import (
    _AUTOSQUASH_PREFIXES,
    _is_on_base,
    _resolve_base_ref,
    fold_fix_into_branch,
    has_unresolved_marker_commits,
)
from sova.core.steps.base import (
    BaseStep,
    GateCheckResult,
    StepResult,
    fix_attempt_routed_reason,
    resolve_fix_attempt_route,
)
from sova.core.steps.commit import is_agent_artifact
from sova.core.test_baseline import diff_results, load_baseline, run_test_suite
from sova.git.branch import get_current_branch
from sova.llm.errors import format_fix_llm_failure
from sova.utils.logging import get_logger
from sova.utils.shell import ShellResult, run, run_checked

log = get_logger(component="step.validate")

_HOOK_OUTPUT_LIMIT = 8000

_ZERO_SHA = "0" * 40


async def build_pre_push_stdin(cwd: Path | None, branch: str) -> str:
    """Build the ref line git feeds a pre-push hook on stdin.

    Git's contract is argv=(remote_name, remote_url) plus one
    '<local_ref> <local_sha> <remote_ref> <remote_sha>' line per pushed ref.
    Hooks routinely loop over these lines, so a hook run without them blocks
    on read until the step timeout kills it, with no output to diagnose.
    """
    if not branch:
        branch = await get_current_branch(cwd)
    ref = f"refs/heads/{branch}"

    # Not tolerated: a zero local_sha reads as a branch deletion, which makes
    # hooks skip every check and report success.
    head = await run_checked("git", "rev-parse", "HEAD", cwd=cwd)

    # A branch with no remote counterpart is normal; git sends zeros there.
    upstream = await run("git", "rev-parse", "--verify", "--quiet", f"origin/{branch}", cwd=cwd)
    remote_sha = upstream.stdout.strip() if upstream.success else _ZERO_SHA

    return f"{ref} {head.stdout.strip()} {ref} {remote_sha}\n"


def _truncate_hook_output(output: str) -> str:
    """Keep the tail of hook output, where actionable errors appear."""
    if len(output) <= _HOOK_OUTPUT_LIMIT:
        return output
    return "...(truncated)\n" + output[-_HOOK_OUTPUT_LIMIT:]


async def find_pre_push_hook(cwd: Path | str | None) -> str | None:
    """Locate the pre-push hook script for the project.

    Auto-configures core.hooksPath when .githooks/pre-push exists but the
    config is missing, preventing silent bypass of invariant checks.
    """
    result = await run("git", "config", "--get", "core.hooksPath", cwd=cwd)
    if result.success and result.stdout.strip():
        hooks_dir = result.stdout.strip()
    else:
        toplevel = await run("git", "rev-parse", "--show-toplevel", cwd=cwd)
        if toplevel.success:
            githooks_hook = Path(toplevel.stdout.strip()) / ".githooks" / "pre-push"
            if githooks_hook.exists():
                log.info("step.validate.auto_configure_hooks")
                config_result = await run("git", "config", "core.hooksPath", ".githooks", cwd=cwd)
                if not config_result.success:
                    log.warning("step.validate.auto_configure_failed", error=config_result.stderr)
                hooks_dir = ".githooks"
            else:
                git_dir = await run("git", "rev-parse", "--git-dir", cwd=cwd)
                if not git_dir.success:
                    return None
                hooks_dir = f"{git_dir.stdout.strip()}/hooks"
        else:
            return None

    hook_path = f"{hooks_dir}/pre-push"
    test_result = await run("test", "-x", hook_path, cwd=cwd)
    if test_result.success:
        return hook_path
    return None


class ValidateStep(BaseStep):
    name = "validate"
    TASK_TYPE = "validate"
    max_retries = 0
    requires_tools = True

    # Set True only once the hook-fix loop actually runs (the hook failed at
    # least once), read back in validate_output(): a fresh instance per
    # pipeline run (per get_developer_steps()/STEP_REGISTRY) so this never
    # leaks across runs. Gates the untracked-file check there, which exists
    # to catch a fix the loop left behind: an untracked build/test artifact
    # already sitting in the worktree because the hook simply passed on the
    # first try has nothing to do with that loop and must not fail the step.
    _fix_loop_ran: bool = False

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        cwd = ctx.working_dir
        self._fix_loop_ran = False
        hook = await find_pre_push_hook(cwd)

        if hook is None:
            log.info("step.validate.no_hook", cwd=str(cwd))
            return StepResult(success=True, summary="No pre-push hook found, skipping validation")

        log.info("step.validate.running_hook", hook=hook)

        hook_timeout = ctx.config.validation.hook_timeout
        hook_stdin = await build_pre_push_stdin(cwd, ctx.branch_name)
        result = await run(hook, "origin", "unused", cwd=cwd, timeout=hook_timeout, stdin=hook_stdin)
        if result.success:
            return StepResult(success=True, summary="Pre-push hook passed")

        if result.timed_out:
            log.error("step.validate.hook_timeout", timeout=hook_timeout)
            return StepResult(
                success=False,
                summary=f"Pre-push hook exceeded {hook_timeout}s and was killed",
                error="hook_timeout",
            )

        hook_output = (result.stdout + "\n" + result.stderr).strip()
        log.warning("step.validate.hook_failed", output=hook_output[:500])

        max_fix_attempts = ctx.config.validation.max_fix_attempts
        total_fix_cost = Decimal("0")
        for attempt in range(1, max_fix_attempts + 1):
            self._fix_loop_ran = True
            # Check budget before attempting fix
            if ctx.is_budget_exceeded:
                return StepResult(
                    success=False,
                    summary=f"Pre-push hook failed; budget exceeded after {attempt - 1} fix attempt(s)",
                    error="budget_exceeded",
                    cost_usd=total_fix_cost,
                )

            if ctx.resource_remaining_fraction < BUDGET_STOP_RETRY_THRESHOLD:
                log.warning(
                    "step.validate.budget_fraction_stop_retry",
                    attempt=attempt,
                    fraction=ctx.resource_remaining_fraction,
                )
                return StepResult(
                    success=True,
                    summary=(
                        f"Pre-push hook failed; stopping fix retries at "
                        f"{ctx.resource_remaining_fraction:.0%} budget remaining ({attempt - 1} attempt(s) made)"
                    ),
                    cost_usd=total_fix_cost,
                )

            log.info("step.validate.fix_attempt", attempt=attempt)

            prompt = (
                f"The project's pre-push hook failed with the following output:\n\n"
                f"```\n{_truncate_hook_output(hook_output)}\n```\n\n"
                f"If the problem is in file contents, fix ALL issues reported by the "
                f"hook and leave the changes uncommitted (do not run git commit); SOVA "
                f"folds the fix into the right commit automatically after this step. "
                f"If the problem is instead with an existing commit's message (e.g. a "
                f"conventional-commit format violation) rather than file contents, "
                f"reword that commit yourself: it must be one of the commits listed by "
                f"`git log origin/{ctx.base_branch}..HEAD` (never a commit already on "
                f"{ctx.base_branch}). Reword it with "
                f"`git commit --no-verify --fixup=reword:<sha> --allow-empty` using a "
                f"non-interactive $GIT_EDITOR that preserves the first 'amend! ...' "
                f"line unchanged and replaces everything after it with the corrected "
                f"message; do not run `git rebase` yourself, SOVA folds it in "
                f"automatically after this step. Do not modify or disable the hook "
                f"itself."
            )

            attempt_model, attempt_task_type = resolve_fix_attempt_route(ctx, self.TASK_TYPE, attempt)

            pre_dispatch_head = await run("git", "rev-parse", "HEAD", cwd=cwd)

            try:
                llm_result = await dispatch_prompt(
                    self,
                    prompt,
                    model=attempt_model,
                    task_type=attempt_task_type,
                    cwd=ctx.working_dir,
                    max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
                    timeout=ctx.config.validation.fix_timeout,
                    ctx=ctx,
                )
                ctx.add_usage(
                    llm_result, routed_reason=fix_attempt_routed_reason(llm_result, self.TASK_TYPE, attempt_task_type)
                )
                total_fix_cost += llm_result.cost_usd
            except RuntimeError as exc:
                log.error("step.validate.llm_failed", error=str(exc))
                return StepResult(
                    success=False,
                    summary=f"Pre-push hook failed and auto-fix failed: {exc}",
                    error=format_fix_llm_failure(exc, attempt),
                    cost_usd=total_fix_cost,
                )

            await self._unwind_ordinary_commits(cwd, pre_dispatch_head, attempt, ctx.base_branch)

            folded = await fold_fix_into_branch(cwd, ctx.base_branch)
            log.info("step.validate.fold_result", attempt=attempt, folded=folded)

            if not folded and await has_unresolved_marker_commits(cwd, ctx.base_branch):
                log.error("step.validate.marker_commit_stranded", attempt=attempt)
                return StepResult(
                    success=False,
                    summary=(
                        f"Pre-push hook fix could not be folded after {attempt} attempt(s): a "
                        f"fixup/squash/amend marker commit remains on the branch"
                    ),
                    error="marker_commit_stranded",
                    cost_usd=total_fix_cost,
                )

            retry_stdin = await build_pre_push_stdin(cwd, ctx.branch_name)
            retry = await run(hook, "origin", "unused", cwd=cwd, timeout=hook_timeout, stdin=retry_stdin)
            if retry.success:
                if not folded and await self._worktree_is_dirty(cwd):
                    # The hook passed because it reads the worktree, not the
                    # commit range, but the fix was never folded in and is
                    # still sitting there uncommitted: a "passed" summary
                    # would hide exactly the failure validate_output()'s own
                    # diff/untracked checks are about to catch anyway.
                    log.error("step.validate.fold_failed", attempt=attempt)
                    return StepResult(
                        success=False,
                        summary=(
                            f"Pre-push hook passed after {attempt} fix attempt(s), but the fix "
                            f"could not be folded into the branch"
                        ),
                        error="fold_failed",
                        cost_usd=total_fix_cost,
                    )
                return StepResult(
                    success=True,
                    summary=f"Pre-push hook passed after {attempt} fix attempt(s)",
                    cost_usd=total_fix_cost,
                )

            if retry.timed_out:
                log.error("step.validate.hook_timeout", timeout=hook_timeout, attempt=attempt)
                return StepResult(
                    success=False,
                    summary=f"Pre-push hook exceeded {hook_timeout}s and was killed",
                    error="hook_timeout",
                    cost_usd=total_fix_cost,
                )

            hook_output = (retry.stdout + "\n" + retry.stderr).strip()
            log.warning("step.validate.still_failing", attempt=attempt, output=hook_output[:500])

        return StepResult(
            success=False,
            summary=f"Pre-push hook still failing after {max_fix_attempts} fix attempts",
            error=hook_output[:1000],
            cost_usd=total_fix_cost,
        )

    @staticmethod
    async def _unwind_ordinary_commits(
        cwd: Path | None, pre_dispatch_head: ShellResult, attempt: int, base_branch: str
    ) -> None:
        """Turn an ordinary commit the fix agent made itself back into a staged diff.

        The fix prompt asks the agent to leave file-content fixes uncommitted
        (SOVA folds them in afterward) and to use ``git commit
        --fixup=reword:<sha>`` for a commit-message fix. An agent that
        ignores this and runs a plain ``git commit`` anyway produces a
        commit :func:`fold_fix_into_branch` never looks at
        (``_changed_paths()`` sees a clean tree, and the commit carries no
        ``fixup!``/``squash!``/``amend!`` prefix for autosquash to find), so
        it would survive on the branch with a non-conventional subject and
        no reported violation. Detected deterministically rather than relying
        on the prompt alone: if HEAD moved since *pre_dispatch_head* and the
        new commit(s) carry no autosquash marker, ``git reset --soft`` turns
        them back into the staged diff the rest of the fold pipeline expects.

        Only unwinds a commit that was *added on top* of ``pre_dispatch_head``
        (verified by ancestry below), never one that *replaced* it: ``git
        commit --amend``/``git rebase -i`` is the far more natural way an
        agent rewords a commit, and after either, ``pre_dispatch_head`` is no
        longer an ancestor of the new HEAD. Resetting in that case would
        revert the very reword the fix was supposed to make, so instead the
        rewritten history is left alone for ``fold_fix_into_branch()``/the
        hook retry to judge on its own merits.
        """
        if not pre_dispatch_head.success:
            return
        pre_sha = pre_dispatch_head.stdout.strip()

        post_dispatch_head = await run("git", "rev-parse", "HEAD", cwd=cwd)
        if not post_dispatch_head.success or post_dispatch_head.stdout.strip() == pre_sha:
            return  # Nothing committed: the agent followed the prompt.

        ancestry = await run("git", "merge-base", "--is-ancestor", pre_sha, "HEAD", cwd=cwd)
        if not ancestry.success:
            # Either pre_dispatch_head is no longer reachable (history was
            # rewritten, not appended to) or the check itself could not run.
            # Either way, resetting risks discarding a legitimate reword, so
            # leave history untouched.
            log.info("step.validate.commit_rewritten", attempt=attempt, ancestry_exit=ancestry.returncode)
            new_head = post_dispatch_head.stdout.strip()
            base_ref = await _resolve_base_ref(cwd, base_branch)
            if base_ref is not None and await _is_on_base(cwd, new_head, base_ref):
                log.error("step.validate.rewrite_landed_on_base", attempt=attempt)
            return

        subjects = await run("git", "log", "--format=%s", f"{pre_sha}..HEAD", cwd=cwd)
        if not subjects.success:
            return
        if any(s.startswith(_AUTOSQUASH_PREFIXES) for s in subjects.stdout.splitlines()):
            return  # A deliberate reword marker: fold_fix_into_branch handles it.

        log.warning("step.validate.ordinary_commit_unwound", attempt=attempt)
        await run("git", "reset", "--soft", pre_sha, cwd=cwd)

    @staticmethod
    async def _worktree_is_dirty(cwd: Path | None) -> bool:
        """Return True when a staged, unstaged, or untracked (non-artifact) change remains.

        Fails open (treated as clean) on an unresolvable ``git status`` or a
        git/filesystem error severe enough to raise, matching
        :func:`has_unresolved_marker_commits`'s own fail-open contract:
        leaves the step to report success exactly as it did before this
        check existed, rather than a new check's own failure to run turning
        an otherwise-passing attempt into a reported one.
        """
        try:
            status = await run("git", "status", "--porcelain", "--untracked-files=all", cwd=cwd)
        except OSError:
            log.warning("step.validate.worktree_status_unavailable", exc_info=True)
            return False
        if not status.success:
            return False
        for line in status.stdout.splitlines():
            if not line:
                continue
            if line.startswith("??") and is_agent_artifact(line[3:]):
                continue
            return True
        return False

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Structural gate: commits must still exist after validation.

        Checks commits ahead of base, plus unstaged, staged, and (only when
        the hook-fix loop actually ran) untracked changes to catch anything
        that loop left behind: the fix agent is told to leave its fix
        uncommitted and let ``fold_fix_into_branch()`` fold it in, and if
        that fold aborts (a suspicious staged file, a failed ``git add
        -A``/fixup commit, an unresolvable merge-base) a fix that consisted
        only of a *new* file is restored to untracked, not staged, which the
        diff-only checks below never see. The untracked check is skipped
        entirely when the hook simply passed without ever needing a fix: an
        untracked build/test artifact already sitting in the worktree for
        unrelated reasons has nothing to do with the hook-fix loop and must
        not fail the whole step over it. Regression checking is performed
        separately in verify_output() to allow longer timeouts for test
        suite execution.
        """
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        if not (log_result.success and log_result.stdout.strip()):
            return GateCheckResult(passed=False, reason="No commits ahead of base after validation")

        unstaged = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        if unstaged.success and unstaged.stdout.strip():
            detail = unstaged.stdout.strip()[:200]
            return GateCheckResult(passed=False, reason=f"Unstaged changes after validation: {detail}")

        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        if staged.success and staged.stdout.strip():
            detail = staged.stdout.strip()[:200]
            return GateCheckResult(passed=False, reason=f"Staged but uncommitted changes after validation: {detail}")

        if not self._fix_loop_ran:
            return GateCheckResult(passed=True)

        untracked = await run("git", "status", "--porcelain", "--untracked-files=all", cwd=ctx.working_dir)
        if untracked.success:
            new_paths = [
                line[3:]
                for line in untracked.stdout.splitlines()
                if line.startswith("??") and not is_agent_artifact(line[3:])
            ]
            if new_paths:
                detail = ", ".join(new_paths[:10])
                return GateCheckResult(passed=False, reason=f"Untracked changes after validation: {detail}")

        return GateCheckResult(passed=True)

    async def verify_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Heavyweight verification: run test suite and check for regressions."""
        regression_check = await self._check_regressions(ctx)
        if regression_check is not None:
            return regression_check

        return GateCheckResult(passed=True)

    async def _check_regressions(self, ctx: ExecutionContext) -> GateCheckResult | None:
        """Check for test regressions against baseline. Returns None if no issues."""
        if ctx.test_baseline_path is None:
            return None

        # Use worktree_dir (where CaptureBaselineStep saves the baseline),
        # falling back to working_dir for consistency.
        test_dir = ctx.worktree_dir or ctx.working_dir
        baseline = load_baseline(test_dir)
        if baseline is None:
            return None

        test_cmd = ctx.config.test_cmd
        if not test_cmd or not test_cmd.strip():
            return None

        try:
            current = await run_test_suite(
                test_cmd=test_cmd,
                cwd=test_dir,
                cmd_timeout=ctx.config.testing.baseline_timeout,
            )
        except Exception:  # noqa: BLE001 (regression check is advisory; validation proceeds without a baseline diff)
            log.warning("step.validate.regression_check_failed", exc_info=True)
            return None

        report = diff_results(baseline, current)
        if report.has_regressions:
            names = [r.nodeid for r in report.regressions[:10]]
            log.error(
                "step.validate.regressions_detected",
                count=len(report.regressions),
                tests=names,
            )
            return GateCheckResult(
                passed=False,
                reason=f"Test regressions detected: {report.summary()}. First: {', '.join(names[:3])}",
            )

        if report.fixed:
            log.info("step.validate.tests_fixed", count=len(report.fixed))

        return None
