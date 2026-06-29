"""Planner role -- analyze project state and propose new tasks.

Works without an existing issue number, enabling issueless planning
agent runs. Gathers project context (open issues, vision, config),
invokes the LLM to propose tasks, and writes handoff with proposals.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.ipc.handoff import (
    AgentHandoff,
    DashboardHandoff,
    HandoffAction,
    write_handoff,
    write_handoff_file,
)
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.utils.logging import get_logger

log = get_logger(component="role.planner")


class PlannedTask(BaseModel):
    """A single task proposed by the planner."""

    title: str
    description: str
    priority: Literal["critical", "high", "medium", "low"]
    complexity: Literal["trivial", "simple", "moderate", "complex"]
    component: str
    rationale: str
    dependencies: list[str] = Field(default_factory=list)


_PLANNING_PROMPT = """\
You are a project planning assistant. Analyze the project state and propose \
new tasks that would advance the project toward its goals.

## Project
Repository: {repo}
Base branch: {base_branch}
Task source: {task_source}

## Open Issues
{open_issues}

## Project Vision
{vision}

## Instructions

Based on the open issues and project vision, propose new tasks that:
1. Fill gaps not covered by existing issues
2. Address technical debt or maintenance needs
3. Advance the project toward its stated goals
4. Are concrete and actionable (not vague epics)

Respond with a JSON array of task objects. Each object has these fields:
- "title": string (use conventional commit prefix, e.g. "feat(dashboard): ...")
- "description": string (full issue body in markdown)
- "priority": "critical" | "high" | "medium" | "low"
- "complexity": "trivial" | "simple" | "moderate" | "complex"
- "component": string (area label, e.g. "dashboard", "cli", "orchestrator")
- "rationale": string (why this task matters now)
- "dependencies": list of strings (issue numbers like "#123", empty if none)

Return ONLY the JSON array, no markdown fencing or extra text.
If no new tasks are needed, return an empty array: []
"""


class PlannerRole(AgentRole):
    name = "planner"
    description = "Analyze project and propose new tasks"
    allowed_input_states: frozenset[TaskState] = frozenset()
    output_state: TaskState | None = None

    def validate_preconditions(self, task: Task, force: bool = False) -> bool:
        return True

    async def assess_task(self, task: Task) -> TaskAssessment:
        return TaskAssessment(
            suitability="ready",
            confidence=0.9,
            reasoning="Planner role is always ready to analyze the project.",
            estimated_complexity="moderate",
            suggested_role="planner",
        )

    async def execute(self, ctx: ExecutionContext) -> RoleResult:
        log.info("planner.start", project=ctx.project_dir.name)

        # Gather context
        open_issues = await self._gather_open_issues(ctx)
        vision = self._read_vision(ctx)

        # Build prompt
        valid_issues = [t for t in open_issues if hasattr(t, "id") and hasattr(t, "title") and hasattr(t, "state")]
        issue_lines = "\n".join(f"- #{t.id}: {t.title} [{t.state}]" for t in valid_issues) or "(no open issues)"

        prompt = _PLANNING_PROMPT.format(
            repo=ctx.config.github_repo or ctx.project_dir.name,
            base_branch=ctx.config.base_branch,
            task_source=ctx.config.task_source.type,
            open_issues=issue_lines,
            vision=vision or "(no VISION.md found)",
        )

        # Invoke LLM
        try:
            from sova.llm.client import invoke

            result = await invoke(
                prompt,
                cwd=ctx.project_dir,
                max_budget_usd=ctx.config.agent.max_budget / 10,
                timeout=180,
            )
            ctx.add_cost(result.cost_usd)
        except Exception as exc:
            log.error("planner.llm_failed", error=str(exc))
            return RoleResult(
                success=False,
                summary="Planner failed: LLM invocation error",
                error=str(exc),
            )

        # Parse response
        tasks = self._parse_response(result.text)
        if tasks is None:
            return RoleResult(
                success=False,
                summary="Planner failed: could not parse LLM response as task list",
                error="JSON parse failure",
            )

        # Write handoffs
        findings = [f"{t.priority}: {t.title}" for t in tasks]
        await self._write_handoff(ctx, tasks)

        summary = f"Proposed {len(tasks)} tasks" if tasks else "No tasks proposed"
        log.info("planner.done", task_count=len(tasks))
        return RoleResult(
            success=True,
            summary=summary,
            findings=findings,
        )

    async def _gather_open_issues(self, ctx: ExecutionContext) -> list[Task]:
        try:
            return await ctx.adapter.list_tasks()
        except Exception as exc:
            log.warning("planner.list_tasks_failed", error=str(exc))
            return []

    def _read_vision(self, ctx: ExecutionContext) -> str:
        vision_path = ctx.project_dir / "docs" / "VISION.md"
        if vision_path.exists():
            try:
                return vision_path.read_text()
            except OSError:
                return ""
        return ""

    def _parse_response(self, text: str) -> list[PlannedTask] | None:
        try:
            cleaned = text.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            data = json.loads(cleaned)
            if not isinstance(data, list):
                log.warning("planner.parse_not_list")
                return None
            return [PlannedTask.model_validate(item) for item in data]
        except (ValueError, KeyError, ValidationError) as exc:
            log.warning("planner.parse_failed", error=str(exc), exc_info=True)
            return None

    async def _write_handoff(self, ctx: ExecutionContext, tasks: list[PlannedTask]) -> None:
        task_dicts = [t.model_dump() for t in tasks]

        # File-based handoff for dashboard
        # Planners are issueless; use "planner" as the issue identifier
        # so the file is named handoff-planner.json (not the shared legacy
        # handoff.json), preserving per-issue isolation for parallel agents.
        next_actions: list[HandoffAction] = []
        if tasks:
            next_actions.append(
                HandoffAction(
                    id="create-issues",
                    label="Create Issues",
                    description=f"Create {len(tasks)} proposed issues on the tracker",
                    style="approve",
                    mode="claude-command",
                    command="",
                )
            )

        dashboard_handoff = DashboardHandoff(
            source="planner",
            status="awaiting_action" if tasks else "completed",
            issue=ctx.issue_number or "planner",
            summary=f"Proposed {len(tasks)} tasks",
            details={"planned_tasks": task_dicts},
            next_actions=next_actions,
        )
        try:
            write_handoff_file(ctx.project_dir, dashboard_handoff)
        except Exception as exc:
            log.warning("planner.file_handoff_failed", error=str(exc))

        # DB-backed handoff for history
        if ctx.task_run_id:
            agent_handoff = AgentHandoff(
                role="planner",
                phase="planning",
                summary=f"Proposed {len(tasks)} tasks",
                next_action="review_proposals",
                branch_name="",
            )
            try:
                await write_handoff(ctx.task_run_id, agent_handoff)
            except Exception as exc:
                log.warning("planner.db_handoff_failed", error=str(exc))
