"""Step: Spec -- generate structured specification before development.

Produces a .claude/specs/{issue}-{slug}.md specification document
that guides the developer with concrete implementation plans,
design decisions, and scope boundaries.

Behavior depends on complexity threshold and open questions:
- Tasks below spec.threshold skip this step entirely
- Simple specs with no open questions: auto-approve, continue pipeline
- Complex specs or open questions: write handoff, pause for dashboard approval
"""

from __future__ import annotations

import re

from sova.core.context import ExecutionContext
from sova.core.steps._handoff_helpers import write_step_handoff
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.dashboard.services.spec_service import find_spec_file
from sova.ipc.handoff import HandoffAction
from sova.llm.client import invoke_command
from sova.utils.logging import get_logger

log = get_logger(component="step.spec")

_COMPLEXITY_ORDER = ("always", "trivial", "simple", "moderate", "complex", "never")


def _complexity_rank(level: str) -> int:
    """Return numeric rank for a complexity level (lower = simpler)."""
    try:
        return _COMPLEXITY_ORDER.index(level.lower())
    except ValueError:
        return _COMPLEXITY_ORDER.index("moderate")


def _extract_complexity(text: str) -> str:
    """Extract complexity rating from markdown text containing **Complexity**: value."""
    match = re.search(r"\*\*Complexity\*\*:\s*(\w+)", text, re.IGNORECASE)
    return match.group(1).lower() if match else "moderate"


def _extract_section(text: str, heading: str) -> str:
    """Extract the content of a markdown section (between ## heading and next ## or EOF)."""
    pattern = rf"^## {re.escape(heading)}\s*$"
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        return ""
    start = match.end()
    # Find the next ## heading or end of text
    next_heading = re.search(r"^## ", text[start:], re.MULTILINE)
    section = text[start : start + next_heading.start()] if next_heading else text[start:]
    return section.strip()


def _text_has_open_questions(text: str) -> bool:
    """Check if text contains an Open Questions section with content."""
    content = _extract_section(text, "Open Questions")
    normalized = content.strip().lower() if content else ""
    if not normalized or normalized.startswith("(omit") or normalized == "none":
        return False
    return True


class SpecStep(BaseStep):
    name = "spec"

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        if self.name in ctx.completed_steps:
            return True

        threshold = ctx.config.spec.threshold
        if threshold == "never":
            return True

        if threshold == "always":
            return False

        # Check task complexity from triage assessment
        task = ctx.task
        if task is None:
            task = await ctx.adapter.get_task(ctx.issue_number)
        body = task.body or ""
        task_complexity = _extract_complexity(body)
        threshold_rank = _complexity_rank(threshold)
        task_rank = _complexity_rank(task_complexity)

        # Skip if task complexity is below threshold
        return task_rank < threshold_rank

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.spec", issue=ctx.issue_number, cwd=str(ctx.project_dir))

        try:
            result = await invoke_command(
                "/spec",
                args=ctx.issue_number,
                model=ctx.config.roles.researcher_model or ctx.config.agent.model,
                cwd=ctx.project_dir,
                max_budget_usd=ctx.config.agent.max_budget / 5,
                timeout=ctx.config.agent.step_timeout,
            )
            ctx.add_cost(result.cost_usd)
        except RuntimeError as exc:
            return StepResult(success=False, summary="Spec generation failed", error=str(exc))

        spec_path = find_spec_file(ctx.issue_number, project_dir=ctx.project_dir)
        if spec_path is None:
            return StepResult(
                success=False,
                summary="Spec command ran but no spec file was produced",
                error="Expected .claude/specs/{issue}-*.md",
            )

        # Read once, derive everything from text
        text = spec_path.read_text()
        has_questions = _text_has_open_questions(text)
        spec_complexity = _extract_complexity(text)

        # Auto-approve simple specs without open questions
        if ctx.config.spec.auto_approve_simple and not has_questions:
            if _complexity_rank(spec_complexity) <= _complexity_rank("simple"):
                # Mark as approved in the spec file
                updated = re.sub(r"\*\*Status\*\*:\s*\w+", "**Status**: approved", text, count=1)
                if updated == text:
                    return StepResult(
                        success=False,
                        summary="Auto-approval failed: status line not found in spec",
                        error="Could not find **Status**: <value> pattern in spec file",
                    )
                try:
                    spec_path.write_text(updated)
                except IOError as exc:
                    return StepResult(
                        success=False,
                        summary="Failed to update spec file",
                        error=str(exc),
                    )
                return StepResult(
                    success=True,
                    summary=f"Spec auto-approved (complexity: {spec_complexity}, no open questions)",
                    cost_usd=result.cost_usd,
                )

        # Needs human review -- write handoff and pause pipeline
        return await self._write_approval_handoff(ctx, spec_complexity, has_questions)

    async def _write_approval_handoff(
        self,
        ctx: ExecutionContext,
        complexity: str,
        has_questions: bool,
    ) -> StepResult:
        """Write a handoff requesting spec approval from the dashboard."""
        reason = "open questions" if has_questions else f"complexity: {complexity}"

        return await write_step_handoff(
            ctx,
            role="researcher",
            phase="spec",
            summary=f"Spec for #{ctx.issue_number} needs review ({reason})",
            agent_summary=f"Spec generated, awaiting approval ({reason})",
            next_action="approve-spec",
            actions=[
                HandoffAction(
                    id="approve-spec",
                    label="Approve Spec",
                    description="Accept the spec and proceed to development",
                    style="approve",
                    mode="agent",
                    command="",
                    args={"issue": ctx.issue_number, "role": "developer"},
                    auto_execute=False,
                ),
                HandoffAction(
                    id="revise-spec",
                    label="Revise",
                    description="Re-run spec generation with feedback",
                    style="neutral",
                    mode="agent",
                    command="",
                    args={"issue": ctx.issue_number, "role": "researcher"},
                    auto_execute=False,
                ),
                HandoffAction(
                    id="skip-spec",
                    label="Skip Spec",
                    description="Proceed to development without spec guidance",
                    style="neutral",
                    mode="agent",
                    command="",
                    args={"issue": ctx.issue_number, "role": "developer"},
                    auto_execute=False,
                ),
                HandoffAction(
                    id="reject-spec",
                    label="Reject",
                    description="Reject spec and mark issue as needs_spec",
                    style="danger",
                    mode="shell",
                    command="",
                    args={"issue": ctx.issue_number},
                    auto_execute=False,
                ),
            ],
            notification_message=f"Spec for #{ctx.issue_number} ready for review ({reason})",
            notification_subtitle=f"Researcher finished #{ctx.issue_number}",
            result_summary=f"Spec awaiting approval ({reason})",
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: spec file must exist for this issue."""
        spec_path = find_spec_file(ctx.issue_number, project_dir=ctx.project_dir)
        if spec_path is not None:
            return GateCheckResult(passed=True)
        return GateCheckResult(
            passed=False,
            reason="Spec file not found in .claude/specs/",
        )
