"""Step 12: Address external findings. Fetch and fix issues from external review tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypedDict

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git.artifact_exclusions import EXCLUDED_PATHSPECS, IGNORABLE_UNTRACKED_RE
from sova.utils.logging import get_logger
from sova.utils.shell import run


class ExternalFindingDict(TypedDict):
    """Schema for serialized external findings passed through handoffs."""

    source: str
    severity: str
    file_path: str
    tool_id: str
    message: str


log = get_logger(component="step.address_external_findings")

# Distinct from the success-path message: a reader of `git log` (human or
# bot) must be able to tell, from the subject line alone, that the dispatch
# that produced this commit failed and never confirmed every finding was
# addressed, rather than reading the same confident "address external review
# findings" subject on both an actually-complete fix and a partial one.
_FINDINGS_COMMIT_MESSAGE = "fix(core): address external review findings"
_PRESERVE_COMMIT_MESSAGE = "fix(core): preserve partial external review fixes after agent failure"


@dataclass(frozen=True)
class _UncommittedChangesCheck:
    """Outcome of checking whether the worktree holds uncommitted edits.

    ``checked=False`` means at least one of the underlying git queries itself
    failed (e.g. index.lock contention), so whether edits exist is genuinely
    unknown. Callers must never treat that the same as ``has_changes=False``:
    a helper that swallows a query failure into the "clean" answer silently
    drops real work it never actually looked at.
    """

    checked: bool
    has_changes: bool = False


class _DispatchFailedButPreserved(RuntimeError):
    """A dispatch failure whose preserved edits were pushed to the PR branch.

    Raised by :meth:`AddressExternalFindingsStep._apply_fixes` instead of the
    bare dispatch error when preservation got the agent's pre-failure edits
    onto the remote: ``execute()`` uses this to let the pipeline continue
    (``monitor_ci``/``handoff_to_reviewer`` still need to run against the
    commit that was just pushed) rather than ending the run on a PR whose new
    head nothing is watching.
    """

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


class AddressExternalFindingsStep(BaseStep):
    name = "address_external_findings"
    max_retries = 0
    requires_tools = True

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if ctx.pr_number is None:
            return StepResult(success=False, summary="No PR to address", error="pr_number is None")

        from sova.adapters.external_reviews import (
            _fetch_coderabbit_threads,
            fetch_sonarcloud_coverage_issues,
            fetch_sonarcloud_issues,
            format_coverage_findings_for_prompt,
            format_findings_for_prompt,
            resolve_coderabbit_threads,
        )

        ext = ctx.config.external_reviews
        all_findings = []
        coderabbit_thread_ids: list[str] = []
        coverage_prompt = ""

        tasks: list[tuple[str, Any]] = []
        if "sonarcloud" in ext.tools and ext.sonarcloud.project_key:
            pk = ext.sonarcloud.project_key
            tasks.append(("sonar_issues", fetch_sonarcloud_issues(pk, ctx.pr_number)))
            tasks.append(
                (
                    "sonar_coverage",
                    fetch_sonarcloud_coverage_issues(pk, ctx.pr_number, required_pct=ext.sonarcloud.coverage_threshold),
                )
            )
        if "coderabbit" in ext.tools:
            coro = _fetch_coderabbit_threads(ctx.repo, ctx.pr_number, github_user=ctx.config.github_user)
            tasks.append(("coderabbit", coro))

        if tasks:
            results = await asyncio.gather(*(t[1] for t in tasks))
            result_map = {tasks[i][0]: results[i] for i in range(len(tasks))}
        else:
            result_map = {}

        if "sonar_issues" in result_map:
            all_findings.extend(result_map["sonar_issues"])
        if "sonar_coverage" in result_map:
            report = result_map["sonar_coverage"]
            if report and (report.coverage_pct < report.required_pct or report.findings):
                coverage_prompt = format_coverage_findings_for_prompt(report)
        if "coderabbit" in result_map:
            cr_result = result_map["coderabbit"]
            all_findings.extend(cr_result.findings)
            coderabbit_thread_ids = cr_result.thread_ids

        if not all_findings and not coverage_prompt:
            log.info("step.address_external_findings.none_found", pr=ctx.pr_number)
            return StepResult(success=True, summary="No external findings to address")

        log.info(
            "step.address_external_findings.found",
            count=len(all_findings),
            has_coverage_gap=bool(coverage_prompt),
            sources=list({f.source for f in all_findings}),
        )

        # Capture findings on ctx BEFORE applying fixes so the handoff
        # records what was *found*, regardless of whether fixes succeed.
        ctx.addressed_external_findings = [
            ExternalFindingDict(
                source=f.source,
                severity=f.severity,
                file_path=f.file_path,
                tool_id=f.tool_id,
                message=f.message,
            )
            for f in all_findings
        ]
        if coverage_prompt:
            ctx.addressed_external_findings.append(
                ExternalFindingDict(
                    source="sonarcloud",
                    severity="coverage",
                    file_path="project-wide",
                    tool_id="",
                    message="Coverage gap remediation applied",
                )
            )

        prompt = format_findings_for_prompt(all_findings)
        if coverage_prompt:
            prompt = f"{prompt}\n\n---\n\n{coverage_prompt}" if prompt else coverage_prompt

        # Captured so a dispatch failure can still bill the StepResult it
        # returns: agent_dispatch._run_agent_process() calls ctx.add_usage()
        # for a failed turn's reported cost before raising, so the spend is
        # already on ctx.cost_usd by the time either except branch below
        # runs. Without this delta, the failed StepResult.cost_usd (what
        # StepExecution.cost_usd, surfaced on the run-detail dashboard,
        # records) would read 0 for a run that spent real money.
        cost_before = ctx.cost_usd
        try:
            cost, has_changes, new_commits = await self._apply_fixes(ctx, prompt)
        except _DispatchFailedButPreserved as exc:
            # The agent's pre-failure edits made it to the remote PR branch,
            # so the pipeline must keep going: monitor_ci needs to watch the
            # commit that was just pushed, and handoff_to_reviewer still needs
            # to spawn the Reviewer against it. Ending the run here (as a bare
            # dispatch failure would) strands the PR on a brand-new,
            # unreviewed head with nothing watching it. CodeRabbit threads are
            # still never resolved on this path: a dispatch that failed never
            # confirmed it addressed every finding.
            log.error(
                "step.address_external_findings.dispatch_failed_partial_preserved",
                error=str(exc.original),
            )
            return StepResult(
                success=True,
                summary=(
                    "Partially addressed external findings before a dispatch failure "
                    f"(edits preserved and pushed): {exc.original}"
                ),
                cost_usd=ctx.cost_usd - cost_before,
            )
        except RuntimeError as exc:
            log.error("step.address_external_findings.dispatch_failed", error=str(exc))
            return StepResult(
                success=False,
                summary=f"Failed to address external findings: {exc}",
                error=str(exc),
                cost_usd=ctx.cost_usd - cost_before,
            )

        # Gated on actual evidence of edits, not billing: an agent run can be
        # billed (cost > 0) while producing zero file changes, which must not
        # resolve external review threads it never actually addressed.
        if coderabbit_thread_ids and (has_changes or new_commits):
            try:
                await resolve_coderabbit_threads(
                    coderabbit_thread_ids,
                    github_user=ctx.config.github_user,
                )
                log.info(
                    "step.address_external_findings.threads_resolved",
                    count=len(coderabbit_thread_ids),
                )
            except RuntimeError:
                log.warning("step.address_external_findings.resolve_failed", exc_info=True)
        elif coderabbit_thread_ids:
            log.info("step.address_external_findings.threads_not_resolved", reason="no_changes_detected")

        parts: list[str] = []
        if all_findings:
            parts.append(f"{len(all_findings)} external finding(s)")
        if coverage_prompt:
            parts.append("coverage gap remediation")
        summary = f"Addressed {' + '.join(parts)}" if parts else "Addressed external findings"

        return StepResult(
            success=True,
            summary=summary,
            cost_usd=cost,
        )

    async def _check_uncommitted_changes(self, ctx: ExecutionContext) -> _UncommittedChangesCheck:
        """Check whether the worktree holds edits that are not yet committed.

        Covers all three shapes an agent's work can take: unstaged edits to
        tracked files, already-staged edits, and brand-new untracked files.
        The untracked check is not redundant: an agent that only creates files
        (a new test module, for instance) moves neither ``git diff`` nor
        ``git diff --cached``, so omitting it reads real work as "no changes"
        and discards it. Mirrored agent infrastructure (``.claude/``,
        ``.sova/``, a runtime's own skills mirror) is excluded from both the
        diff pathspecs and the untracked-file scan: none of it is the agent's
        own work, so churn there must never be read as "uncommitted changes"
        (issue #1090's failure mode, reopened here without the exclusion).

        Returns ``checked=False`` when any of the three underlying git
        queries itself fails, so a caller never mistakes "could not verify"
        for "verified clean".
        """
        diff_result = await run("git", "diff", "--stat", "HEAD", "--", ".", *EXCLUDED_PATHSPECS, cwd=ctx.working_dir)
        staged = await run("git", "diff", "--cached", "--stat", "--", ".", *EXCLUDED_PATHSPECS, cwd=ctx.working_dir)
        # --untracked-files=all forces one line per untracked file rather than
        # collapsing a wholly-untracked directory (e.g. a fresh `.agents/skills/`
        # mirror in a project that doesn't track it) into a single parent-level
        # line that IGNORABLE_UNTRACKED_RE's per-directory prefixes can't match.
        status = await run("git", "status", "--porcelain", "--untracked-files=all", cwd=ctx.working_dir)

        if not (diff_result.success and staged.success and status.success):
            failed = [
                name
                for name, result in (
                    ("git diff --stat HEAD", diff_result),
                    ("git diff --cached --stat", staged),
                    ("git status --porcelain", status),
                )
                if not result.success
            ]
            log.warning("step.address_external_findings.status_check_failed", failed_commands=failed)
            return _UncommittedChangesCheck(checked=False, has_changes=True)

        untracked = any(
            line.startswith("??") and not IGNORABLE_UNTRACKED_RE.search(line[3:].strip())
            for line in status.stdout.splitlines()
        )
        has_changes = bool(diff_result.stdout.strip() or staged.stdout.strip() or untracked)
        return _UncommittedChangesCheck(checked=True, has_changes=has_changes)

    async def _detect_changes(self, ctx: ExecutionContext, prev_count: int) -> tuple[bool, bool]:
        """Detect if LLM produced changes and new commits.

        Returns (has_changes, new_commits) tuple. An unverifiable uncommitted-
        changes check (``checked=False``) is treated as ``has_changes=True``:
        failure must not look like success, and a silent "no changes" answer
        here is indistinguishable from the agent genuinely doing nothing.
        """
        check = await self._check_uncommitted_changes(ctx)
        if not check.checked:
            log.warning(
                "step.address_external_findings.detect_changes_unverifiable",
                reason="uncommitted-changes check failed; treating the worktree as unverified",
            )
        has_changes = check.has_changes

        post_log = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        post_count = len(post_log.stdout.strip().splitlines()) if post_log.success and post_log.stdout.strip() else 0
        new_commits = post_count > prev_count

        return has_changes, new_commits

    async def _apply_fixes(self, ctx: ExecutionContext, prompt: str) -> tuple[Decimal, bool, bool]:
        """Invoke the agent to fix external findings, commit, and push.

        Returns ``(cost, has_changes, new_commits)`` so the caller can gate
        outward-facing side effects (resolving external review threads) on
        actual evidence of edits rather than on whether the call was billed.

        If the dispatch itself fails, any edits the agent made before the
        failure are still committed and pushed (never discarded). When that
        push actually succeeds, raises :class:`_DispatchFailedButPreserved`
        instead of the bare dispatch error, so the caller can let the
        pipeline continue against the commit that just reached the PR branch.
        Otherwise re-raises the original ``RuntimeError`` so the caller
        reports a plain dispatch failure.
        """
        from sova.core.agent_dispatch import dispatch_prompt

        # Captured before dispatch: the agent runtime commits directly to the
        # worktree, so "previous" commit count must be read before it runs,
        # not after (text-only invoke() could never move HEAD, so this ordering
        # did not matter under the prior implementation).
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        prev_commits = log_result.stdout.strip() if log_result.success else ""
        prev_count = len(prev_commits.splitlines()) if prev_commits else 0

        try:
            result = await dispatch_prompt(
                self,
                prompt,
                model=ctx.resolved_model or ctx.config.agent.model,
                fallback_model=ctx.get_cli_fallback_model(),
                # Reuses "address_review" rather than a key of its own: both
                # steps address reviewer findings against an open PR, and a
                # second key would split one routing decision across two
                # config entries (same reasoning as develop.py's "extraction"
                # tag).
                task_type=ctx.routing_task_type("address_review"),
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
                ctx=ctx,
                timeout=ctx.config.agent.step_timeout,
            )
            ctx.add_usage(result)
        except RuntimeError as exc:
            log.warning("step.address_external_findings.llm_failed", exc_info=True)
            # Preservation is best effort and must never shadow the dispatch
            # error: _detect_changes() shells out, so a missing git binary or a
            # deleted cwd raises OSError, which is not a RuntimeError and would
            # otherwise escape past execute()'s handler and lose the real cause.
            persisted = False
            preserved_changes = preserved_commits = pushed = False
            try:
                preserved_changes, preserved_commits, pushed = await self._persist_changes(
                    ctx, prev_count, message=_PRESERVE_COMMIT_MESSAGE
                )
                persisted = True
            except Exception:  # noqa: BLE001 (best effort preservation; original error must win)
                log.warning("step.address_external_findings.edit_preservation_failed", exc_info=True)
            else:
                if preserved_changes or preserved_commits:
                    log.info(
                        "step.address_external_findings.edits_preserved",
                        uncommitted_edits=preserved_changes,
                        agent_commits=preserved_commits,
                        pushed=pushed,
                    )
                else:
                    log.info("step.address_external_findings.nothing_to_preserve")
            if persisted and pushed:
                raise _DispatchFailedButPreserved(exc) from exc
            raise

        has_changes, new_commits, _pushed = await self._persist_changes(
            ctx, prev_count, message=_FINDINGS_COMMIT_MESSAGE
        )

        return result.cost_usd, has_changes, new_commits

    async def _persist_changes(
        self, ctx: ExecutionContext, prev_count: int, *, message: str
    ) -> tuple[bool, bool, bool]:
        """Detect the agent's edits, commit any uncommitted ones under *message*, then push.

        Returns ``(has_changes, new_commits, pushed)``: the first two match
        :meth:`_detect_changes`'s tuple so the caller can gate outward-facing
        side effects (resolving external review threads) on it; ``pushed`` is
        ``True`` only when the push actually succeeded, so a caller deciding
        whether preserved work truly reached the PR branch (as opposed to a
        local-only commit a failed push left stranded) can tell the two apart.
        Pushes if the fallback commit above succeeded or the agent added
        commits of its own; the latter is a commit-count comparison, which
        cannot detect the agent amending, squashing, or rebasing its own
        history (HEAD moves without the count changing), a known gap rather
        than a guarantee.
        """
        from sova.git import operations as git_ops

        has_changes, new_commits = await self._detect_changes(ctx, prev_count)

        # Tracked separately from has_changes: a failed commit leaves the edits
        # in the worktree and HEAD where it was, so pushing on has_changes alone
        # would force-with-lease the remote back over whatever is already there
        # while publishing none of the work the push was meant to preserve.
        committed = False
        if has_changes:
            try:
                await git_ops.commit(
                    message,
                    # Excludes mirrored agent infrastructure the pipeline
                    # itself writes into the worktree: a bare `git add -A`
                    # would otherwise stage and commit it onto the PR branch.
                    files=[".", *EXCLUDED_PATHSPECS],
                    cwd=ctx.working_dir,
                )
                committed = True
            except RuntimeError:
                log.warning("step.address_external_findings.commit_failed", exc_info=True)

        pushed = False
        if committed or new_commits:
            try:
                await git_ops.push(
                    ctx.branch_name,
                    force=True,
                    set_upstream=True,
                    cwd=ctx.working_dir,
                    github_user=ctx.config.github_user,
                )
                pushed = True
            except RuntimeError:
                log.warning("step.address_external_findings.push_failed", exc_info=True)

        return has_changes, new_commits, pushed

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        ext = ctx.config.external_reviews
        if not ext.enabled or not ext.tools:
            return GateCheckResult(passed=True)
        # Same helper _detect_changes() uses, so the gate and the persistence
        # path cannot disagree about what counts as an uncommitted edit.
        check = await self._check_uncommitted_changes(ctx)
        if not check.checked:
            return GateCheckResult(passed=False, reason="Could not verify worktree state")
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_commits = bool(log_result.success and log_result.stdout.strip())
        if check.has_changes:
            return GateCheckResult(passed=False, reason="Uncommitted changes after addressing findings")
        if not has_commits:
            return GateCheckResult(passed=False, reason="No commits ahead of base after addressing findings")
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        if self.name in ctx.completed_steps:
            return True
        ext = ctx.config.external_reviews
        return not ext.enabled or not ext.tools
