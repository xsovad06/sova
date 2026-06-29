"""Triage role -- assess issues and classify them for the pipeline.

Reads BACKLOG issues, evaluates them for agent suitability, applies
labels, appends assessment to the issue body, and moves them to TRIAGED.

Uses heuristic pre-checks and optional Claude-based deep assessment.
"""

from __future__ import annotations

import json

from sova.adapters.base import Task, TaskState
from sova.config.models import TriageConfig
from sova.core.context import ExecutionContext
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.utils.logging import get_logger

log = get_logger(component="role.triage")

_ACCEPTANCE_CRITERIA = "acceptance criteria"

# Prompt template for Claude-based assessment
_ASSESSMENT_PROMPT = """\
Assess this GitHub issue for autonomous AI agent suitability.

## Issue #{issue_id}: {title}

{body}

## Labels
{labels}

## Assessment Criteria

Evaluate on these dimensions:
1. Is the description specific enough? (acceptance criteria, steps, expected behavior)
2. Does it reference specific files, functions, or components?
3. Is the scope bounded? (single feature vs epic)
4. Does it require domain knowledge the agent lacks?
5. Are there external dependencies or environment requirements?

## Output Format

Respond with a JSON object (no markdown fencing):
{{
    "suitability": "ready" | "needs_spec" | "needs_research" | "human_only",
    "confidence": 0.0-1.0,
    "reasoning": "one paragraph explanation",
    "missing_context": ["list", "of", "missing", "items"],
    "estimated_complexity": "trivial" | "simple" | "moderate" | "complex" | "epic",
    "suggested_role": "researcher" | "developer" | "triage",
    "sub_tasks": ["optional", "breakdown"]
}}
"""


