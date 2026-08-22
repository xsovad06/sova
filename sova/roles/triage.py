"""Triage role -- assess issues and classify them for the pipeline.

Reads BACKLOG issues, evaluates them for agent suitability, applies
labels, appends assessment to the issue body, and moves them to TRIAGED.

Uses heuristic pre-checks, deterministic quality scoring, optional
LLM-based enrichment, and optional Claude-based deep assessment.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sova.adapters.base import Task, TaskState
from sova.config.models import TriageConfig
from sova.core.context import ExecutionContext
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.utils.logging import get_logger

log = get_logger(component="role.triage")

_ACCEPTANCE_CRITERIA = "acceptance criteria"


def _strip_markdown_fencing(text: str) -> str:
    """Remove markdown code fencing (```...```) wrapping from LLM output."""
    if text.startswith("```"):
        lines = text.split("\n")
        return "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return text


# Line-initial patterns that indicate LLM-generated preamble/postamble.
# Anchored to line start (after stripping) to avoid false positives on
# legitimate mid-sentence occurrences like "Let me know if you have questions".
_LLM_LEAK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^here(?:'s| is) (?:a |an |the )", re.IGNORECASE),
    re.compile(r"^i(?:'ve| have) (?:created|written|drafted|prepared|updated)", re.IGNORECASE),
    re.compile(r"^let me (?!know\b)", re.IGNORECASE),
    re.compile(r"^(?:sure|certainly|absolutely)[,!.]", re.IGNORECASE),
    re.compile(r"^i'll ", re.IGNORECASE),
    re.compile(r"^(?:feel free|don't hesitate) to ", re.IGNORECASE),
]


@dataclass(frozen=True, slots=True)
class EnrichedAssessment:
    """Result of a combined assess-and-enrich LLM call.

    Keeps enrichment concerns out of the TaskAssessment Pydantic model.
    """

    assessment: TaskAssessment
    enriched_body: str | None


@dataclass(frozen=True, slots=True)
class QualityScore:
    """Deterministic quality assessment of an issue body.

    Evaluates 7 structural dimensions (max 8 points):
      1. has_objective (1 pt): ## Objective section present
      2. has_description (1 pt): ## Detailed Description present
      3. has_acceptance_criteria (2 pts): ## Acceptance Criteria with checkboxes
      4. has_files_to_change (1 pt): ## Files / Modules to Change present
      5. has_scope_boundaries (1 pt): ## Out of Scope section present
      6. has_references (1 pt): links or code paths referenced
      7. no_llm_leaks (1 pt): no LLM preamble/postamble detected
    """

    has_objective: bool
    has_description: bool
    has_acceptance_criteria: bool
    has_files_to_change: bool
    has_scope_boundaries: bool
    has_references: bool
    no_llm_leaks: bool

    @property
    def total(self) -> int:
        score = 0
        if self.has_objective:
            score += 1
        if self.has_description:
            score += 1
        if self.has_acceptance_criteria:
            score += 2
        if self.has_files_to_change:
            score += 1
        if self.has_scope_boundaries:
            score += 1
        if self.has_references:
            score += 1
        if self.no_llm_leaks:
            score += 1
        return score

    def meets_threshold(self, min_score: int = 4) -> bool:
        return self.total >= min_score and self.has_acceptance_criteria


def compute_quality_score(body: str) -> QualityScore:
    """Score an issue body against structural quality dimensions.

    Pure function: no LLM, no I/O. Returns QualityScore with max 8 points.
    """
    if not body or not body.strip():
        return QualityScore(
            has_objective=False,
            has_description=False,
            has_acceptance_criteria=False,
            has_files_to_change=False,
            has_scope_boundaries=False,
            has_references=False,
            no_llm_leaks=True,
        )

    body_lower = body.lower()
    headings = {h.strip() for h in re.findall(r"^##\s+([^\n]+)", body_lower, re.MULTILINE)}

    has_objective = "objective" in headings
    has_description = "detailed description" in headings
    has_acceptance_criteria = "acceptance criteria" in headings and "- [ ]" in body_lower
    has_files = any(h in headings for h in ("files / modules to change", "files to change", "files"))
    has_scope = any(
        h in headings for h in ("out of scope / constraints", "out of scope", "constraints", "scope boundaries")
    )
    has_references = "references" in headings or bool(re.search(r"(?:#\d+|https?://|`[a-zA-Z_/]+\.\w+`)", body))

    # LLM leak detection: check line-initial patterns
    no_llm_leaks = True
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for pattern in _LLM_LEAK_PATTERNS:
            if pattern.match(stripped):
                no_llm_leaks = False
                break
        if not no_llm_leaks:
            break

    return QualityScore(
        has_objective=has_objective,
        has_description=has_description,
        has_acceptance_criteria=has_acceptance_criteria,
        has_files_to_change=has_files,
        has_scope_boundaries=has_scope,
        has_references=has_references,
        no_llm_leaks=no_llm_leaks,
    )


def _collect_missing_sections(quality: QualityScore) -> list[str]:
    """Return headings for quality dimensions the issue body is missing."""
    _SECTION_MAP = [
        (quality.has_objective, "## Objective"),
        (quality.has_description, "## Detailed Description"),
        (quality.has_acceptance_criteria, "## Acceptance Criteria (with - [ ] checkboxes)"),
        (quality.has_files_to_change, "## Files / Modules to Change"),
        (quality.has_scope_boundaries, "## Out of Scope / Constraints"),
        (quality.has_references, "## References"),
    ]
    return [heading for present, heading in _SECTION_MAP if not present]


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


_ASSESS_AND_ENRICH_PROMPT = """\
You are an issue quality improver AND assessor. The following GitHub issue \
body is missing key structural sections that are needed for an autonomous \
agent to work on it. Your job is to:
1. Rewrite the issue body with all missing sections filled in.
2. Assess the issue for autonomous AI agent suitability.

