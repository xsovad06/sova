"""Step: Spec -- generate structured specification before development.

Produces a .claude/specs/{issue}-{slug}.md specification document
that guides the developer with concrete implementation plans,
design decisions, and scope boundaries.

The step generates specs by sending a focused prompt to the LLM via
``invoke()`` and writing the response to disk. This avoids the
interactive ``/spec`` command which expects user input (Steps 5-8:
"ask the user", "iterate on feedback", "pre-write checklist") and
fails silently in headless pipeline mode.

Behavior depends on complexity threshold, research findings, and open questions:
- Issues whose Research section indicates already implemented: skip entirely
- Tasks below spec.threshold skip this step entirely
- Simple specs with no open questions: auto-approve, continue pipeline
- Complex specs or open questions: write handoff, pause for dashboard approval
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from sova.core.context import ExecutionContext
from sova.core.steps._handoff_helpers import write_step_handoff
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.dashboard.services.spec_service import find_spec_file
from sova.ipc.handoff import HandoffAction
from sova.llm.client import invoke
from sova.llm.guard import PromptInjectionError
from sova.utils.logging import get_logger
from sova.utils.markdown import extract_section as _extract_section

log = get_logger(component="step.spec")

_COMPLEXITY_ORDER = ("always", "trivial", "simple", "moderate", "complex", "never")

# Longer patterns first -- substring matches greedily, so "already fully
# implemented" must precede "already implemented" to avoid a short match
# shadowing intent (ordering is cosmetic for `any()` but documents intent).
_ALREADY_IMPLEMENTED_PATTERNS = (
    "already fully implemented",
    "already implemented",
    "already complete",
    "already been implemented",
    "has been fully implemented",
    "has been implemented",
    "fully implemented already",
    "implementation is complete",
    "implementation is already complete",
    "no remaining work",
    "nothing left to implement",
)


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


# _extract_section is imported from sova.utils.markdown above


def _research_says_implemented(body: str) -> bool:
    """Return True if the Research section indicates the issue is already implemented.

    Uses simple substring matching scoped to the Research section only.
    Known limitation: a Research section that mentions partial implementation
    (e.g., "Feature X is already implemented, but Feature Y is not") will
    still match and cause a skip.  This is an acceptable trade-off because
    the researcher agent uses specific phrasing for its verdicts, and the
    worst case (skipping spec for a done issue) is low-impact.
    """
    research = _extract_section(body, "Research")
    if not research:
        return False
    lower = research.lower()
    return any(p in lower for p in _ALREADY_IMPLEMENTED_PATTERNS)


def _make_slug(title: str, max_len: int = 40) -> str:
    """Generate a filesystem-safe slug from an issue title.

    Delegates to the shared ``slugify`` utility, with a fallback for
    empty titles.
    """
    from sova.utils.formatting import slugify

    slug = slugify(title, max_length=max_len)
    return slug or "spec"


def _extract_spec_content(text: str) -> str:
    """Extract spec markdown from an LLM response.

    The LLM may return the spec in several formats:
    1. Wrapped in a ```markdown or ```md fenced block
    2. Starting directly with ``# Spec:`` heading
    3. As plain text (fallback)
    """
    if not text:
        return ""

    fence_match = re.search(r"```(?:markdown|md)\s*\n(.*?)```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    heading_match = re.search(r"^(# Spec:.+)", text, re.MULTILINE | re.DOTALL)
    if heading_match:
        return heading_match.group(1).strip()

    return text.strip()


_SPEC_PROMPT_TEMPLATE = """\
You are a technical spec writer. Produce a structured specification document for the task below.

Return ONLY the spec document in markdown format. Do not include any preamble, explanation, or \
commentary outside the spec itself. The spec must start with ``# Spec: {{title}}`` and follow \
the exact template structure shown below.

## Task

**Issue**: #{{issue_number}}
**Title**: {{title}}

### Description

{{body}}

## Spec Template

Use this exact structure:

```
# Spec: {{title}}

**Issue**: #{{issue_number}}
**Status**: draft
**Created**: {{date}}
**Complexity**: simple | moderate | complex

## Problem

What problem does this solve? 1-3 sentences from the user's perspective.

## Solution

High-level approach in 2-4 sentences. What changes and why.

## Implementation Plan

Ordered steps. Each step should be one commit-sized unit of work.
Reference specific files to create/modify.

1. Step one
2. Step two

## Design Decisions

Pre-answered questions for ambiguities found.

1. **Question?** Answer with rationale.

(Omit if no ambiguities exist.)

## Scope Boundaries

Explicit limits to prevent over-engineering.

- Do NOT {{thing out of scope}}

## Edge Cases

Things the implementation must handle that aren't obvious.

## Testing Strategy

What to test: key scenarios, edge cases, integration points.

## Open Questions

Anything that needs user input before development starts.

(Omit this section if there are no open questions.)
```

## Rules

