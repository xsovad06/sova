"""Step 1: Sync -- pull latest changes on the base branch.

Also fetches the task from the tracker so ctx.task is populated for
downstream steps (commit message, PR title) even when --force skips
the assess step.
"""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git import operations as git_ops
from sova.utils.gh import PushPermission, check_push_permission, get_active_gh_user
from sova.utils.logging import get_logger
from sova.utils.shell import check_git_identity

log = get_logger(component="step.sync")


class SyncStep(BaseStep):
    name = "sync"
    max_retries = 1

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        # Preflight: verify git identity is configured
        identity = await check_git_identity(cwd=ctx.project_dir)
        if not identity.valid:
            missing = ", ".join(identity.missing_fields)
            return StepResult(
                success=False,
                summary="Git identity not configured",
                error=(
                    f"Missing git config: {missing}. "
                    f"Set with: git config user.name 'Your Name' && git config user.email 'you@example.com'"
                ),
            )

        # Preflight: verify the active gh account can push to the repo (fail-open on unknown)
        if ctx.repo:
            check = await check_push_permission(ctx.repo, github_user=ctx.config.github_user, cwd=ctx.project_dir)
            if check.permission is PushPermission.DENIED:
                # check.checked_as is only set when resolve_gh_env actually resolved a
                # token for the configured user, i.e. the identity the API call really
                # ran under. If token resolution failed (or no user is configured), the
                # call ran under whatever gh account is ambient-active instead, so name
                # that account rather than blaming an identity that was never used.
                account = check.checked_as or await get_active_gh_user() or "(unknown account)"
                return StepResult(
                    success=False,
                    summary="GitHub account lacks push permission",
                    error=(
                        f"Account '{account}' does not have push access to {ctx.repo}. "
                        f"Switch to an account with push access: gh auth switch --user <account>"
                    ),
                )

        base = ctx.config.base_branch
        log.info("step.sync", base_branch=base)
        try:
            await git_ops.sync_branch(base, cwd=ctx.project_dir)
        except RuntimeError as exc:
            return StepResult(success=False, summary="Failed to sync", error=str(exc))

        if ctx.task is None and ctx.has_issue:
            try:
                ctx.task = await ctx.adapter.get_task(ctx.issue_number)
            except Exception:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
                log.warning("step.sync.task_fetch_failed", issue=ctx.issue_number, exc_info=True)

        return StepResult(success=True, summary=f"Synced {base}")

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)
