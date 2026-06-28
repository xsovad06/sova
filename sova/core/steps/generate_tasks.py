"""Step: Generate Tasks -- invoke LLM to propose tasks from scan context.

Builds a prompt from the scan summary, calls the LLM, and parses
JSON output into a list of PlannedTask objects.
"""

from __future__ import annotations

import json

from sova.core.context import ExecutionContext
from sova.core.planning import PlannedTask
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.utils.logging import get_logger

log = get_logger(component="step.generate_tasks")

_GENERATE_PROMPT = """\
You are a project planning assistant. Based on the project scan below, \
propose 5-10 concrete, actionable tasks that would advance the project.

{scan_summary}

## Instructions

For each task, provide:
- "title": string (use conventional commit prefix, e.g. "feat(dashboard): ...")
- "body": string (full issue body in markdown with Acceptance Criteria section including checkboxes)
- "labels": list of strings (type:, priority:, area: labels)
- "priority": "critical" | "high" | "medium" | "low"
- "complexity": "small" | "medium" | "large" | "xlarge"
- "rationale": string (why this task matters, 1-2 sentences)

Rules:
- Each task must be specific and actionable (not vague "improve X")
- Each task body must include an "## Acceptance Criteria" section with "- [ ]" checkboxes
- Do not propose tasks that duplicate existing open issues
- Focus on gaps, technical debt, and next logical steps

Respond with a JSON array of task objects. No markdown fencing or extra text.
"""


def _extract_json(text: str) -> str:
    """Extract JSON from LLM response, stripping markdown fences if present."""
    cleaned = text.strip()
    # Strip markdown code fences using string ops (avoids ReDoS-prone regex)
    fence_start = cleaned.find("```")
    if fence_start == -1:
        return cleaned
    fence_end = cleaned.find("\n", fence_start)
    if fence_end == -1:
        return cleaned
    closing = cleaned.find("```", fence_end)
    if closing == -1:
        return cleaned
    return cleaned[fence_end + 1 : closing].strip()


def _parse_tasks(text: str) -> list[PlannedTask] | None:
    """Parse LLM response text into a list of PlannedTask."""
    try:
        raw = _extract_json(text)
        data = json.loads(raw)
        if not isinstance(data, list):
            log.warning("generate.parse_not_list")
            return None
        tasks = []
        for item in data:
            if not isinstance(item, dict):
                continue
            title = item.get("title", "")
            body = item.get("body", "")
            if not title or not title.strip():
                log.warning("generate.skip_item", reason="missing or blank title")
                continue
            if not body or not body.strip():
                log.warning("generate.skip_item", reason="missing or blank body")
                continue
            tasks.append(
                PlannedTask(
                    title=title,
                    body=body,
                    labels=item.get("labels", []),
                    priority=item.get("priority", "medium"),
                    complexity=item.get("complexity", "medium"),
                    rationale=item.get("rationale", ""),
                )
            )
        return tasks
    except (ValueError, KeyError) as exc:
        log.warning("generate.parse_failed", error=str(exc), exc_info=True)
        return None


class GenerateTasksStep(BaseStep):
    name = "generate_tasks"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.generate_tasks", project=str(ctx.project_dir))

        if ctx.plan_result is None or ctx.plan_result.scan is None:
            return StepResult(success=False, summary="No scan result available", error="Missing scan data")

        if ctx.is_budget_exceeded:
            return StepResult(success=False, summary="Budget exceeded", error="Budget limit reached")

        prompt = _GENERATE_PROMPT.format(scan_summary=ctx.plan_result.scan.raw_summary)

        try:
            from sova.llm.client import invoke

            result = await invoke(
                prompt,
                cwd=ctx.project_dir,
                max_budget_usd=ctx.config.agent.max_budget / 5,
                timeout=180,
            )
            ctx.add_cost(result.cost_usd)
        except Exception as exc:
            log.error("generate.llm_failed", error=str(exc), exc_info=True)
            return StepResult(success=False, summary="Task generation failed", error=str(exc))

        tasks = _parse_tasks(result.text)
        if tasks is None:
            return StepResult(success=False, summary="Failed to parse LLM response", error="JSON parse failure")

        ctx.plan_result.proposed_tasks = tasks
        log.info("generate.done", task_count=len(tasks))

        return StepResult(
            success=True,
            summary=f"Generated {len(tasks)} task proposals",
            cost_usd=result.cost_usd,
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        if ctx.plan_result is not None and len(ctx.plan_result.proposed_tasks) > 0:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="No tasks were generated")
