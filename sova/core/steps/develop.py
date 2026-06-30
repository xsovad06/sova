"""Step 4: Develop -- invoke Claude CLI for TDD implementation.

This is the critical step where actual code is written. The gate check
ensures that development actually produced code changes (preventing the
issue #60 failure mode where the agent ran through the entire pipeline
with zero changes).

After the /develop command returns, an inner check loop runs the project's
check command (e.g. ``make check``) and feeds failures back to the LLM for
correction, repeating until green or ``develop.max_fix_cycles`` exhausted.
"""

from __future__ import annotations

import re
from pathlib import Path

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.llm.client import invoke, invoke_command
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.develop")


def _load_spec_for_develop(ctx: ExecutionContext) -> str:
    """Load approved spec sections as compressed context for the develop step.

    Returns the spec content string, or empty string if no approved spec exists.
    """
    try:
        from sova.core.steps._spec_helpers import DEVELOP_SECTIONS, extract_sections_from_text
        from sova.dashboard.services.spec_service import read_spec

        spec_data = read_spec(ctx.issue_number, ctx.project_dir)
        if spec_data is None or spec_data.get("status") != "approved":
            return ""

        content = extract_sections_from_text(spec_data["raw_content"], DEVELOP_SECTIONS)
        if content:
            log.info(
                "step.develop.spec_compression",
                issue=ctx.issue_number,
                spec_chars=len(content),
            )
        return content
    except Exception:
        log.debug("step.develop.spec_load_failed", exc_info=True)
        return ""


async def _append_implementation_notes(ctx: ExecutionContext) -> None:
    """Summarize implementation deviations and append to spec (non-fatal)."""
    try:
        import asyncio

        from sova.core.steps._spec_helpers import (
            SECTION_IMPLEMENTATION_NOTES,
            SPEC_PLAN_SECTIONS,
            append_spec_section,
            read_spec_sections,
        )

        original_plan = read_spec_sections(ctx.issue_number, ctx.project_dir, SPEC_PLAN_SECTIONS)
        if not original_plan:
            return

        diff_task = run("git", "diff", "--stat", ctx.base_branch, cwd=ctx.working_dir)
        log_task = run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        diff_result, log_result = await asyncio.gather(diff_task, log_task)

        diff_stat = diff_result.stdout.strip() if diff_result.success else "(unavailable)"
        commit_log = log_result.stdout.strip() if log_result.success else "(no commits)"

        prompt = f"""You are a technical writer. Given the original spec and the actual implementation, \
produce a concise "Implementation Notes" section listing:
- Deviations from the spec's proposed approach
- Unexpected constraints discovered during implementation
- Key architectural choices not covered in the original spec

If the implementation matches the spec exactly, return "Implementation followed the spec as designed."

## Original Spec Plan
{original_plan}

## Implementation Summary
Diff stats:
{diff_stat}

Commits:
{commit_log}

Return ONLY the section content (no heading, no markdown fences). Keep it under 10 bullet points."""

        llm_result = await invoke(prompt, model="haiku", cwd=ctx.working_dir, timeout=60)
        ctx.add_cost(llm_result.cost_usd)

        append_spec_section(ctx.issue_number, SECTION_IMPLEMENTATION_NOTES, llm_result.text.strip(), ctx.project_dir)
    except Exception:
        log.warning("step.develop.implementation_notes_failed", exc_info=True)


def _resolve_check_cmd(ctx: ExecutionContext) -> str | None:
    """Determine the check command to use for the inner loop.

    Returns the command string, or None if no check command is available.
    """
    if ctx.config.check_cmd and ctx.config.check_cmd.strip():
        return ctx.config.check_cmd

    makefile = Path(ctx.working_dir) / "Makefile"
    if makefile.exists():
        return "make check"

    return None


_TEST_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]+\.py|tests\.py|[^/]+_test\.py)$")


async def _get_dirty_test_files(cwd: Path) -> set[str]:
    """Return test files with uncommitted changes (staged or unstaged) vs HEAD.

    Uses ``git diff HEAD --name-only`` which is index-based (no file I/O)
    and respects ``.gitignore``, unlike recursive glob + checksum.
    """
    result = await run("git", "diff", "HEAD", "--name-only", cwd=cwd)
    if not result.success or not result.stdout.strip():
        return set()
    return {f for f in result.stdout.strip().splitlines() if _TEST_FILE_RE.search(f)}


