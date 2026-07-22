"""Step: Address review -- fix review findings from the Reviewer agent."""

from __future__ import annotations

import re
from pathlib import Path

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.ipc.handoff import read_handoff, read_handoff_file
from sova.llm.client import invoke_command
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.address_review")


def _load_review_findings(project_dir: Path, issue: str = "") -> list[dict]:
    """Load review findings from the reviewer's handoff file."""
    handoff = read_handoff_file(project_dir, issue=issue or None)
    if handoff is None:
        return []
    # "pending_findings" is the canonical key written by ReviewerRole.
    # "findings" is kept as a legacy fallback for older handoff files.
    return handoff.details.get("pending_findings") or handoff.details.get("findings", [])


async def _load_review_findings_from_db(task_run_id: int | None) -> list[dict]:
    """Load review findings from a specific run's handoff in DB."""
    if task_run_id is None:
        return []
    try:
        handoff = await read_handoff(task_run_id)
        if handoff and handoff.pending_findings:
            return handoff.pending_findings
    except Exception:
        log.debug("address_review.db_findings_failed", exc_info=True)
    return []


async def _load_review_findings_by_issue(issue_number: str) -> list[dict]:
    """Load findings from the most recent reviewer run for this issue."""
    issue = issue_number.lstrip("#").strip()
    if not issue:
        return []

    from sqlalchemy import select

    from sova.db.models import TaskRun
    from sova.db.session import get_session

    try:
        async with await get_session() as session:
            stmt = (
                select(TaskRun)
                .where(
                    TaskRun.issue_number == issue,
                    TaskRun.role.in_(["reviewer", "command:review-pr"]),
                    TaskRun.status.in_(["done", "failed", "interrupted"]),
                    TaskRun.handoff_json.isnot(None),
                )
                .order_by(TaskRun.started_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            run_record = result.scalar_one_or_none()
            if run_record and run_record.handoff_json:
                findings = run_record.handoff_json.get("pending_findings", [])
                if findings:
                    log.info("address_review.findings_from_reviewer", run_id=run_record.id, count=len(findings))
                    return findings
    except Exception:
        log.debug("address_review.issue_findings_failed", exc_info=True)
    return []


_SEVERITY_MAP = {"CRITICAL": 10, "HIGH": 9, "MEDIUM": 6, "LOW": 3}

_FINDING_HEADER_RE = re.compile(r"\*{0,2}\[(\w+)\]\s*([\w][\w ]*?)\s*--\s*(.*?)\*{0,2}\s*$", re.MULTILINE)
_LOCATION_RE = re.compile(r"Location:\s*(\S+?)(?:\n|$)")
_PROBLEM_RE = re.compile(r"Problem:\s*(.*?)(?:\nSuggestion:|\Z)", re.DOTALL)
_SUGGESTION_RE = re.compile(r"Suggestion:\s*(.*?)(?:\n\[|```|\Z)", re.DOTALL)


def _parse_review_body(body: str) -> list[dict]:
    """Parse structured findings from a single review body.

    Expected format: ``[HIGH/MEDIUM/LOW] Category -- Description``
    with optional ``Location:``, ``Problem:``, and ``Suggestion:`` fields.

    Falls back to treating the whole body as a single finding when
    no structured findings are found and the body is non-trivial.
    """
    structured: list[dict] = []
    blocks = re.split(r"\n(?=\*{0,2}\[(?:CRITICAL|HIGH|MEDIUM|LOW)\])", body)
    for block in blocks:
        m = _FINDING_HEADER_RE.match(block.strip())
        if not m:
            continue

        severity = _SEVERITY_MAP.get(m.group(1).upper(), 5)
        file_path = ""
        line = None

        loc_m = _LOCATION_RE.search(block)
        if loc_m:
            loc = loc_m.group(1)
            if ":" in loc:
                parts = loc.rsplit(":", 1)
                file_path = parts[0]
                try:
                    line = int(parts[1])
                except ValueError:
                    file_path = parts[0]
            else:
                file_path = loc

        problem_m = _PROBLEM_RE.search(block)
        suggestion_m = _SUGGESTION_RE.search(block)

        structured.append(
            {
                "file": file_path,
                "line": line,
                "severity": severity,
                "category": m.group(2).lower(),
                "description": problem_m.group(1).strip() if problem_m else m.group(3),
                "suggestion": suggestion_m.group(1).strip() if suggestion_m else "",
                "source": "github-review",
            }
        )

    if structured:
        return structured

    if len(body) > 50:
        return [
            {
                "file": "",
                "line": None,
                "severity": 7,
                "category": "review",
                "description": body[:3000],
                "suggestion": "",
                "source": "github-review",
            }
        ]
    return []


async def _load_findings_from_github_reviews(ctx: ExecutionContext) -> list[dict]:
    """Fetch review findings from GitHub PR review bodies.

    Calls ``gh api`` directly (bypassing the adapter) so this works for
    both GitHub-Issues and JIRA task sources -- PRs always live on GitHub.
    """
    if not ctx.pr_number:
        return []
    try:
        import json as _json

        from sova.utils.gh import resolve_gh_env

        env = await resolve_gh_env(ctx.config.github_user)
        result = await run(
            "gh",
            "api",
            f"repos/{ctx.repo}/pulls/{ctx.pr_number}/reviews",
            "--paginate",
            cwd=ctx.working_dir,
            env=env,
        )
        if not result.success or not result.stdout.strip():
            return []

        raw_reviews = _json.loads(result.stdout)
        if not isinstance(raw_reviews, list):
            return []

        findings: list[dict] = []
        for r in raw_reviews:
            if not isinstance(r, dict):
                continue
            state = r.get("state", "")
            body = r.get("body", "") or ""
            is_bot = r.get("user", {}).get("type", "") == "Bot"
            if state == "DISMISSED" or is_bot or not body.strip():
                continue
            findings.extend(_parse_review_body(body))

        if findings:
            log.info("address_review.github_review_findings", count=len(findings))
        return findings
    except Exception:
        log.warning("address_review.github_review_fetch_failed", exc_info=True)
        return []


async def _load_coderabbit_findings(ctx: ExecutionContext) -> tuple[list[dict], list[str]]:
    """Fetch unresolved CodeRabbit findings from the PR.

    Returns (findings_as_dicts, thread_ids).
    """
    if not ctx.pr_number:
        return [], []
    try:
        from sova.adapters.external_reviews import _fetch_coderabbit_threads

        cr_result = await _fetch_coderabbit_threads(
            ctx.repo,
            ctx.pr_number,
            github_user=ctx.config.github_user,
        )
        findings = [
            {
                "file": f.file_path,
                "line": f.line,
                "severity": 6,
                "category": "external-review",
                "description": f.message,
                "suggestion": "",
                "source": "coderabbit",
            }
            for f in cr_result.findings
        ]
        if findings:
            log.info("address_review.coderabbit_findings", count=len(findings))
        return findings, cr_result.thread_ids
    except Exception:
        log.warning("address_review.coderabbit_fetch_failed", exc_info=True)
        return [], []


def _load_spec_for_context(ctx: ExecutionContext) -> str:
    """Load spec decision context for the address-review prompt."""
    try:
        from sova.core.steps._spec_helpers import REVIEW_CONTEXT_SECTIONS, read_spec_sections

        return read_spec_sections(ctx.issue_number, ctx.project_dir, REVIEW_CONTEXT_SECTIONS)
    except Exception:
        log.debug("address_review.spec_context_failed", exc_info=True)
        return ""


def _format_findings_prompt(findings: list[dict], *, spec_context: str = "") -> str:
    """Format findings into a prompt for the LLM to address."""
    lines = []
    if spec_context:
        lines.extend(
            [
                "## Decision Context (from spec)",
                "Use this context to understand WHY previous agents made specific choices.",
                spec_context,
                "",
            ]
        )
    lines.extend(
        [
            "Address ALL of the following code review findings. For each finding:",
            "- DEFAULT: Fix the issue in the code.",
            "- EXCEPTION: If a finding is a false positive, not applicable in context,",
            "  or requires a human decision, state the reason instead of fixing.",
            "  Do NOT skip findings without justification.\n",
        ]
    )
    for i, f in enumerate(findings, 1):
        loc = f.get("file", "unknown")
        if f.get("line"):
            loc += f":{f['line']}"
        source_tag = f" [from {f['source']}]" if f.get("source") else ""
        lines.append(f"{i}. [{f.get('severity', '?')}/10] [{f.get('category', 'other')}] `{loc}`{source_tag}")
        lines.append(f"   {f.get('description', '')}")
        if f.get("suggestion"):
            lines.append(f"   Fix: {f['suggestion']}")
        lines.append("")
    lines.append("After fixing all issues, make sure all tests still pass.")
    return "\n".join(lines)


class AddressReviewStep(BaseStep):
    name = "address_review"

    def __init__(self) -> None:
        super().__init__()
        self._head_before_llm: str | None = None

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.address_review", pr=ctx.pr_number)

        # Capture HEAD before LLM invocation for gate check
        head_result = await run("git", "rev-parse", "HEAD", cwd=ctx.working_dir)
        if head_result.success:
            self._head_before_llm = head_result.stdout.strip()

        # Load findings: file -> resumed run -> most recent reviewer for this issue
        findings = _load_review_findings(ctx.project_dir, issue=ctx.issue_number)
        if not findings:
            findings = await _load_review_findings_from_db(ctx.resume_run_id)
        if not findings:
            findings = await _load_review_findings_by_issue(ctx.issue_number)

        # Fetch findings from GitHub PR review bodies (covers reviews not
        # posted via Sova's own handoff mechanism).
        if not findings:
            findings = await _load_findings_from_github_reviews(ctx)

        # Also fetch CodeRabbit findings from the PR
        cr_findings, _thread_ids = await _load_coderabbit_findings(ctx)
        if cr_findings:
            findings.extend(cr_findings)

        if not findings:
            log.info("step.address_review.no_findings")
            return StepResult(success=True, summary="No review findings to address")

        log.info("step.address_review.findings_loaded", count=len(findings))

        spec_context = _load_spec_for_context(ctx)
        if spec_context:
            log.info(
                "step.address_review.spec_compression",
                spec_context_chars=len(spec_context),
                findings_count=len(findings),
            )
        prompt = _format_findings_prompt(findings, spec_context=spec_context)
        try:
            result = await invoke_command(
                prompt,
                model=ctx.config.agent.model,
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
                timeout=ctx.config.agent.step_timeout,
            )
            ctx.add_cost(result.cost_usd)
            return StepResult(
                success=True,
                summary=f"Addressed {len(findings)} review findings",
                cost_usd=result.cost_usd,
            )
        except RuntimeError as exc:
            return StepResult(success=False, summary="Failed to address review findings", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: the LLM must have produced new changes, commits, or confirmed prior fixes.

        Three passing conditions:
        1. Uncommitted changes exist (LLM modified files)
        2. HEAD moved (LLM committed directly)
        3. Branch is already ahead of base (findings were fixed in prior runs)
        """
        diff_result = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        has_uncommitted = bool(
            (diff_result.success and diff_result.stdout.strip()) or (staged.success and staged.stdout.strip())
        )

        head_result = await run("git", "rev-parse", "HEAD", cwd=ctx.working_dir)
        head_after = head_result.stdout.strip() if head_result.success else ""
        head_moved = self._head_before_llm is not None and head_after != self._head_before_llm

        if has_uncommitted or head_moved:
            return GateCheckResult(passed=True)

        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_prior_commits = bool(log_result.success and log_result.stdout.strip())
        if has_prior_commits:
            log.info("step.address_review.findings_already_fixed")
            return GateCheckResult(passed=True)

        return GateCheckResult(passed=False, reason="No changes after addressing review findings")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps
