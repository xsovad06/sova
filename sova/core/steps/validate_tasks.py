"""Step: Validate Tasks -- filter proposed tasks for quality.

Runs rule-based checks: specificity, testability, duplicate detection
against open issues and self-deduplication. No LLM calls.
"""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.planning import PlannedTask
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.utils.logging import get_logger

log = get_logger(component="step.validate_tasks")

_MIN_VALID_TASKS = 3
_LOG_REJECTED = "validate.rejected"

_VAGUE_PREFIXES = (
    "improve ",
    "update ",
    "fix things",
    "clean up",
    "misc ",
    "various ",
)


def _word_set(text: str) -> set[str]:
    """Extract a set of lowercase words (3+ chars) from text."""
    return {w.lower() for w in text.split() if len(w) >= 3}


def _word_overlap(a: str, b: str) -> float:
    """Calculate word overlap ratio between two strings."""
    words_a = _word_set(a)
    words_b = _word_set(b)
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    smaller = min(len(words_a), len(words_b))
    return len(intersection) / smaller if smaller > 0 else 0.0


def _check_specificity(task: PlannedTask) -> str | None:
    """Check that the task title is specific and body is substantive."""
    title_lower = task.title.lower().strip()
    for prefix in _VAGUE_PREFIXES:
        if title_lower.startswith(prefix):
            return f"Vague title: '{task.title}' starts with '{prefix.strip()}'"
    if len(task.body) < 50:
        return f"Body too short ({len(task.body)} chars): '{task.title}'"
    return None


def _check_testability(task: PlannedTask) -> str | None:
    """Check that the task body contains acceptance criteria."""
    body = task.body.lower()
    if "acceptance criteria" in body or "- [ ]" in body or "- [x]" in body:
        return None
    return f"No acceptance criteria: '{task.title}'"


def _check_duplicate_against_issues(
    task: PlannedTask,
    open_issues: list[dict],
    threshold: float = 0.6,
) -> str | None:
    """Check if a task duplicates an existing open issue."""
    for issue in open_issues:
        issue_title = issue.get("title", "")
        if _word_overlap(task.title, issue_title) > threshold:
            return f"Duplicate of #{issue.get('number', '?')}: '{task.title}' overlaps with '{issue_title}'"
    return None


def _validate_task(
    task: PlannedTask, open_issues: list[dict], accepted_titles: list[str]
) -> str | None:
    """Run all checks on a single task. Return rejection reason or None if valid."""
    for check in (_check_specificity, _check_testability):
        reason = check(task)
        if reason:
            return reason

    reason = _check_duplicate_against_issues(task, open_issues)
    if reason:
        return reason

    for accepted in accepted_titles:
        if _word_overlap(task.title, accepted) > 0.6:
            return f"Self-duplicate: '{task.title}' overlaps with '{accepted}'"

    return None


class ValidateTasksStep(BaseStep):
    name = "validate_tasks"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.validate_tasks")

        if ctx.plan_result is None:
            return StepResult(success=False, summary="No plan result available", error="Missing plan data")

        proposed = ctx.plan_result.proposed_tasks
        open_issues = ctx.plan_result.scan.open_issues if ctx.plan_result.scan else []

        valid: list[PlannedTask] = []
        rejected: list[str] = []
        accepted_titles: list[str] = []

        for task in proposed:
            reason = _validate_task(task, open_issues, accepted_titles)
            if reason:
                rejected.append(reason)
                log.debug(_LOG_REJECTED, reason=reason)
                continue
            valid.append(task)
            accepted_titles.append(task.title)

        ctx.plan_result.valid_tasks = valid
        ctx.plan_result.rejected_reasons = rejected

        total = len(proposed)
        valid_count = len(valid)
        rejected_count = len(rejected)

        log.info("validate.done", valid=valid_count, rejected=rejected_count, total=total)

        if valid_count < _MIN_VALID_TASKS:
            reasons_summary = "; ".join(rejected[:5])
            return StepResult(
                success=False,
                summary=(
                    f"Only {valid_count}/{total} tasks passed validation "
                    f"(need {_MIN_VALID_TASKS}). Reasons: {reasons_summary}"
                ),
                error=f"Insufficient valid tasks: {valid_count} < {_MIN_VALID_TASKS}",
            )

        return StepResult(
            success=True,
            summary=f"Validated: {valid_count}/{total} tasks passed, {rejected_count} rejected",
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        if ctx.plan_result is not None and len(ctx.plan_result.valid_tasks) >= _MIN_VALID_TASKS:
            return GateCheckResult(passed=True)
        count = len(ctx.plan_result.valid_tasks) if ctx.plan_result else 0
        return GateCheckResult(
            passed=False,
            reason=f"Only {count} valid tasks (need {_MIN_VALID_TASKS})",
        )