- Keep the implementation plan concrete ("add X to Y", not "implement the feature")
- Reference specific file paths when the issue body mentions them
- Omit sections that don't apply (e.g., no "Data Model" for a pure UI change)
- Complexity: simple (1-2 files, <50 lines), moderate (3-6 files, 50-200 lines), complex (7+ files or >200 lines)
- Do NOT include code blocks in the spec itself (it is a planning document)
- Return ONLY the spec document, no surrounding text
"""


def _text_has_open_questions(text: str) -> bool:
    """Check if text contains an Open Questions section with content."""
    content = _extract_section(text, "Open Questions")
    normalized = content.strip().lower() if content else ""
    if not normalized or normalized.startswith("(omit") or normalized.startswith("none"):
        return False
    return True


def _build_spec_prompt(issue_number: str, title: str, body: str) -> str:
    """Build the LLM prompt for spec generation."""
    from datetime import date

    return (
        _SPEC_PROMPT_TEMPLATE.replace("{{issue_number}}", issue_number)
        .replace("{{title}}", title)
        .replace("{{body}}", body)
        .replace("{{date}}", date.today().isoformat())
    )


def _sanitize_issue_number(issue_number: str) -> str:
    """Strip path separators and traversal tokens from issue_number."""
    return re.sub(r"[/\\]", "", issue_number).lstrip(".")


def _write_spec_file(issue_number: str, title: str, content: str, project_dir: Path) -> Path:
    """Write spec content to .claude/specs/{issue}-{slug}.md."""
    safe_issue = _sanitize_issue_number(issue_number)
    specs_dir = project_dir / ".claude" / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    slug = _make_slug(title)
    spec_path = specs_dir / f"{safe_issue}-{slug}.md"
    spec_path.write_text(content)
    return spec_path


class SpecStep(BaseStep):
    name = "spec"

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        if self.name in ctx.completed_steps:
            return True

        threshold = ctx.config.spec.threshold
        if threshold == "never":
            return True

        task = ctx.task
        if task is None:
            task = await ctx.adapter.get_task(ctx.issue_number)
        body = task.body or ""

        if _research_says_implemented(body):
            log.info("step.spec.skip_implemented", issue=ctx.issue_number)
            return True

        if threshold == "always":
            return False

        task_complexity = _extract_complexity(body)
        threshold_rank = _complexity_rank(threshold)
        task_rank = _complexity_rank(task_complexity)

        # Skip if task complexity is below threshold
        return task_rank < threshold_rank

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.spec", issue=ctx.issue_number, cwd=str(ctx.project_dir))

        task = ctx.task
        if task is None:
            task = await ctx.adapter.get_task(ctx.issue_number)

        prompt = _build_spec_prompt(ctx.issue_number, task.title, task.body or "")

        try:
            result = await invoke(
                prompt,
                model=ctx.resolved_model or ctx.config.roles.researcher_model or ctx.config.agent.model,
                cwd=ctx.project_dir,
                max_budget_usd=ctx.config.agent.max_budget / 5,
                timeout=ctx.config.agent.step_timeout,
            )
            ctx.add_cost(result.cost_usd)
        except (RuntimeError, PromptInjectionError) as exc:
            return StepResult(success=False, summary="Spec generation failed", error=str(exc))

        spec_content = _extract_spec_content(result.text)
        if not spec_content:
            return StepResult(
                success=False,
                summary="LLM returned no spec content",
                error="LLM response was empty or could not be parsed into a spec",
                cost_usd=result.cost_usd,
            )

        try:
            spec_path = _write_spec_file(ctx.issue_number, task.title, spec_content, ctx.project_dir)
        except IOError as exc:
            return StepResult(
                success=False,
                summary="Failed to write spec file",
                error=str(exc),
                cost_usd=result.cost_usd,
            )
        log.info("step.spec.written", issue=ctx.issue_number, path=str(spec_path))

        # Read once, derive everything from text
        text = spec_path.read_text()
        has_questions = _text_has_open_questions(text)
        spec_complexity = _extract_complexity(text)

        # Auto-approve simple specs without open questions.
        # Auto-approved specs chain directly to developer via handoff with auto_execute=True,
        # exiting the researcher pipeline early (skip-to-role pattern).
        if ctx.config.spec.auto_approve_simple and not has_questions:
            if _complexity_rank(spec_complexity) <= _complexity_rank("simple"):
                # Mark as approved in the spec file
                if not re.search(r"\*\*Status\*\*:\s*\w+", text):
                    return StepResult(
                        success=False,
                        summary="Auto-approval failed: status line not found in spec",
                        error="Could not find **Status**: <value> pattern in spec file",
                    )
                updated = re.sub(r"\*\*Status\*\*:\s*\w+", "**Status**: approved", text, count=1)
                try:
                    spec_path.write_text(updated)
                except IOError as exc:
                    return StepResult(
                        success=False,
                        summary="Failed to update spec file",
                        error=str(exc),
                    )
                log.info(
                    "step.spec.auto_approved",
                    issue=ctx.issue_number,
                    complexity=spec_complexity,
                )
                return await write_step_handoff(
                    ctx,
                    role="researcher",
                    phase="spec",
                    summary=f"Spec auto-approved for #{ctx.issue_number} (complexity: {spec_complexity})",
                    agent_summary="Spec auto-approved, spawning developer",
                    next_action="develop",
                    actions=[
                        HandoffAction(
                            id="develop",
                            label="Develop",
                            description=f"Start development for #{ctx.issue_number}",
                            style="approve",
                            mode="agent",
                            command="",
                            args={"issue": ctx.issue_number, "role": "developer"},
                            auto_execute=True,
                        ),
                    ],
                    notification_message=f"Spec auto-approved for #{ctx.issue_number}, starting developer",
                    notification_subtitle=f"Researcher finished #{ctx.issue_number}",
                    result_summary=f"Spec auto-approved (complexity: {spec_complexity}), handed off to developer",
                    cost_usd=result.cost_usd,
                )

        # Needs human review -- write handoff and pause pipeline
        return await self._write_approval_handoff(ctx, spec_complexity, has_questions, cost_usd=result.cost_usd)

    async def _write_approval_handoff(
        self,
        ctx: ExecutionContext,
        complexity: str,
        has_questions: bool,
        *,
        cost_usd: Decimal = Decimal("0"),
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
            cost_usd=cost_usd,
            awaiting_approval=True,
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
