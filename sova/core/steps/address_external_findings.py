"""Step 12: Address external findings -- fetch and fix issues from external review tools."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, TypedDict

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
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


class AddressExternalFindingsStep(BaseStep):
    name = "address_external_findings"
    max_retries = 0

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
        cost = await self._apply_fixes(ctx, prompt)

        if coderabbit_thread_ids and cost > 0:
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
            log.info("step.address_external_findings.threads_not_resolved", reason="llm_failed")

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

    async def _apply_fixes(self, ctx: ExecutionContext, prompt: str) -> Decimal:
        """Invoke the LLM to fix external findings, commit, and push."""
        from sova.git import operations as git_ops
        from sova.llm.client import invoke

        try:
            result = await invoke(
                prompt,
                model=ctx.resolved_model or ctx.config.agent.model,
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
            )
            ctx.add_cost(result.cost_usd)
        except RuntimeError:
            log.warning("step.address_external_findings.llm_failed", exc_info=True)
            return Decimal("0")

        diff_result = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)

        has_changes = (diff_result.success and diff_result.stdout.strip()) or (staged.success and staged.stdout.strip())

        prev_commits = log_result.stdout.strip() if log_result.success else ""
        prev_count = len(prev_commits.splitlines()) if prev_commits else 0

        if has_changes:
            try:
                await git_ops.commit(
                    "fix: address external review findings",
                    cwd=ctx.working_dir,
                )
            except RuntimeError:
                log.warning("step.address_external_findings.commit_failed", exc_info=True)

        post_log = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        post_count = len(post_log.stdout.strip().splitlines()) if post_log.success and post_log.stdout.strip() else 0
        new_commits = post_count > prev_count

        if has_changes or new_commits:
            try:
                await git_ops.push(
                    ctx.branch_name,
                    force=True,
                    set_upstream=True,
                    cwd=ctx.working_dir,
                )
            except RuntimeError:
                log.warning("step.address_external_findings.push_failed", exc_info=True)

        return result.cost_usd

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        ext = ctx.config.external_reviews
        if not ext.enabled or not ext.tools:
            return GateCheckResult(passed=True)
        diff_result = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_uncommitted = (diff_result.success and diff_result.stdout.strip()) or (
            staged.success and staged.stdout.strip()
        )
        has_commits = bool(log_result.success and log_result.stdout.strip())
        if has_uncommitted:
            return GateCheckResult(passed=False, reason="Uncommitted changes after addressing findings")
        if not has_commits:
            return GateCheckResult(passed=False, reason="No commits ahead of base after addressing findings")
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        if self.name in ctx.completed_steps:
            return True
        ext = ctx.config.external_reviews
        return not ext.enabled or not ext.tools
