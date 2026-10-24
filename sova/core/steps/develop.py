"""Step 4: Develop. Invoke Claude CLI for TDD implementation.

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

from sova.core.agent_dispatch import dispatch_command, dispatch_prompt
from sova.core.context import BUDGET_STOP_RETRY_THRESHOLD, ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git.artifact_exclusions import RUNTIME_ARTIFACT_PREFIXES
from sova.llm.client import invoke
from sova.llm.errors import format_fix_llm_failure
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.develop")


def _load_spec_for_develop(ctx: ExecutionContext) -> str:
    """Load approved spec sections as compressed context for the develop step.

    Returns the spec content string, or empty string if no approved spec exists.
    """
    try:
        from sova.core.spec_utils import read_spec
        from sova.core.steps._spec_helpers import DEVELOP_SECTIONS, extract_sections_from_text

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
    except (OSError, ValueError):
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

        from sova.llm.routing import resolve_extraction_model

        model = resolve_extraction_model(ctx.config.llm)
        llm_result = await invoke(prompt, model=model, task_type="extraction", cwd=ctx.working_dir, timeout=60)
        ctx.add_usage(llm_result)

        append_spec_section(ctx.issue_number, SECTION_IMPLEMENTATION_NOTES, llm_result.text.strip(), ctx.project_dir)
    except Exception:  # noqa: BLE001 (implementation notes are optional; the develop step still succeeds)
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


# Chars of check output kept when logging and when persisting a failure
# summary. The tail, not the head, is diagnosable: a check command (e.g.
# ``make check``) prints lint/build output first and the failing test names
# last.
_CHECK_OUTPUT_TAIL_CHARS = 500


def _with_check_output_tail(summary: str, check_output: str) -> str:
    """Append the tail of *check_output* to a failure *summary*.

    Without this, a loop exit that bails before a fix cycle completes
    (budget exhausted, step deadline approaching, duplicate failure, cycles
    exhausted, or a fix cycle itself giving up: no changes produced, test
    weakening detected) leaves ``step_executions.output_summary`` and
    ``failure_records.message`` with only the generic reason string, since
    both are populated from this return value, not from the log line that
    already carries the tail (issue #1260).

    *check_output* is a target project's own raw subprocess output, and this
    return value flows into ``TaskRun.error_message``, which
    ``telemetry_push.py`` posts to an external hub: redact before truncating
    (mirroring ``_build_exit_failure_message()`` in
    ``sova/dashboard/services/agent_db.py``) so a secret can't be sliced in
    half at the truncation boundary and survive redaction as a mangled
    fragment. The tail's lines are joined with " | " rather than embedded
    newlines for the same reason that function does it: this value is
    rendered in dashboard table cells and single-line log records. The
    "\\n\\nLast check output" separator before it is a stable split point
    ``WorkflowEngine._handle_step_failure_result()`` uses to classify only
    the reason paragraph, not a target project's own output, as a billing or
    rate-limit failure (issue #1260 review).
    """
    from sova.llm.egress import scan_and_redact

    redacted = scan_and_redact(check_output).redacted_text
    tail = redacted[-_CHECK_OUTPUT_TAIL_CHARS:].strip()
    if not tail:
        return summary
    tail_line = " | ".join(line.strip() for line in tail.splitlines() if line.strip())
    return f"{summary}\n\nLast check output (tail): {tail_line}"


_TEST_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]+\.py|tests\.py|[^/]+_test\.py)$")

# Fixed allowance for the non-LLM overhead inside a fix cycle (git diff/status
# calls in _detect_fix_changes, ruff, commit) that check_timeout + fix_timeout
# alone do not account for. A slow or heavily loaded environment can add
# enough real wall-clock time here that a cycle allowed to start under a bare
# check_timeout + fix_timeout reserve still overruns step_time_remaining,
# landing back on the uninformative step_hard_timeout path this budget check
# exists to avoid.
_CYCLE_OVERHEAD_BUFFER_SECONDS = 60

# A floor on the measured-duration-derived check reserve (see
# _check_loop_budget): a check that failed fast (e.g. a lint error caught in
# under a second) does not guarantee the *next* cycle's check will be just as
# fast, since a later-running, slower failure mode (e.g. a full test suite
# that only runs once lint passes) can surface instead. Without a floor the
# reserve could shrink to near zero and let a cycle start that the step's
# remaining deadline can't actually absorb.
_MIN_CHECK_RESERVE_SECONDS = 10.0

# Safety margin subtracted from the step's remaining deadline when bounding a
# check invocation's own timeout (see _check_timeout_for_remaining): leaves
# room for the cheap git/log bookkeeping around the check call itself so the
# check's own timeout, not the outer asyncio.timeout, is what fires first.
_CHECK_TIMEOUT_SAFETY_MARGIN_SECONDS = 5


def _check_timeout_for_remaining(ctx: ExecutionContext, configured_timeout: int) -> int:
    """Bound a check invocation's timeout by the step's actual remaining deadline.

    _check_loop_budget() only runs once, at the start of a cycle, sized from
    the *previous* check's measured duration. A cycle admitted on a fast
    measurement (e.g. lint failed in 2s) can still run into a slower failure
    mode on the re-run (lint now passes, the full test suite runs next), and
    without this bound that re-run check would still be launched with the
    full configured ``check_timeout``, overrunning the step's own deadline
    and getting killed by the outer WorkflowEngine timeout as an
    uninformative ``step_hard_timeout`` instead of ending as a normal
    timed-out check result this loop can report and size the next budget
    check from.
    """
    if ctx.step_time_remaining is None or ctx.step_deadline_is_runaway:
        return configured_timeout
    return max(1, min(configured_timeout, int(ctx.step_time_remaining) - _CHECK_TIMEOUT_SAFETY_MARGIN_SECONDS))


_NON_SUBSTANTIVE_RE = re.compile(
    r"(?:"
    r"Pipfile\.lock$|package-lock\.json$|yarn\.lock$|pnpm-lock\.yaml$|"
    r"poetry\.lock$|Gemfile\.lock$|composer\.lock$|Cargo\.lock$|go\.sum$|"
    + "|".join(rf"^{re.escape(prefix)}" for prefix in sorted(RUNTIME_ARTIFACT_PREFIXES))
    + r")"
)


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
    TASK_TYPE = "develop"
    max_retries = 1
    requires_tools = True

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.develop", issue=ctx.issue_number, cwd=str(ctx.working_dir))

        args = ctx.issue_number
        spec_context = _load_spec_for_develop(ctx)
        if spec_context:
            # Pass spec as inline context so the /develop agent uses it directly
            # instead of re-fetching the verbose issue body from GitHub.
            args = (
                f"{ctx.issue_number}\n\n"
                "## Spec Context (use as primary task context: do NOT re-fetch the issue body)\n\n"
                f"{spec_context}"
            )

        try:
            cost_before_develop = ctx.cost_usd
            result = await dispatch_command(
                self,
                "/develop",
                args=args,
                model=ctx.resolved_model or ctx.config.agent.model,
                fallback_model=ctx.get_cli_fallback_model(),
                task_type=ctx.routing_task_type(self.TASK_TYPE),
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
                timeout=ctx.config.develop.step_timeout,
                ctx=ctx,
            )
            ctx.add_usage(result)
            ctx.session_id = result.session_id

            if result.cost_usd < 0.50:
                diff = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
                staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
                log_check = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
                untracked = await run("git", "status", "--porcelain", cwd=ctx.working_dir)
                has_any_change = bool(
                    (diff.success and diff.stdout.strip())
                    or (staged.success and staged.stdout.strip())
                    or (log_check.success and log_check.stdout.strip())
                    or (untracked.success and any(line.startswith("??") for line in untracked.stdout.splitlines()))
                )
                if not has_any_change:
                    error = f"Development produced no changes (cost ${result.cost_usd:.2f} < $0.50 threshold)"
                    log.warning("step.develop.early_no_change_abort", cost=result.cost_usd)
                    return StepResult(
                        success=False,
                        summary="Development failed",
                        error=error,
                        cost_usd=ctx.cost_usd - cost_before_develop,
                    )

            await _append_implementation_notes(ctx)

            checks_passed, check_summary = await self._run_inner_check_loop(ctx)

            summary = f"Development completed ({result.total_tokens} tokens)"
            if check_summary:
                summary += f"; {check_summary}"

            return StepResult(
                success=checks_passed,
                summary=summary,
                cost_usd=ctx.cost_usd - cost_before_develop,
                error=check_summary if not checks_passed else None,
            )
        except RuntimeError as exc:
            return StepResult(success=False, summary="Development failed", error=str(exc))

    async def _run_inner_check_loop(self, ctx: ExecutionContext) -> tuple[bool, str]:
        """Run the project's check command and fix failures via LLM.

        Returns a (passed, summary) tuple where passed is True when checks
        succeed (or are skipped) and summary is a short description string.
        """
        import time

        develop_cfg = ctx.config.develop
        max_cycles = develop_cfg.max_fix_cycles
        if max_cycles == 0:
            log.info("step.develop.inner_check_disabled")
            return True, ""

        check_cmd = await self._resolve_and_verify_check_cmd(ctx)
        if check_cmd is None:
            return True, ""

        loop_start_time = time.monotonic()

        # Initial check run
        check_start = time.monotonic()
        check_result = await run(
            "sh",
            "-c",
            check_cmd,
            cwd=ctx.working_dir,
            timeout=_check_timeout_for_remaining(ctx, develop_cfg.check_timeout),
        )
        last_check_duration = time.monotonic() - check_start
        max_check_duration = last_check_duration
        if check_result.success:
            log.info("step.develop.checks_passed_first_try")
            return True, "checks passed"

        check_output = (check_result.stdout + "\n" + check_result.stderr).strip()
        log.warning("step.develop.checks_failed", output=check_output[-_CHECK_OUTPUT_TAIL_CHARS:])

        last_check_output_tail: str = check_output[-3000:]

        for cycle in range(1, max_cycles + 1):
            budget_check = self._check_loop_budget(
                ctx, loop_start_time, develop_cfg.max_fix_time, cycle, max_check_duration
            )
            if budget_check is not None:
                return False, _with_check_output_tail(budget_check, check_output)

            if ctx.resource_remaining_fraction < BUDGET_STOP_RETRY_THRESHOLD:
                log.warning(
                    "step.develop.budget_fraction_stop_retry",
                    cycle=cycle,
                    fraction=ctx.resource_remaining_fraction,
                )
                return True, (
                    f"checks still failing after {cycle - 1} fix cycle(s); stopping retries at "
                    f"{ctx.resource_remaining_fraction:.0%} budget remaining"
                )

            log.info("step.develop.fix_cycle", cycle=cycle, max=max_cycles)

            result = await self._try_fix_cycle(ctx, check_cmd, check_output, cycle, max_cycles)
            if result is not None:
                if result == "":
                    continue  # no-op or test weakening; skip re-run
                return False, _with_check_output_tail(result, check_output)

            # Re-run checks
            check_start = time.monotonic()
            check_result = await run(
                "sh",
                "-c",
                check_cmd,
                cwd=ctx.working_dir,
                timeout=_check_timeout_for_remaining(ctx, develop_cfg.check_timeout),
            )
            last_check_duration = time.monotonic() - check_start
            max_check_duration = max(max_check_duration, last_check_duration)
            if check_result.success:
                log.info("step.develop.checks_passed_after_fix", cycles=cycle)
                return True, f"checks passed after {cycle} fix cycle(s)"

            check_output = (check_result.stdout + "\n" + check_result.stderr).strip()
            duplicate_check = self._check_duplicate_failure(check_output, last_check_output_tail, cycle)
            if duplicate_check is not None:
                return False, _with_check_output_tail(duplicate_check, check_output)

            last_check_output_tail = check_output[-3000:]
            log.warning(
                "step.develop.checks_still_failing", cycle=cycle, output=check_output[-_CHECK_OUTPUT_TAIL_CHARS:]
            )

        return False, _with_check_output_tail(f"checks still failing after {max_cycles} fix cycle(s)", check_output)

    def _check_loop_budget(
        self,
        ctx: ExecutionContext,
        loop_start_time: float,
        max_fix_time: int,
        cycle: int,
        measured_check_duration: float | None = None,
    ) -> str | None:
        """Check time and budget constraints for the inner check loop.

        Returns error summary if budget exceeded, None if OK to continue.

        Two time constraints are checked: the loop's own max_fix_time window,
        and (when known) the step's own remaining hard-timeout budget. A
        worst-case cycle (check reserve + fix_timeout, plus a fixed buffer for
        the non-LLM overhead between them: git diff/status calls, ruff, commit)
        must still fit inside what is left of the develop step's per-tier hard
        timeout (develop.step_timeout_normal/_complex), otherwise
        the outer WorkflowEngine hard timeout would kill the step mid-cycle,
        reporting the generic "step_hard_timeout" instead of this loop's own
        diagnostic summary, and starving whatever pipeline steps run after
        this one.

        The check reserve is sized from *measured_check_duration* (the
        highest wall-clock duration any check run in this loop has taken so
        far, a high-water mark rather than just the most recent run), capped
        at develop.check_timeout and floored at _MIN_CHECK_RESERVE_SECONDS,
        rather than always assuming the full check_timeout ceiling: a fast
        check (e.g. a syntax error caught in a couple of seconds) should
        make another cycle easier to fit, not harder, but the next cycle's
        check isn't guaranteed to hit the same fast failure mode. The
        high-water mark means a slow failure mode observed once (e.g. lint
        passed and the full test suite ran) keeps the reserve sized for it
        on every subsequent cycle, rather than shrinking back down if a
        later cycle happens to fail fast again. ``None`` (no measurement
        available, e.g. a direct unit-test call to this method) falls back
        to the full check_timeout ceiling, matching the previous behavior.
        Each check invocation's own timeout is separately bounded by
        _check_timeout_for_remaining() so even an admitted cycle's re-run
        check ends as a diagnosable timeout rather than overrunning into the
        outer step-level hard timeout.

        When the step's remaining deadline was itself capped by the run-wide
        runaway wall-clock guard (ctx.step_deadline_is_runaway), this
        preemptive check is skipped: bailing out here would return a plain
        failed StepResult and silently lose the runaway guard's
        pause/resume (PAUSED) semantics. Instead the loop is allowed to run
        until the outer asyncio.timeout fires, which WorkflowEngine already
        classifies as a resumable runaway event.
        """
        import time

        elapsed = time.monotonic() - loop_start_time
        if elapsed >= max_fix_time:
            log.warning("step.develop.max_fix_time_exceeded", elapsed=int(elapsed), limit=max_fix_time)
            return f"checks still failing after {cycle - 1} fix cycle(s) (time budget exceeded)"

        step_remaining = ctx.step_time_remaining
        if step_remaining is not None and not ctx.step_deadline_is_runaway:
            develop_cfg = ctx.config.develop
            check_reserve = develop_cfg.check_timeout
            if measured_check_duration is not None:
                check_reserve = max(
                    min(measured_check_duration, develop_cfg.check_timeout),
                    _MIN_CHECK_RESERVE_SECONDS,
                )
            worst_case_cycle = check_reserve + develop_cfg.fix_timeout + _CYCLE_OVERHEAD_BUFFER_SECONDS
            if step_remaining < worst_case_cycle:
                log.warning(
                    "step.develop.step_deadline_insufficient",
                    cycle=cycle,
                    step_remaining=int(step_remaining),
                    worst_case_cycle=worst_case_cycle,
                )
                return f"checks still failing after {cycle - 1} fix cycle(s) (step deadline approaching)"

        if ctx.is_budget_exceeded:
            log.warning("step.develop.budget_exceeded_in_check_loop", cycle=cycle)
            return f"checks still failing after {cycle - 1} fix cycle(s) (budget exceeded)"

        return None

    def _check_duplicate_failure(
        self,
        check_output: str,
        last_check_output_tail: str,
        cycle: int,
    ) -> str | None:
        """Check if the failure output is identical to the previous cycle.

        Returns error summary if duplicate detected, None otherwise.
        """
        check_output_tail = check_output[-3000:]
        if check_output_tail == last_check_output_tail:
            log.warning("step.develop.duplicate_failure_detected", cycle=cycle)
            return f"checks still failing after {cycle} fix cycle(s) (duplicate failure)"
        return None

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
            f"Fix ALL issues causing the check failures. Modify only the source code: "
            f"do NOT weaken, delete, or skip tests. Do NOT commit: just fix and stage the changes."
        )
        try:
            # dispatch_prompt, not invoke: this prompt asks for source edits, so
            # it needs the same AgentRuntime the /develop dispatch above used. A
            # text-only provider would answer it with prose and no file changes.
            llm_result = await dispatch_prompt(
                self,
                prompt,
                model=ctx.resolved_model or ctx.config.agent.model,
                fallback_model=ctx.get_cli_fallback_model(),
                task_type=ctx.routing_task_type("develop_fix"),
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
                timeout=ctx.config.develop.fix_timeout,
                ctx=ctx,
            )
            ctx.add_usage(llm_result)
        except RuntimeError as exc:
            log.error("step.develop.fix_llm_failed", error=str(exc), exc_info=True)
            return format_fix_llm_failure(exc, cycle)
        return None

    async def _detect_fix_changes(self, ctx: ExecutionContext, pre_hash: str) -> dict[str, bool]:
        """Detect whether the LLM fix produced any changes (unstaged, staged, commits, or untracked).

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
        status_result = await run("git", "status", "--porcelain", cwd=ctx.working_dir)
        has_untracked = bool(
            status_result.success and any(line.startswith("??") for line in status_result.stdout.splitlines())
        )
        return {
            "has_unstaged": has_unstaged,
            "has_staged": has_staged,
            "has_new_commits": has_new_commits,
            "has_untracked": has_untracked,
            "any": has_unstaged or has_staged or has_new_commits or has_untracked,
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
        """Gate: development must produce actual substantive code changes."""
        diff_result = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        has_changes = bool(
            (diff_result.success and diff_result.stdout.strip()) or (staged.success and staged.stdout.strip())
        )
        # Also check commits ahead of base branch (Claude may have committed)
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_commits = bool(log_result.success and log_result.stdout.strip())
        # Also check for untracked new files: Claude often writes new modules without staging them.
        # git diff and git log are blind to untracked files, so they miss this case entirely.
        # --untracked-files=all forces one line per file rather than collapsing a wholly-untracked
        # directory into a single parent-level line _NON_SUBSTANTIVE_RE's prefixes can't match.
        status_result = await run("git", "status", "--porcelain", "--untracked-files=all", cwd=ctx.working_dir)
        has_untracked = bool(
            status_result.success and any(line.startswith("??") for line in status_result.stdout.splitlines())
        )

        if not (has_changes or has_commits or has_untracked):
            return GateCheckResult(
                passed=False,
                reason="Development produced no code changes",
            )

        # Collect filenames from all change sources for the substantive check.
        all_files: list[str] = []
        if has_changes:
            unstaged_names = await run("git", "diff", "--name-only", "HEAD", cwd=ctx.working_dir)
            if unstaged_names.success and unstaged_names.stdout.strip():
                all_files.extend(unstaged_names.stdout.strip().splitlines())
            staged_names = await run("git", "diff", "--cached", "--name-only", cwd=ctx.working_dir)
            if staged_names.success and staged_names.stdout.strip():
                all_files.extend(staged_names.stdout.strip().splitlines())
        if has_commits:
            committed_names = await run("git", "diff", "--name-only", f"{ctx.base_branch}..HEAD", cwd=ctx.working_dir)
            if committed_names.success and committed_names.stdout.strip():
                all_files.extend(committed_names.stdout.strip().splitlines())
        if has_untracked:
            for line in status_result.stdout.splitlines():
                if line.startswith("??"):
                    all_files.append(line[3:].strip())

        # Fail-open: if we detected changes (from --stat) but couldn't get filenames, pass.
        if not all_files:
            return GateCheckResult(passed=True)

        has_substantive = any(not _NON_SUBSTANTIVE_RE.search(f) for f in all_files)
        if not has_substantive:
            unique_files = sorted(set(all_files))
            log.warning(
                "step.develop.only_non_substantive_changes",
                files=unique_files[:5],
            )
            return GateCheckResult(
                passed=False,
                reason=f"Development produced no substantive code changes (only: {', '.join(unique_files[:3])})",
            )

        return GateCheckResult(passed=True)