## Issue #{issue_id}: {title}

### Current Body
{body}

### Labels
{labels}

### Missing Sections
{missing_sections}

### GitHub Issue Template Sections
The issue should have these sections:
- ## Objective (one sentence: what and why)
- ## Detailed Description (concrete description with examples)
- ## Acceptance Criteria (testable pass/fail checkboxes using - [ ])
- ## Files / Modules to Change (which files to modify)
- ## Out of Scope / Constraints (what must NOT happen)
- ## References (related issues, code paths, docs)

### Assessment Criteria
Note: if enrichment succeeds (quality score meets threshold), the heuristic suitability
will be used instead. Your assessment is used only if enrichment fails or is insufficient.
Evaluate on these dimensions:
1. Is the description specific enough? (acceptance criteria, steps, expected behavior)
2. Does it reference specific files, functions, or components?
3. Is the scope bounded? (single feature vs epic)
4. Does it require domain knowledge the agent lacks?
5. Are there external dependencies or environment requirements?

### Output Format

Respond with a JSON object (no markdown fencing) containing two top-level keys:

{{
    "enriched_body": "the complete rewritten issue body in markdown, preserving all existing details and context",
    "assessment": {{
        "suitability": "ready" | "needs_spec" | "needs_research" | "human_only",
        "confidence": 0.0-1.0,
        "reasoning": "one paragraph explanation",
        "missing_context": ["list", "of", "missing", "items"],
        "estimated_complexity": "trivial" | "simple" | "moderate" | "complex" | "epic",
        "suggested_role": "researcher" | "developer" | "triage",
        "sub_tasks": ["optional", "breakdown"]
    }}
}}

