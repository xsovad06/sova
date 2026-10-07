"""Step: Rearrange commits -- reorganize branch history into clean, logical commits.

Invokes /rearrange-commits in the working directory so that review fixes are
folded back into the original commits rather than appended as separate
"address review" commits. The result is a history that reads as if the code
was written correctly from the start.

Used in the address-review pipeline in place of CommitStep.
"""

from __future__ import annotations

import re

from sova.agents.registry import artifact_exclusion_prefixes
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.llm.client import invoke_command
from sova.utils.logging import get_logger
from sova.utils.shell import run

# `.claude/` and `.sova/` hold agent infrastructure the pipeline itself writes
# into the worktree (ensure_claude_artifacts mirrors commands, rules and skills
# from the primary checkout on every run), so churn there is inherited state,
# never the agent's own output. Counting it made every address-review run pause
# whenever the primary checkout was dirty: 17 gate failures before #1090.
# `artifact_exclusion_prefixes()` adds every registered RuntimeAdapter's own
# mirrored directory (e.g. Codex's `.agents/skills/`) on top of those two, so a
# future adapter's skills mirror can't reopen the same failure mode for a new,
# not-yet-hand-listed directory. Every prefix here is a whole directory
# (trailing `/`): `_mirror_runtime_skills()` copies an adapter's entire skills
# directory into the worktree, including hand-authored content sharing it under
# a plain name, so the exclusion has to cover the whole directory too, not just
# the SOVA-managed, name-prefixed subtree within it.
_RUNTIME_ARTIFACT_PREFIXES = frozenset({".claude/", ".sova/"}) | artifact_exclusion_prefixes()
_IGNORABLE_UNTRACKED_RE = re.compile(
    "|".join(rf"^{re.escape(prefix)}" for prefix in sorted(_RUNTIME_ARTIFACT_PREFIXES))
)


def _pathspec_exclude(prefix: str) -> str:
    """Build a git pathspec excluding *prefix*.

    A bare name-prefix (no trailing ``/``, e.g. ``.agents/skills/sova-``) needs
    a trailing ``*`` to exclude everything under matching directories: git's
    default (non-literal) pathspec wildcard matching already treats ``*`` as
    matching any characters including ``/``, so no explicit ``:(glob)`` magic
    is required.
    """
    if prefix.endswith("/"):
        return f":(exclude){prefix}"
    return f":(exclude){prefix}*"


# Pathspecs excluded from the "did the agent leave work uncommitted?" checks.
# _IGNORABLE_UNTRACKED_RE above already applies the same rule to untracked
# files, and DevelopStep._NON_SUBSTANTIVE_RE applies it to change detection.
_EXCLUDED_PATHSPECS = tuple(_pathspec_exclude(prefix) for prefix in sorted(_RUNTIME_ARTIFACT_PREFIXES))

log = get_logger(component="step.rearrange_commits")


class RearrangeCommitsStep(BaseStep):
    name = "rearrange_commits"
    TASK_TYPE = "rearrange_commits"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.rearrange_commits", branch=ctx.branch_name, base=ctx.base_branch)

        try:
            result = await invoke_command(
                "/rearrange-commits",
                model=ctx.resolved_model or ctx.config.agent.model,
                fallback_model=ctx.get_cli_fallback_model(),
                task_type=ctx.routing_task_type(self.TASK_TYPE),
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
                timeout=ctx.config.agent.step_timeout,
            )
            ctx.add_usage(result)
            return StepResult(
                success=True,
                summary="Commits reorganized into clean logical units",
                cost_usd=result.cost_usd,
            )
        except RuntimeError as exc:
            return StepResult(success=False, summary="Commit reorganization failed", error=str(exc))

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
