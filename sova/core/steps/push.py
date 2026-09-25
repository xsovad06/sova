"""Step 6: Push -- push branch to remote after validating."""

from __future__ import annotations

from sova.core.context import BUDGET_SKIP_HOOKS_THRESHOLD, ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git import operations as git_ops
from sova.git.operations import DivergenceCheck, DivergenceStatus
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.push")

_MAX_LOGGED_DROPPED_COMMITS = 20


def _capped_commit_list(dropped: list[str]) -> list[str]:
    """Cap a dropped-commit list before it reaches a log line or feed event.

    A significantly diverged branch can produce an unbounded commit list;
    this mirrors the truncation convention used elsewhere for log/telemetry
    payloads (e.g. agent output tail capping).
    """
    if len(dropped) <= _MAX_LOGGED_DROPPED_COMMITS:
        return dropped
    remaining = len(dropped) - _MAX_LOGGED_DROPPED_COMMITS
    return [*dropped[:_MAX_LOGGED_DROPPED_COMMITS], f"... and {remaining} more"]


class PushStep(BaseStep):
    name = "push"
    max_retries = 1

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.push", branch=ctx.branch_name, cwd=str(ctx.working_dir))

        if not ctx.branch_name:
            return StepResult(success=False, summary="Push failed", error="Cannot push: branch_name is empty")

        # Dollar budget only, not the composite resource_remaining_fraction:
        # wall-clock or LLM-call pressure alone must never disable the
        # pre-push hook, since that would push code that failed validation
        # for a reason unrelated to actual spend. Matches CommitStep's
        # no_verify threshold so both hook-skip decisions agree.
        no_verify = ctx.budget_remaining_fraction < BUDGET_SKIP_HOOKS_THRESHOLD
        if no_verify:
            log.warning("step.push.budget_skip_hooks", fraction=ctx.budget_remaining_fraction)

        # A rejected push is re-evaluated once with a freshly-read divergence
        # check rather than repeating the identical, doomed command, but
        # only when this attempt already intended to force (the original
        # decision was DIVERGED) AND the fresh lease still matches what the
        # first attempt observed. An unchanged lease means the rejection was
        # transient (e.g. a client/network hiccup unrelated to the lease
        # itself), and retrying with that same lease is safe. A lease that
        # has moved means a concurrent writer landed a real commit in the
        # race window between our check and our push: re-deriving force from
        # that new state would produce a lease trivially satisfied by the
        # very commit that caused the rejection, silently discarding it, so
        # that must surface as a clear error instead of a second attempt. A
        # plain push (ANCESTOR) that gets rejected is never retried into a
        # force for the same reason.
        error = ""
        first_lease: str | None = None
        for attempt in range(2):
            force, lease_sha, divergence = await self._resolve_force(ctx)
            if attempt == 0:
                first_lease = lease_sha
            elif lease_sha != first_lease:
                error = (
                    f"origin/{ctx.branch_name} moved during push retry "
                    f"({first_lease} -> {lease_sha}); refusing to force-push over a concurrent writer"
                )
                break
            try:
                await self._push(ctx, force=force, lease_sha=lease_sha, no_verify=no_verify)
            except RuntimeError as exc:
                error = str(exc)
                if attempt == 1 or not force or not git_ops.is_push_rejection(error):
                    break
                log.warning("step.push.rejected_retrying", branch=ctx.branch_name, error=error[:200])
                continue

            # Only announce dropped commits once the push has actually taken
            # effect: emitting on the decision instead of the outcome would
            # report discards for a push a concurrent writer just rejected.
            if force and divergence is not None:
                await self._log_dropped_commits(ctx, divergence)
            return StepResult(success=True, summary=f"Pushed {ctx.branch_name}")

        return StepResult(success=False, summary="Push failed", error=error)

    async def _resolve_force(self, ctx: ExecutionContext) -> tuple[bool, str | None, DivergenceCheck | None]:
        """Decide whether this push needs --force-with-lease.

        Only a confirmed divergence (local HEAD is not a descendant of
        origin's fetched tip) forces the push, and always with a lease SHA
        so a concurrent writer refuses the push rather than being silently
        overwritten. An absent remote ref or a failed check (unreachable
        git, shallow clone) both fall through to a plain push rather than
        guessing. Purely a decision: does not log or emit anything, since
        that must wait until the push this decision feeds has actually
        succeeded.
        """
        divergence = await git_ops.check_branch_divergence(ctx.branch_name, cwd=ctx.working_dir)
        if divergence.status != DivergenceStatus.DIVERGED:
            return False, None, None

        return True, divergence.remote_sha, divergence

    async def _push(self, ctx: ExecutionContext, *, force: bool, lease_sha: str | None, no_verify: bool) -> None:
        await git_ops.push(
            ctx.branch_name,
            force=force,
            lease_sha=lease_sha,
            set_upstream=True,
            cwd=ctx.working_dir,
            no_verify=no_verify,
            github_user=ctx.config.github_user,
        )

    async def _log_dropped_commits(self, ctx: ExecutionContext, divergence: DivergenceCheck) -> None:
        dropped: list[str] = []
        if divergence.remote_sha and divergence.head_sha:
            dropped = await git_ops.list_dropped_commits(
                divergence.head_sha, divergence.remote_sha, cwd=ctx.working_dir
            )

        log.warning(
            "step.push.force_with_lease",
            branch=ctx.branch_name,
            remote_sha=divergence.remote_sha,
            head_sha=divergence.head_sha,
            dropped_commits=_capped_commit_list(dropped),
        )

        from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe

        emit_safe(
            f"Force-pushing {ctx.branch_name}",
            severity=FeedEventSeverity.warning if dropped else FeedEventSeverity.info,
            detail=(
                f"{len(dropped)} remote commit(s) not reachable from local HEAD will be discarded: "
                + "; ".join(_capped_commit_list(dropped))
                if dropped
                else "Remote branch diverged from local HEAD; no commits are being discarded."
            ),
            category="agent",
            metadata={
                "issue": ctx.issue_number,
                "branch": ctx.branch_name,
                "remote_sha": divergence.remote_sha,
                "head_sha": divergence.head_sha,
                "dropped_commit_count": len(dropped),
            },
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: branch must have commits ahead of base."""
        result = await run("git", "rev-list", "--count", f"{ctx.base_branch}..HEAD", cwd=ctx.working_dir)
        if not result.success:
            return GateCheckResult(passed=False, reason="Failed to count commits ahead of base")

        try:
            count = int(result.stdout.strip() or "0")
        except ValueError:
            return GateCheckResult(passed=False, reason=f"Unexpected rev-list output: {result.stdout[:100]}")
        if count == 0:
            return GateCheckResult(passed=False, reason="No commits ahead of base branch")
        return GateCheckResult(passed=True)