IMPORTANT: Output ONLY the JSON object. No markdown fencing, no preamble, no postamble.
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

    async def assess_tasks_batch(
        self,
        tasks: list[Task],
        ctx: ExecutionContext,
    ) -> list[tuple[Task, TaskAssessment]]:
        """Assess multiple tasks via the Batch API.

        Falls back to sequential assess_task_with_llm() if batch submission
        fails or no batch backend is available.
        """
        from sova.llm.client import invoke_batch, resolve_model
        from sova.llm.cost import record_cost
        from sova.llm.models import BatchRequest

        resolved = resolve_model("triage", ctx.config.roles, llm_config=ctx.config.llm)
        model = resolved[0] if resolved else ""
        model_reason = resolved[1] if resolved else None

        batch_requests: list[BatchRequest] = []
        heuristic_results: list[tuple[int, Task, TaskAssessment]] = []

        triage_cfg = ctx.config.triage
        for i, task in enumerate(tasks):
            if not task.body or not task.body.strip():
                heuristic_results.append((i, task, self._heuristic_assess(task)))
                continue
            skip_assessment = self.heuristic_assess(task, triage_cfg)
            if skip_assessment.suitability == "human_only":
                heuristic_results.append((i, task, skip_assessment))
                continue
            prompt = _ASSESSMENT_PROMPT.format(
                issue_id=task.id,
                title=task.title,
                body=task.body or "(no description)",
                labels=", ".join(task.labels) if task.labels else "none",
            )
            batch_requests.append(
                BatchRequest(
                    custom_id=str(task.id),
                    prompt=prompt,
                    model=model,
                )
            )

        if not batch_requests:
            return [(t, a) for _, t, a in sorted(heuristic_results, key=lambda x: x[0])]

        try:
            batch_results = await invoke_batch(
                batch_requests,
                poll_interval=ctx.config.llm.batch_poll_interval,
                timeout=ctx.config.llm.batch_timeout,
                gcs_bucket=ctx.config.llm.batch_gcs_bucket,
                gcs_prefix=ctx.config.llm.batch_gcs_prefix,
            )
        except Exception as exc:
            log.warning("triage.batch_fallback", error=str(exc), exc_info=True)
            return await self._sequential_fallback(tasks, ctx)

        task_by_id = {str(t.id): t for t in tasks}
        llm_results: list[tuple[int, Task, TaskAssessment]] = []
        task_index = {str(t.id): i for i, t in enumerate(tasks)}

        for br in batch_results:
            task = task_by_id.get(br.request.custom_id)
            if task is None:
                log.warning("triage.batch_unknown_custom_id", custom_id=br.request.custom_id)
                continue
            idx = task_index[str(task.id)]

            if br.error:
                log.warning("triage.batch_item_failed", issue=task.id, error=br.error)

            if br.succeeded and br.result is not None:
                ctx.add_cost(br.result.cost_usd)
                try:
                    await record_cost(
                        br.result,
                        phase="batch_triage",
                        issue=str(task.id),
                        task_run_id=ctx.task_run_id,
                        model_selection_reason=f"batch:{model_reason}" if model_reason else "batch",
                    )
                except Exception:
                    log.warning("triage.batch_cost_record_failed", issue=task.id, exc_info=True)

                assessment = self._parse_llm_assessment(br.result.text)
                if assessment:
                    llm_results.append((idx, task, assessment))
                    continue

            llm_results.append((idx, task, self._heuristic_assess(task)))

        covered_indices = {i for i, _, _ in heuristic_results} | {i for i, _, _ in llm_results}
        for i, task in enumerate(tasks):
            if i not in covered_indices:
                log.warning("triage.batch_missing_result", issue=task.id)
                llm_results.append((i, task, self._heuristic_assess(task)))

        all_results = heuristic_results + llm_results
        all_results.sort(key=lambda x: x[0])
        return [(t, a) for _, t, a in all_results]

    async def _sequential_fallback(
        self,
        tasks: list[Task],
        ctx: ExecutionContext,
    ) -> list[tuple[Task, TaskAssessment]]:
        """Fall back to sequential LLM assessment for each task."""
        results: list[tuple[Task, TaskAssessment]] = []
        for task in tasks:
            assessment = await self.assess_task_with_llm(task, ctx)
            results.append((task, assessment))
        return results

    def _heuristic_assess(self, task: Task) -> TaskAssessment:
        """Quick heuristic-based assessment without LLM.

        Uses structured signals (body length, section headings, code refs,
        labels) to produce high-confidence results, reducing LLM fallback.
        """
        # Check labels first -- human-only takes priority regardless of body content
        label_set = {lbl.lower() for lbl in (task.labels or [])}
        if "agent:human-only" in label_set:
            return TaskAssessment(
                suitability="human_only",
                confidence=0.95,
                reasoning="Issue is labeled as human-only.",
                estimated_complexity="moderate",
                suggested_role="triage",
            )

        has_body = bool(task.body and task.body.strip())
        if not has_body:
            return TaskAssessment(
                suitability="needs_spec",
                confidence=0.9,
                reasoning="Issue has no description; needs specification before work can begin.",
                missing_context=["description", _ACCEPTANCE_CRITERIA],
                estimated_complexity="moderate",
                suggested_role="triage",
            )

        return self._assess_body_content(task.body.strip(), label_set)

    def _assess_body_content(self, body: str, label_set: set[str]) -> TaskAssessment:
        """Classify issue by body content and label signals."""
        body_lower = body.lower()
        body_len = len(body)

        has_criteria = self._has_criteria_markers(body_lower)
        has_code_refs = self._has_code_references(body_lower)
        has_section_headings = body_lower.count("\n##") >= 1
        has_type_label = any(lbl.startswith("type:") for lbl in label_set)
        is_bug = "type:bug" in label_set or "bug" in label_set

        complexity = self._estimate_complexity(body)

        if has_criteria and has_code_refs:
            return TaskAssessment(
                suitability="ready",
                confidence=0.9,
                reasoning="Issue has acceptance criteria and code references; ready for research.",
                estimated_complexity=complexity,
                suggested_role="researcher",
            )

        if is_bug and has_code_refs:
            return TaskAssessment(
                suitability="ready",
                confidence=0.85,
                reasoning="Bug report with code references; ready for research.",
                estimated_complexity=complexity,
                suggested_role="researcher",
            )

        if not has_criteria and body_len < 100:
            return TaskAssessment(
                suitability="needs_spec",
                confidence=0.8,
                reasoning="Issue has a short description without acceptance criteria.",
                missing_context=[_ACCEPTANCE_CRITERIA, "expected behavior"],
                estimated_complexity="simple",
                suggested_role="triage",
            )

        if has_criteria or (has_section_headings and body_len > 200):
            return TaskAssessment(
                suitability="ready",
                confidence=0.85,
                reasoning="Issue has structured sections indicating clear scope; ready for research.",
                estimated_complexity=complexity,
                suggested_role="researcher",
            )

        if body_len > 300 and (has_code_refs or has_type_label):
            return TaskAssessment(
                suitability="ready",
                confidence=0.8,
                reasoning="Issue has a detailed description with contextual signals; ready for research.",
                estimated_complexity=complexity,
                suggested_role="researcher",
            )

        return TaskAssessment(
            suitability="needs_research",
            confidence=0.75,
            reasoning="Issue has a description but lacks structured criteria; needs research first.",
            estimated_complexity=complexity,
            suggested_role="researcher",
        )

    @staticmethod
    def _has_criteria_markers(body_lower: str) -> bool:
        """Check if body contains acceptance criteria markers."""
        return any(
            marker in body_lower
            for marker in [
                "- [ ]",
                _ACCEPTANCE_CRITERIA,
                "expected behavior",
                "## scope",
                "## requirements",
                "## solution",
                "## steps",
                "## design",
            ]
        )

    @staticmethod
    def _has_code_references(body_lower: str) -> bool:
        """Check if body contains code references."""
        return any(
            marker in body_lower
            for marker in [".py", ".ts", ".js", ".sh", "`", "```", "function", "class ", "def ", "import "]
        )

    @staticmethod
    def _estimate_complexity(body: str) -> str:
        """Estimate task complexity from body signals."""
        body_lower = body.lower()
        body_len = len(body)
        if body_len < 150:
            return "simple"
        if body_len > 1000 and body_lower.count("\n##") >= 1:
            return "complex"
        if any(w in body_lower for w in ["migration", "refactor", "breaking change", "epic"]):
            return "complex"
        return "moderate"

    def _parse_llm_assessment(self, text: str) -> TaskAssessment | None:
        """Parse Claude's JSON response into a TaskAssessment."""
        try:
            cleaned = _strip_markdown_fencing(text.strip())

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

        # Quality gate: score the body and potentially enrich or block
        quality = compute_quality_score(task.body or "")
        log.info(
            "triage.quality_score",
            issue=ctx.issue_number,
            score=quality.total,
            threshold=triage_cfg.min_quality_score,
            has_ac=quality.has_acceptance_criteria,
        )

        if triage_cfg.mode == "dry_run":
            log.info(
                "triage.dry_run",
                issue=ctx.issue_number,
                suitability=assessment.suitability,
                confidence=assessment.confidence,
                quality_score=quality.total,
            )
            return RoleResult(
                success=True,
                summary=f"Issue #{ctx.issue_number} assessed as {assessment.suitability} "
                f"({assessment.confidence:.0%} confidence, quality {quality.total}/8) "
                "(dry run, no changes written)",
            )

        # Only apply the quality gate for issues that heuristics would mark "ready"
        if assessment.suitability == "ready" and not quality.meets_threshold(triage_cfg.min_quality_score):
            assessment = await self._apply_quality_gate(ctx, task, quality, triage_cfg, assessment)

        if triage_cfg.auto_label:
            label = self.resolve_label(assessment.suitability, triage_cfg)
            if label:
                await ctx.adapter.add_label(ctx.issue_number, label)

        assessment_section = self._build_assessment_comment(task, assessment, quality)
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
            summary=f"Issue #{ctx.issue_number} triaged as {assessment.suitability} (quality {quality.total}/8)",
            output_state=TaskState.TRIAGED if triage_cfg.write_transition else None,
        )

    async def _apply_quality_gate(
        self,
        ctx: ExecutionContext,
        task: Task,
        quality: QualityScore,
        triage_cfg: TriageConfig,
        original_assessment: TaskAssessment,
    ) -> TaskAssessment:
        """Apply quality gate: attempt enrichment, re-score, or downgrade to needs_spec."""
        llm_assessment: TaskAssessment | None = None

        if triage_cfg.auto_enrich:
            result = await self._assess_and_enrich(ctx, task, quality)
            if result:
                llm_assessment = result.assessment
                if result.enriched_body:
                    new_quality = compute_quality_score(result.enriched_body)
                    if new_quality.meets_threshold(triage_cfg.min_quality_score):
                        log.info(
                            "triage.enrichment_success",
                            issue=ctx.issue_number,
                            old_score=quality.total,
                            new_score=new_quality.total,
                        )
                        await ctx.adapter.edit_body(ctx.issue_number, result.enriched_body)
                        return original_assessment
                    log.info(
                        "triage.enrichment_insufficient",
                        issue=ctx.issue_number,
                        old_score=quality.total,
                        new_score=new_quality.total,
                        threshold=triage_cfg.min_quality_score,
                    )

        missing = []
        if not quality.has_acceptance_criteria:
            missing.append(_ACCEPTANCE_CRITERIA)
        if not quality.has_objective:
            missing.append("objective section")
        if not quality.has_description:
            missing.append("detailed description")
        log.info("triage.quality_gate_blocked", issue=ctx.issue_number, score=quality.total)

        if llm_assessment:
            return TaskAssessment(
                suitability="needs_spec",
                confidence=llm_assessment.confidence,
                reasoning=llm_assessment.reasoning,
                missing_context=llm_assessment.missing_context or missing,
                estimated_complexity=llm_assessment.estimated_complexity,
                suggested_role="triage",
            )

        return TaskAssessment(
            suitability="needs_spec",
            confidence=0.9,
            reasoning=f"Issue body quality score {quality.total}/8 is below threshold "
            f"{triage_cfg.min_quality_score}; needs specification work.",
            missing_context=missing,
            estimated_complexity=original_assessment.estimated_complexity,
            suggested_role="triage",
        )

    async def _assess_and_enrich(
        self, ctx: ExecutionContext, task: Task, quality: QualityScore
    ) -> EnrichedAssessment | None:
        """Combined LLM call: assess suitability AND enrich the issue body."""
        missing = _collect_missing_sections(quality)

        prompt = _ASSESS_AND_ENRICH_PROMPT.format(
            issue_id=task.id,
            title=task.title,
            body=task.body or "(empty)",
            labels=", ".join(task.labels) if task.labels else "none",
            missing_sections="\n".join(f"- {s}" for s in missing),
        )

        try:
            from sova.llm.client import invoke, resolve_model
            from sova.llm.cost import record_cost

            resolved = resolve_model("triage", ctx.config.roles, llm_config=ctx.config.llm)
            model = resolved[0] if resolved else None
            model_reason = resolved[1] if resolved else None

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
                phase="triage_assess_enrich",
                issue=str(task.id),
                task_run_id=ctx.task_run_id,
                model_selection_reason=model_reason,
            )

            cleaned = _strip_markdown_fencing(result.text.strip())

            data = json.loads(cleaned)

            assessment_data = data.get("assessment", {})
            assessment = TaskAssessment(
                suitability=assessment_data.get("suitability", "needs_spec"),
                confidence=float(assessment_data.get("confidence", 0.7)),
                reasoning=assessment_data.get("reasoning", ""),
                missing_context=assessment_data.get("missing_context", []),
                estimated_complexity=assessment_data.get("estimated_complexity", "moderate"),
                suggested_role=assessment_data.get("suggested_role", "researcher"),
                sub_tasks=assessment_data.get("sub_tasks", []),
            )

            enriched_body: str | None = None
            raw_body = data.get("enriched_body", "")
            if isinstance(raw_body, str):
                raw_body = _strip_markdown_fencing(raw_body.strip())
                if raw_body and len(raw_body) > len(task.body or ""):
                    enriched_body = raw_body
                elif raw_body:
                    log.warning(
                        "triage.assess_enrich_body_too_short",
                        issue=task.id,
                        original_len=len(task.body or ""),
                        enriched_len=len(raw_body),
                    )

            return EnrichedAssessment(assessment=assessment, enriched_body=enriched_body)

        except (KeyError, ValueError) as exc:
            log.warning("triage.assess_enrich_parse_failed", error=str(exc), exc_info=True)
        except Exception as exc:
            log.warning("triage.assess_enrich_failed", error=str(exc))

        return None

    def _build_assessment_comment(
        self, task: Task, assessment: TaskAssessment, quality: QualityScore | None = None
    ) -> str:
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
        ]
        if quality is not None:
            parts.append(f"**Quality score**: {quality.total}/8")
        parts.extend(
            [
                f"**Missing context**: {missing}",
                f"**Labels**: {', '.join(task.labels) if task.labels else 'none'}",
                "",
                assessment.reasoning,
            ]
        )

        if assessment.suitability == "needs_spec" and assessment.missing_context:
            parts.append("\n### What's needed before this can be worked on:\n")
            for item in assessment.missing_context:
                parts.append(f"- {item}")

        if assessment.sub_tasks:
            parts.append("\n### Suggested sub-tasks:\n")
            for sub in assessment.sub_tasks:
                parts.append(f"- {sub}")

        return "\n".join(parts)
