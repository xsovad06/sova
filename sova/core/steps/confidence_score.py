"""Step: Confidence Score (LLM-generated deployment risk score for the PR diff).

Advisory by default (``confidence.gate_enabled=False``): the score is computed
and persisted but does not influence handoff routing. Never blocks the
pipeline: any failure (LLM error, budget exhausted, malformed response)
degrades gracefully to "scoring skipped".
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

from sova.core.context import BUDGET_SKIP_OPTIONAL_THRESHOLD, ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git.operations import get_pr_body, update_pr_body
from sova.llm.client import invoke
from sova.utils.json import extract_json
from sova.utils.logging import get_logger
from sova.utils.markdown import upsert_section
from sova.utils.shell import run

log = get_logger(component="step.confidence_score")

_UNAVAILABLE = "(unavailable)"
_DIFF_CONTENT_LIMIT = 6000

# Higher-risk paths are prioritized when the diff must be truncated; test and
# doc changes are truncated first.
_CRITICAL_PATH_RE = re.compile(
    r"(migrat|alembic|security|\bauth|credential|permission|schema|\.github/workflows|dockerfile|docker-compose)",
    re.IGNORECASE,
)
_LOW_PRIORITY_PATH_RE = re.compile(r"(^|/)(tests?|docs?)/|(^|/)test_|_test\.py$|\.md$", re.IGNORECASE)

# LLM prose is embedded verbatim in the PR body, where two constructs are not
# inert: a "## " line becomes a section boundary that breaks upsert_section's
# in-place replacement (orphaning stale content on every re-run), and a bare
# "#123" next to a closing keyword makes GitHub close an unrelated issue when
# the PR merges.
_MD_HEADING_RE = re.compile(r"^(#{1,6})(?= )", re.MULTILINE)
_ISSUE_REF_RE = re.compile(r"(?<![`\w])#(\d+)")

_CONFIDENCE_PROMPT = """\
Analyze this pull request's diff and produce a deployment confidence score \
from 0 (highest risk) to 100 (lowest risk, safe to auto-merge).

Consider:
- Compound risk: do multiple risky changes combine (e.g. a schema change \
alongside an auth/permission change) to create a deployment ordering hazard?
- Blast radius: how many files/modules are affected, and how central are they?
- Change category: migrations, security/auth, API contracts, and CI/config \
changes are higher risk than tests, docs, or comments.
- Testability: are the changes covered by the diff itself (new/updated tests)?

Task: {task_title}

Diff stat:
{diff_stat}

Diff (may be truncated; higher-risk files are prioritized):
{diff_content}

Return JSON with:
- score: integer 0-100
- risks: array of objects with "category", "severity" \
("critical"|"important"|"follow-up"), and "description"
- summary: one paragraph explaining the score

