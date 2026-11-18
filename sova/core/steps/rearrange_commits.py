"""Step: Rearrange commits. Reorganize branch history into clean, logical commits.

For Claude, invokes /rearrange-commits in the working directory so review fixes
are folded into logical commits. For Codex, SOVA amends the PR tip from its host
process because the coding agent cannot write shared Git metadata.

Used in the address-review pipeline in place of CommitStep.
"""

from __future__ import annotations

from sova.core.agent_dispatch import dispatch_command
from sova.core.context import ExecutionContext
from sova.core.steps._commit_fold import _changed_paths, _resolve_base_ref
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.core.steps.commit import is_agent_artifact
from sova.git.artifact_exclusions import EXCLUDED_PATHSPECS as _EXCLUDED_PATHSPECS
from sova.git.artifact_exclusions import IGNORABLE_UNTRACKED_RE as _IGNORABLE_UNTRACKED_RE
from sova.git.safety import unstage_suspicious_files
from sova.utils.env import scrub_check_env
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.rearrange_commits")


class RearrangeCommitsStep(BaseStep):
    name = "rearrange_commits"
    TASK_TYPE = "rearrange_commits"
    requires_tools = True

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.rearrange_commits", branch=ctx.branch_name, base=ctx.base_branch)

        if ctx.config.agent.runtime == "codex":
            return await self._amend_from_host(ctx)

        try:
            result = await dispatch_command(
                self,
                "/rearrange-commits",
                model=ctx.resolved_model or ctx.config.agent.model,
                fallback_model=ctx.get_cli_fallback_model(),
                task_type=ctx.routing_task_type(self.TASK_TYPE),
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
                timeout=ctx.config.agent.step_timeout,
                ctx=ctx,
            )
            ctx.add_usage(result)
            return StepResult(
                success=True,
                summary="Commits reorganized into clean logical units",
                cost_usd=result.cost_usd,
            )
        except RuntimeError as exc:
            return StepResult(success=False, summary="Commit reorganization failed", error=str(exc))

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