class TriageRole(AgentRole):
    name = "triage"
    description = "Assess issues for agent suitability and classify them"
    allowed_input_states = frozenset({TaskState.BACKLOG})
    output_state = TaskState.TRIAGED

    _DEFAULT_LABELS: dict[str, str] = {
        "ready": "agent:ready",
        "needs_spec": "agent:needs-spec",
        "needs_research": "agent:needs-research",
        "human_only": "agent:human-only",
    }
    SUITABILITY_LABELS = _DEFAULT_LABELS

    def resolve_label(self, suitability: str, triage_cfg: TriageConfig) -> str | None:
        """Resolve the label for a suitability outcome, respecting config overrides.

        Returns None if labeling should be skipped for that outcome.
        """
        if suitability in triage_cfg.labels:
            label = triage_cfg.labels[suitability]
            return label if label else None
        return self._DEFAULT_LABELS.get(suitability)

    def heuristic_assess(self, task: Task, triage_cfg: TriageConfig) -> TaskAssessment:
        """Config-aware heuristic assessment with skip pattern support.

        Checks title prefixes and labels against configured skip patterns
        before falling back to content-based heuristics.
        """
        title_lower = task.title.lower()
        for prefix in triage_cfg.skip_title_prefixes:
            if title_lower.startswith(prefix.lower()):
                return TaskAssessment(
                    suitability="human_only",
                    confidence=0.9,
                    reasoning=f"Title prefix '{prefix}' matches skip pattern; not suitable for agent work.",
                    estimated_complexity="moderate",
                    suggested_role="triage",
                )

        if triage_cfg.skip_labels:
            skip_set = {s.lower() for s in triage_cfg.skip_labels}
            matched = [lbl for lbl in task.labels if lbl.lower() in skip_set]
            if matched:
                return TaskAssessment(
                    suitability="human_only",
                    confidence=0.9,
                    reasoning=f"Label '{matched[0]}' matches skip pattern; not suitable for agent work.",
                    estimated_complexity="moderate",
                    suggested_role="triage",
                )

        return self._heuristic_assess(task)

    async def assess_task(self, task: Task) -> TaskAssessment:
        """Assess task suitability using heuristics.

        Falls back to heuristic-only when LLM is not available.
        Use assess_task_with_llm() for Claude-based deep assessment.
        """
        return self._heuristic_assess(task)

    async def assess_task_with_llm(self, task: Task, ctx: ExecutionContext) -> TaskAssessment:
        """Assess task using Claude for deeper analysis.

        Falls back to heuristic assessment if LLM invocation fails.
        """
        # Quick heuristic pre-check: no body = definitely needs spec
        if not task.body or not task.body.strip():
            return self._heuristic_assess(task)

        try:
            from sova.llm.client import invoke, resolve_model
            from sova.llm.cost import record_cost

            resolved = resolve_model("triage", ctx.config.roles, llm_config=ctx.config.llm)
            model = resolved[0] if resolved else None
            model_reason = resolved[1] if resolved else None
            prompt = _ASSESSMENT_PROMPT.format(
                issue_id=task.id,
                title=task.title,
                body=task.body or "(no description)",
                labels=", ".join(task.labels) if task.labels else "none",
            )

            result = await invoke(
                prompt,
                model=model,
                cwd=ctx.project_dir,
                max_budget_usd=ctx.config.agent.max_budget / 10,
                timeout=120,
            )

            ctx.add_cost(result.cost_usd)
            await record_cost(
                result,
                phase="triage",
                issue=str(task.id),
                task_run_id=ctx.task_run_id,
                model_selection_reason=model_reason,
            )
            assessment = self._parse_llm_assessment(result.text)
            if assessment:
                return assessment

        except Exception as exc:
            log.warning("triage.llm_fallback", error=str(exc))

        return self._heuristic_assess(task)

    def _heuristic_assess(self, task: Task) -> TaskAssessment:
        """Quick heuristic-based assessment without LLM."""
        has_body = bool(task.body and task.body.strip())
        if not has_body:
            return TaskAssessment(
                suitability="needs_spec",
                confidence=0.7,
                reasoning="Issue has no description; needs specification before work can begin.",
                missing_context=["description", _ACCEPTANCE_CRITERIA],
                estimated_complexity="moderate",
                suggested_role="triage",
            )

        body = task.body.strip().lower()

        # Check for acceptance criteria indicators
        has_criteria = any(
            marker in body
            for marker in ["- [ ]", _ACCEPTANCE_CRITERIA, "expected behavior", "## scope", "## requirements"]
        )

        # Check for file/code references
        has_code_refs = any(
            marker in body for marker in [".py", ".ts", ".js", ".sh", "`", "```", "function", "class ", "def "]
        )

        if not has_criteria and len(body) < 100:
            return TaskAssessment(
                suitability="needs_spec",
                confidence=0.6,
                reasoning="Issue has a short description without acceptance criteria.",
                missing_context=[_ACCEPTANCE_CRITERIA, "expected behavior"],
                estimated_complexity="moderate",
                suggested_role="triage",
            )

        if has_criteria and has_code_refs:
            return TaskAssessment(
                suitability="ready",
                confidence=0.85,
                reasoning="Issue has acceptance criteria and code references; ready for research.",
                estimated_complexity="moderate",
                suggested_role="researcher",
            )

        return TaskAssessment(
            suitability="ready",
            confidence=0.8,
            reasoning="Issue has a title and description; ready for research.",
            estimated_complexity="moderate",
            suggested_role="researcher",
        )

    def _parse_llm_assessment(self, text: str) -> TaskAssessment | None:
        """Parse Claude's JSON response into a TaskAssessment."""
        try:
            # Strip markdown fencing if present
            cleaned = text.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

            data = json.loads(cleaned)

            return TaskAssessment(
                suitability=data["suitability"],
                confidence=float(data.get("confidence", 0.7)),
                reasoning=data.get("reasoning", ""),
                missing_context=data.get("missing_context", []),
                estimated_complexity=data.get("estimated_complexity", "moderate"),
                suggested_role=data.get("suggested_role", "researcher"),
                sub_tasks=data.get("sub_tasks", []),
            )
        except (KeyError, ValueError) as exc:
            log.warning("triage.parse_failed", error=str(exc), exc_info=True)
            return None

    async def execute(self, ctx: ExecutionContext) -> RoleResult:
        task = await ctx.adapter.get_task(ctx.issue_number)

        if not self.validate_preconditions(task, force=ctx.force):
            return RoleResult(
                success=False,
                summary=f"Issue #{ctx.issue_number} not in valid state for triage",
                error=f"Precondition failed: issue is in {task.state}, "
                f"expected one of {', '.join(self.allowed_input_states)}",
            )

        triage_cfg = ctx.config.triage
        log.info("triage.start", issue=ctx.issue_number, mode=triage_cfg.mode)

        assessment = self.heuristic_assess(task, triage_cfg)

        if assessment.confidence < triage_cfg.min_confidence:
            log.info(
                "triage.below_confidence",
                issue=ctx.issue_number,
                confidence=assessment.confidence,
                threshold=triage_cfg.min_confidence,
            )

        if triage_cfg.mode == "dry_run":
            log.info(
                "triage.dry_run",
                issue=ctx.issue_number,
                suitability=assessment.suitability,
                confidence=assessment.confidence,
            )
            return RoleResult(
                success=True,
                summary=f"Issue #{ctx.issue_number} assessed as {assessment.suitability} "
                f"({assessment.confidence:.0%} confidence) -- dry run, no changes written",
            )

        if triage_cfg.auto_label:
            label = self.resolve_label(assessment.suitability, triage_cfg)
            if label:
                await ctx.adapter.add_label(ctx.issue_number, label)

        assessment_section = self._build_assessment_comment(task, assessment)
        if triage_cfg.mode == "comment":
            await ctx.adapter.post_comment(ctx.issue_number, assessment_section)
        elif triage_cfg.write_body:
            updated_body = (task.body or "").rstrip() + "\n\n" + assessment_section
            await ctx.adapter.edit_body(ctx.issue_number, updated_body)

        if triage_cfg.write_transition:
            await ctx.adapter.transition_state(ctx.issue_number, TaskState.TRIAGED)

        log.info("triage.done", issue=ctx.issue_number, mode=triage_cfg.mode)
        return RoleResult(
            success=True,
            summary=f"Issue #{ctx.issue_number} triaged as {assessment.suitability}",
            output_state=TaskState.TRIAGED if triage_cfg.write_transition else None,
        )

    def _build_assessment_comment(self, task: Task, assessment: TaskAssessment) -> str:
        """Build a triage assessment section to append to the issue body."""
        has_body = bool(task.body and task.body.strip())
        missing = ", ".join(assessment.missing_context) if assessment.missing_context else "none"
        parts = [
            "## Triage Assessment\n",
            f"**Title**: {task.title}",
            f"**Has description**: {'yes' if has_body else 'no'}",
            f"**Suitability**: {assessment.suitability}",
            f"**Confidence**: {assessment.confidence:.0%}",
            f"**Complexity**: {assessment.estimated_complexity}",
            f"**Missing context**: {missing}",
            f"**Labels**: {', '.join(task.labels) if task.labels else 'none'}",
            "",
            assessment.reasoning,
        ]

        if assessment.suitability == "needs_spec" and assessment.missing_context:
            parts.append("\n### What's needed before this can be worked on:\n")
            for item in assessment.missing_context:
                parts.append(f"- {item}")

        if assessment.sub_tasks:
            parts.append("\n### Suggested sub-tasks:\n")
            for sub in assessment.sub_tasks:
                parts.append(f"- {sub}")

        return "\n".join(parts)