Output ONLY the JSON object, no markdown fences, no commentary.
"""

_PR_BODY_HEADING = "Confidence Score"


def _file_priority(path: str) -> int:
    """Lower is higher priority (kept whole first when truncating).

    Tests and docs are matched before the critical-path patterns on purpose:
    ``tests/test_migration.py`` and ``docs/security-guidelines.md`` both hit
    ``_CRITICAL_PATH_RE`` while carrying none of the deployment risk it exists
    to catch, and promoting them to priority 0 spends the diff budget before
    any real source file is seen.
    """
    if _LOW_PRIORITY_PATH_RE.search(path):
        return 2
    if _CRITICAL_PATH_RE.search(path):
        return 0
    return 1


def _sanitize_for_pr_body(text: str) -> str:
    """Neutralize LLM prose that GitHub or upsert_section would act on."""
    return _ISSUE_REF_RE.sub(r"`#\1`", _MD_HEADING_RE.sub(r"\\\1", text))


def _clamp_score(value: object) -> int:
    try:
        score = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, score))


def _parse_risks(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    risks = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        risks.append(
            {
                "category": str(item.get("category", "other")),
                "severity": str(item.get("severity", "follow-up")),
                "description": str(item.get("description", "")),
            }
        )
    return risks


class ConfidenceScoreStep(BaseStep):
    name = "confidence_score"
    TASK_TYPE = "confidence_score"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.confidence_score", pr=ctx.pr_number)

        diff_range = f"{ctx.base_branch}..HEAD"
        files_result = await run("git", "diff", diff_range, "--name-only", cwd=ctx.working_dir)
        if not files_result.success:
            log.warning("step.confidence_score.diff_names_failed", stderr=files_result.stderr[:200])
            return StepResult(success=True, summary="Confidence scoring skipped: could not determine changed files")

        files = [f for f in files_result.stdout.strip().splitlines() if f.strip()]

        if not files:
            ctx.confidence_score = 100
            ctx.confidence_details = {"risks": [], "summary": "No changes to score."}
            await self._update_pr_body(ctx)
            return StepResult(success=True, summary="Confidence score: 100/100 (no changes)")

        budget_remaining = ctx.config.agent.max_budget - ctx.cost_usd
        max_budget = min(ctx.config.confidence.max_budget_usd, budget_remaining)
        if max_budget <= Decimal("0"):
            log.warning("step.confidence_score.budget_exhausted")
            return StepResult(success=True, summary="Confidence scoring skipped: budget exhausted")

        diff_stat_result = await run("git", "diff", diff_range, "--stat", cwd=ctx.working_dir)
        diff_stat = diff_stat_result.stdout.strip() if diff_stat_result.success else _UNAVAILABLE
        diff_content = await self._build_diff_content(ctx, diff_range, files)
        if diff_content is None:
            log.warning("step.confidence_score.diff_content_failed")
            return StepResult(success=True, summary="Confidence scoring skipped: could not build diff content")

        task_title = ctx.task.title if ctx.task else ctx.display_label
        prompt = _CONFIDENCE_PROMPT.format(task_title=task_title, diff_stat=diff_stat, diff_content=diff_content)

        try:
            result = await invoke(
                prompt,
                model=ctx.config.confidence.model,
                task_type=ctx.routing_task_type(self.TASK_TYPE),
                cwd=ctx.working_dir,
                max_budget_usd=max_budget,
                timeout=ctx.config.agent.step_timeout,
            )
        except Exception as exc:
            log.warning("step.confidence_score.llm_failed", exc_info=True)
            return StepResult(success=True, summary=f"Confidence scoring skipped (non-fatal): {exc}")

        ctx.add_usage(result)

        try:
            data = json.loads(extract_json(result.text))
            if not isinstance(data, dict):
                raise TypeError("confidence response is not a JSON object")
        except (TypeError, ValueError):
            log.warning("step.confidence_score.parse_failed", text_preview=result.text[:200])
            return StepResult(
                success=True,
                summary="Confidence scoring skipped: could not parse LLM response",
                cost_usd=result.cost_usd,
            )

        raw_score = data.get("score")
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            log.warning("step.confidence_score.invalid_score", raw_score=raw_score)
            return StepResult(
                success=True,
                summary="Confidence scoring skipped: LLM response missing a valid score",
                cost_usd=result.cost_usd,
            )

        score = _clamp_score(raw_score)
        risks = _parse_risks(data.get("risks"))
        summary = str(data.get("summary", ""))

        ctx.confidence_score = score
        ctx.confidence_details = {"risks": risks, "summary": summary}

        await self._update_pr_body(ctx)

        return StepResult(
            success=True,
            summary=f"Confidence score: {score}/100 ({len(risks)} risk(s))",
            cost_usd=result.cost_usd,
        )

    @staticmethod
    async def _build_diff_content(ctx: ExecutionContext, diff_range: str, files: list[str]) -> str | None:
        """Build a truncated diff, prioritizing higher-risk files when over budget.

        Returns ``None`` if any per-priority ``git diff`` command fails, so the
        caller can fail closed rather than score an incomplete diff.
        """
        grouped: dict[int, list[str]] = {0: [], 1: [], 2: []}
        for f in files:
            grouped[_file_priority(f)].append(f)

        sections: list[str] = []
        budget = _DIFF_CONTENT_LIMIT
        for priority in (0, 1, 2):
            group_files = grouped[priority]
            if not group_files or budget <= 0:
                continue
            result = await run("git", "diff", diff_range, "--", *group_files, cwd=ctx.working_dir)
            if not result.success:
                return None
            if not result.stdout:
                continue
            chunk = result.stdout
            if len(chunk) > budget:
                chunk = chunk[:budget] + "\n\n... (diff truncated at the size limit)"
            sections.append(chunk)
            budget -= len(chunk)

        return "\n".join(sections) if sections else "(no changes detected)"

    async def _update_pr_body(self, ctx: ExecutionContext) -> None:
        """Embed the confidence score in the PR body. Non-fatal on failure."""
        if not ctx.pr_number:
            return
        try:
            body = await get_pr_body(ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user)
            new_body = upsert_section(body, _PR_BODY_HEADING, self._render_pr_section(ctx))
            await update_pr_body(ctx.pr_number, body=new_body, repo=ctx.repo, github_user=ctx.config.github_user)
        except Exception:
            log.warning("step.confidence_score.pr_body_update_failed", pr=ctx.pr_number, exc_info=True)

    @staticmethod
    def _render_pr_section(ctx: ExecutionContext) -> str:
        score = ctx.confidence_score
        details = ctx.confidence_details or {}
        lines = [f"<!-- sova-confidence: {score} -->", "", f"**{score}/100**"]
        summary = details.get("summary")
        if summary:
            lines.append("")
            lines.append(_sanitize_for_pr_body(summary))
        risks = details.get("risks") or []
        if risks:
            lines.append("")
            for risk in risks:
                entry = f"- [{risk['severity']}] {risk['category']}: {risk['description']}"
                lines.append(_sanitize_for_pr_body(entry).replace("\n", " "))
        return "\n".join(lines)

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        if self.name in ctx.completed_steps or not ctx.config.confidence.enabled:
            return True
        if ctx.budget_remaining_fraction < BUDGET_SKIP_OPTIONAL_THRESHOLD:
            log.warning("step.confidence_score.budget_skip", fraction=ctx.budget_remaining_fraction)
            return True
        return False