class DevelopStep(BaseStep):
    name = "develop"
    max_retries = 1

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.develop", issue=ctx.issue_number, cwd=str(ctx.working_dir))

        args = ctx.issue_number
        spec_context = _load_spec_for_develop(ctx)
        if spec_context:
            # Pass spec as inline context so the /develop agent uses it directly
            # instead of re-fetching the verbose issue body from GitHub.
            args = (
                f"{ctx.issue_number}\n\n"
                "## Spec Context (use as primary task context -- do NOT re-fetch the issue body)\n\n"
                f"{spec_context}"
            )

        try:
            result = await invoke_command(
                "/develop",
                args=args,
                model=ctx.config.agent.model,
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
                timeout=ctx.config.agent.step_timeout,
            )
            ctx.add_cost(result.cost_usd)
            ctx.session_id = result.session_id

            await _append_implementation_notes(ctx)

            cost_before_checks = ctx.cost_usd
            check_summary = await self._run_inner_check_loop(ctx)
            inner_loop_cost = ctx.cost_usd - cost_before_checks

            summary = f"Development completed ({result.total_tokens} tokens)"
            if check_summary:
                summary += f"; {check_summary}"

            return StepResult(
                success=True,
                summary=summary,
                cost_usd=result.cost_usd + inner_loop_cost,
            )
        except RuntimeError as exc:
            return StepResult(success=False, summary="Development failed", error=str(exc))

    async def _run_inner_check_loop(self, ctx: ExecutionContext) -> str:
        """Run the project's check command and fix failures via LLM.

        Returns a short summary string (empty if skipped or passed on first try).
        """
        develop_cfg = ctx.config.develop
        max_cycles = develop_cfg.max_fix_cycles
        if max_cycles == 0:
            log.info("step.develop.inner_check_disabled")
            return ""

        check_cmd = await self._resolve_and_verify_check_cmd(ctx)
        if check_cmd is None:
            return ""

        # Initial check run
        check_result = await run("sh", "-c", check_cmd, cwd=ctx.working_dir, timeout=develop_cfg.check_timeout)
        if check_result.success:
            log.info("step.develop.checks_passed_first_try")
            return "checks passed"

        check_output = (check_result.stdout + "\n" + check_result.stderr).strip()
        log.warning("step.develop.checks_failed", output=check_output[:500])

        for cycle in range(1, max_cycles + 1):
            if ctx.is_budget_exceeded:
                log.warning("step.develop.budget_exceeded_in_check_loop", cycle=cycle)
                return f"checks still failing after {cycle - 1} fix cycle(s) (budget exceeded)"

            log.info("step.develop.fix_cycle", cycle=cycle, max=max_cycles)

            result = await self._try_fix_cycle(ctx, check_cmd, check_output, cycle, max_cycles)
            if result is not None:
                if result == "":
                    continue  # no-op or test weakening; skip re-run
                return result

            # Re-run checks
            check_result = await run("sh", "-c", check_cmd, cwd=ctx.working_dir, timeout=develop_cfg.check_timeout)
            if check_result.success:
                log.info("step.develop.checks_passed_after_fix", cycles=cycle)
                return f"checks passed after {cycle} fix cycle(s)"

            check_output = (check_result.stdout + "\n" + check_result.stderr).strip()
            log.warning("step.develop.checks_still_failing", cycle=cycle, output=check_output[:500])

        return f"checks still failing after {max_cycles} fix cycle(s)"

    async def _resolve_and_verify_check_cmd(self, ctx: ExecutionContext) -> str | None:
        """Resolve the check command and verify it is executable.

        Returns the command string, or None if unavailable/not found.
        """
        check_cmd = _resolve_check_cmd(ctx)
        if check_cmd is None:
            log.info("step.develop.no_check_cmd")
            return None

        cmd_name = check_cmd.split()[0]
        probe = await run("sh", "-c", f"command -v {cmd_name}", cwd=ctx.working_dir, timeout=10)
        if not probe.success:
            log.warning("step.develop.check_cmd_not_found", cmd=check_cmd)
            return None

        return check_cmd

    async def _try_fix_cycle(
        self,
        ctx: ExecutionContext,
        check_cmd: str,
        check_output: str,
        cycle: int,
        max_cycles: int,
    ) -> str | None:
        """Attempt one fix cycle: invoke LLM, detect no-op, guard test weakening.

        Returns a summary string to short-circuit the loop, or None to continue
        (re-run checks).
        """
        develop_cfg = ctx.config.develop
        pre_dirty = await _get_dirty_test_files(ctx.working_dir) if develop_cfg.guard_test_weakening else set()

        pre_hash = await self._get_head_hash(ctx)
        fix_error = await self._invoke_fix_llm(ctx, check_cmd, check_output, cycle)
        if fix_error is not None:
            return fix_error

        changes = await self._detect_fix_changes(ctx, pre_hash)
        if not changes["any"]:
            log.warning("step.develop.fix_no_changes", cycle=cycle)
            if cycle == max_cycles:
                return f"checks still failing after {max_cycles} fix cycle(s) (no changes produced)"
            return ""  # signal: skip re-run, continue loop

        if develop_cfg.guard_test_weakening:
            weakened = await self._check_test_weakening(ctx, pre_dirty, pre_hash, changes["has_new_commits"], cycle)
            if weakened:
                # Defense-in-depth: if the LLM committed despite explicit instructions
                # not to, `git checkout HEAD -- files` would restore from the bad HEAD.
                # Use `git reset --hard pre_hash` to undo the entire commit instead.
                if changes["has_new_commits"] and pre_hash:
                    log.error(
                        "step.develop.llm_committed_despite_instructions",
                        cycle=cycle,
                        msg="LLM created commits in fix loop (violating explicit instructions); "
                        "resetting to pre-fix state",
                    )
                    await run("git", "reset", "--hard", pre_hash, cwd=ctx.working_dir)
                else:
                    await run("git", "checkout", "HEAD", "--", *weakened, cwd=ctx.working_dir)
                if cycle == max_cycles:
                    return f"checks still failing after {max_cycles} fix cycle(s) (test weakening detected)"
                return ""  # signal: skip re-run, continue loop

        return None  # signal: proceed to re-run checks

    async def _get_head_hash(self, ctx: ExecutionContext) -> str:
        """Return the current HEAD commit hash."""
        result = await run("git", "rev-parse", "HEAD", cwd=ctx.working_dir)
        return result.stdout.strip() if result.success else ""

    async def _invoke_fix_llm(
        self,
        ctx: ExecutionContext,
        check_cmd: str,
        check_output: str,
        cycle: int,
    ) -> str | None:
        """Invoke the LLM to fix check failures. Returns error summary or None on success."""
        prompt = (
            f"The project's check command (`{check_cmd}`) failed with the following output:\n\n"
            f"```\n{check_output[-3000:]}\n```\n\n"
            f"Fix ALL issues causing the check failures. Modify only the source code -- "
            f"do NOT weaken, delete, or skip tests. Do NOT commit -- just fix and stage the changes."
        )
        try:
            llm_result = await invoke(
                prompt,
                model=ctx.config.agent.model,
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
            )
            ctx.add_cost(llm_result.cost_usd)
        except RuntimeError as exc:
            log.error("step.develop.fix_llm_failed", error=str(exc), exc_info=True)
            return f"check fix LLM failed on cycle {cycle}: {exc}"
        return None

    async def _detect_fix_changes(self, ctx: ExecutionContext, pre_hash: str) -> dict[str, bool]:
        """Detect whether the LLM fix produced any changes (unstaged, staged, or new commits).

        Commit detection is defense-in-depth: the prompt instructs the LLM not to commit,
        but agents sometimes disobey. Detecting commits here lets _try_fix_cycle handle
        test weakening restoration correctly (via ``git reset --hard``).
        """
        diff_result = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        has_unstaged = diff_result.success and bool(diff_result.stdout.strip())
        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        has_staged = staged.success and bool(staged.stdout.strip())
        post_hash = await self._get_head_hash(ctx)
        has_new_commits = pre_hash != post_hash
        return {
            "has_unstaged": has_unstaged,
            "has_staged": has_staged,
            "has_new_commits": has_new_commits,
            "any": has_unstaged or has_staged or has_new_commits,
        }

    async def _check_test_weakening(
        self,
        ctx: ExecutionContext,
        pre_dirty: set[str],
        pre_hash: str,
        has_new_commits: bool,
        cycle: int,
    ) -> list[str]:
        """Check if the fix modified test files. Returns sorted list of weakened files (empty if clean)."""
        post_dirty = await _get_dirty_test_files(ctx.working_dir)
        newly_modified_uncommitted = post_dirty - pre_dirty
        newly_modified_committed: set[str] = set()
        if has_new_commits and pre_hash:
            committed_diff = await run("git", "diff", "--name-only", pre_hash, "HEAD", cwd=ctx.working_dir)
            if committed_diff.success and committed_diff.stdout.strip():
                newly_modified_committed = {
                    f for f in committed_diff.stdout.strip().splitlines() if _TEST_FILE_RE.search(f)
                }
        newly_modified = sorted(newly_modified_uncommitted | newly_modified_committed)
        if newly_modified:
            log.warning(
                "step.develop.test_weakening_detected",
                files=newly_modified[:5],
                cycle=cycle,
            )
        return newly_modified

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: development must produce actual code changes."""
        diff_result = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        has_changes = bool(
            (diff_result.success and diff_result.stdout.strip()) or (staged.success and staged.stdout.strip())
        )
        # Also check commits ahead of base branch (Claude may have committed)
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_commits = bool(log_result.success and log_result.stdout.strip())

        if has_changes or has_commits:
            return GateCheckResult(passed=True)
        return GateCheckResult(
            passed=False,
            reason="Development produced no code changes",
        )
