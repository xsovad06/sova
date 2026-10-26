"""Triage role: assess issues and classify them for the pipeline.

Reads BACKLOG issues, evaluates them for agent suitability, applies
labels, appends assessment to the issue body, and moves them to TRIAGED.

Uses heuristic pre-checks, deterministic quality scoring, optional
LLM-based enrichment, and optional Claude-based deep assessment.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from sova.adapters.base import Task, TaskAdapter, TaskState
from sova.config.models import TriageConfig
from sova.core.context import ExecutionContext
from sova.core.schema_validation import ValidationError, validate_step_output
from sova.core.schemas import get_triage_schema
from sova.roles._review_format import SCOPE_BOUNDARY_HEADINGS
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.supervisor.dependency_graph import is_human_only, is_interactive_recommended
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.adapters.ldap_client import LdapClient, Person
    from sova.llm.models import LLMResult

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
    has_scope = any(h.lower() in headings for h in SCOPE_BOUNDARY_HEADINGS)
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
        (quality.has_scope_boundaries, f"## {SCOPE_BOUNDARY_HEADINGS[0]}"),
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
6. Does it need another repo, a tool an autonomous agent cannot reach (an ephemeral environment,
   stage/prod, VPN, a cloud console, log search), investigation/spike work, a human decision
   or conversation, or UI work whose acceptance depends on visual verification in a browser?
   If so, suitability is "interactive" rather than "human_only": the issue is useful content
   for an agent-assisted interactive session, just not a fully autonomous run.

## Output Format

Respond with a JSON object (no markdown fencing):
{{
    "suitability": "ready" | "needs_spec" | "needs_research" | "human_only" | "interactive",
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
Note: if you assess suitability as "interactive" or "human_only", that verdict is used
regardless of enrichment outcome. Otherwise, if enrichment succeeds (quality score meets
threshold), the heuristic suitability is used instead; your assessment is used only if
enrichment fails or is insufficient.
Evaluate on these dimensions:
1. Is the description specific enough? (acceptance criteria, steps, expected behavior)
2. Does it reference specific files, functions, or components?
3. Is the scope bounded? (single feature vs epic)
4. Does it require domain knowledge the agent lacks?
5. Are there external dependencies or environment requirements?
6. Does it need another repo, a tool an autonomous agent cannot reach (an ephemeral environment,
   stage/prod, VPN, a cloud console, log search), investigation/spike work, a human decision
   or conversation, or UI work whose acceptance depends on visual verification in a browser?
   If so, suitability is "interactive" rather than "human_only": the issue is useful content
   for an agent-assisted interactive session, just not a fully autonomous run.

### Output Format

Respond with a JSON object (no markdown fencing) containing two top-level keys:

{{
    "enriched_body": "the complete rewritten issue body in markdown, preserving all existing details and context",
    "assessment": {{
        "suitability": "ready" | "needs_spec" | "needs_research" | "human_only" | "interactive",
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


# A config title-prefix/label match, or an existing suitability label already
# on the issue (the "sticky" checks in heuristic_assess()), are both
# deterministic verdicts, so these two outcomes short-circuit the batch LLM
# call: sending the ticket anyway would only let the model override what the
# operator (or a prior triage/`/find-task` run) already decided.
_LLM_SHORT_CIRCUIT_SUITABILITIES = frozenset({"human_only", "interactive"})


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
        "interactive": "agent:interactive-recommended",
    }
    SUITABILITY_LABELS = _DEFAULT_LABELS

    # ready/interactive/human_only are mutually exclusive outcomes of the same
    # pickability question; needs_spec/needs_research are not part of this set
    # and are left untouched when one of these three is applied.
    _EXCLUSIVE_SUITABILITY_KEYS: tuple[str, ...] = ("ready", "interactive", "human_only")

    # Conservatism ranking among the exclusive keys above, least to most
    # blocking. Used to decide which stale labels an opted-out outcome may
    # still clear: it may remove a less conservative stale label (that label
    # is simply wrong now) but never a more conservative one (an unlabelled
    # outcome is never the authority for erasing a stronger block).
    _CONSERVATISM_RANK: dict[str, int] = {"ready": 0, "interactive": 1, "human_only": 2}

    def resolve_label(self, suitability: str, triage_cfg: TriageConfig) -> str | None:
        """Resolve the label for a suitability outcome, respecting config overrides.

        Returns None if labeling should be skipped for that outcome.
        """
        if suitability in triage_cfg.labels:
            label = triage_cfg.labels[suitability]
            return label if label else None
        return self._DEFAULT_LABELS.get(suitability)

    async def apply_suitability_label(
        self,
        adapter: TaskAdapter,
        task_id: str,
        suitability: str,
        triage_cfg: TriageConfig,
        known_labels: list[str] | None = None,
    ) -> None:
        """Apply the label for `suitability`, then strip the other stale ready/interactive/human_only labels.

        Re-triage can change the outcome between these three mutually exclusive
        labels; without stripping, a previous run's label survives alongside
        the new one, leaving the issue with contradictory suitability labels.

        The new label is added before the stale ones are removed: if `add_label`
        raises, the issue keeps its previous (more conservative) label instead of
        being left with no suitability label at all. A failed removal is logged and
        swallowed, since the duplicate it leaves behind is corrected by the next
        retriage.

        `known_labels` lets a caller that just fetched the task pass its labels
        straight through instead of paying for another tracker round trip. Pass
        None (the default) when a round trip (an LLM call, a quality-gate
        enrichment) separates the caller's own snapshot from this call: the
        quality gate alone can take tens of seconds, and a label change made
        during that window would otherwise be invisible.

        Regardless of whether `suitability` resolves to a label, stale labels
        for outcomes strictly more conservative than `suitability` (ranked
        `ready`/`needs_spec`/`needs_research` < `interactive` < `human_only`)
        are never stripped: a verdict can only ever clear a label at or below
        its own conservatism. This is what makes `interactive` and
        `human_only` sticky across re-triage (an outcome weaker than the
        existing block is never the authority for erasing a stronger one),
        and it applies whether `suitability` resolved to a real label or was
        opted out of labelling entirely via an empty-string `triage.labels`
        override.
        """
        label = self.resolve_label(suitability, triage_cfg)
        if label:
            await adapter.add_label(task_id, label)
        elif suitability not in self._EXCLUSIVE_SUITABILITY_KEYS:
            return

        if known_labels is not None:
            current_lower = {lbl.lower() for lbl in known_labels}
        else:
            try:
                current_task = await adapter.get_task(task_id)
            except Exception:  # noqa: BLE001 (a refetch failure must not block the already-applied label)
                log.warning("triage.refetch_labels_failed", task_id=task_id, exc_info=True)
                return
            current_lower = {lbl.lower() for lbl in current_task.labels}

        suitability_rank = self._CONSERVATISM_RANK.get(suitability, 0)
        for key in self._EXCLUSIVE_SUITABILITY_KEYS:
            if key == suitability:
                continue
            if self._CONSERVATISM_RANK[key] > suitability_rank:
                continue
            stale_label = self.resolve_label(key, triage_cfg)
            if not stale_label or stale_label.lower() not in current_lower:
                continue
            if label and stale_label.lower() == label.lower():
                continue
            try:
                await adapter.remove_label(task_id, stale_label)
            except Exception:  # noqa: BLE001 (a leftover stale label is corrected by the next retriage)
                log.warning("triage.remove_stale_label_failed", task_id=task_id, label=stale_label, exc_info=True)

    @staticmethod
    def _match_pattern(task: Task, title_prefixes: list[str], labels: list[str]) -> str | None:
        """Describe the first title-prefix or label match, or None when nothing matches.

        Matching is case-insensitive on both sides. A prefix match names the
        configured prefix; a label match names the label as it appears on the
        issue, so the reasoning text quotes the ticket's own casing.
        """
        title_lower = task.title.lower()
        for prefix in title_prefixes:
            if title_lower.startswith(prefix.lower()):
                return f"Title prefix '{prefix}'"

        label_set = {lbl.lower() for lbl in labels}
        for label in task.labels:
            if label.lower() in label_set:
                return f"Label '{label}'"
        return None

    def heuristic_assess(self, task: Task, triage_cfg: TriageConfig) -> TaskAssessment:
        """Config-aware heuristic assessment with skip and interactive pattern support.

        Checks the issue's own human-only label first (resolved through
        `triage.labels`, so a renamed label is still honoured, plus the
        hardcoded `agent:human-only` helper as defence in depth for a
        partially-overridden map): a human's hard block must never lose to a
        config pattern match (e.g. an `interactive_title_prefixes` entry),
        since human_only is the more conservative of the two outcomes. Then
        checks title prefixes and labels against the configured skip
        patterns, then the interactive patterns. An issue already carrying
        the resolved interactive label is preserved as `interactive` rather
        than falling through to the content heuristic, since that label is a
        human/`/find-task` signal the content heuristic cannot see and would
        otherwise be silently downgraded to `ready` on the next re-triage.
        """
        label_lower = {lbl.lower() for lbl in task.labels}
        human_label = self.resolve_label("human_only", triage_cfg)
        if (human_label and human_label.lower() in label_lower) or is_human_only(task.labels):
            # Matches what _heuristic_assess() returns for its own hardcoded
            # 'agent:human-only' check, but built directly here so a renamed
            # label (which _heuristic_assess() cannot see) is honoured too.
            return TaskAssessment(
                suitability="human_only",
                confidence=0.95,
                reasoning="Issue is labeled as human-only.",
                estimated_complexity="moderate",
                suggested_role="triage",
            )

        # (title prefixes, labels, suitability, pattern name, reasoning consequence)
        patterns = (
            (
                triage_cfg.skip_title_prefixes,
                triage_cfg.skip_labels,
                "human_only",
                "skip",
                "not suitable for agent work",
            ),
            (
                triage_cfg.interactive_title_prefixes,
                triage_cfg.interactive_labels,
                "interactive",
                "interactive",
                "recommended for an interactive session",
            ),
        )
        for prefixes, labels, suitability, pattern_name, consequence in patterns:
            match = self._match_pattern(task, prefixes, labels)
            if match:
                return TaskAssessment(
                    suitability=suitability,  # type: ignore[arg-type]
                    confidence=0.9,
                    reasoning=f"{match} matches {pattern_name} pattern; {consequence}.",
                    estimated_complexity="moderate",
                    suggested_role="triage",
                )

        interactive_label = self.resolve_label("interactive", triage_cfg)
        if (interactive_label and interactive_label.lower() in label_lower) or is_interactive_recommended(task.labels):
            return TaskAssessment(
                suitability="interactive",
                confidence=0.95,
                reasoning="Issue is labeled as interactive-recommended.",
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
                task_type="triage",
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

            # Validate output with retry on failure
            try:
                triage_budget = ctx.config.agent.max_budget / 10
                triage_spent = result.cost_usd
                last_retry_text = result.text

                async def retry_triage(retry_prompt: str) -> LLMResult:
                    """Retry function for schema validation."""
                    nonlocal triage_spent, last_retry_text
                    remaining = triage_budget - triage_spent
                    if remaining <= Decimal("0"):
                        raise RuntimeError("Triage budget exhausted, cannot retry")
                    max_retry_budget = max(Decimal("0"), remaining)
                    retry_result = await invoke(
                        retry_prompt,
                        model=model,
                        task_type="triage",
                        cwd=ctx.project_dir,
                        max_budget_usd=max_retry_budget,
                        timeout=120,
                    )
                    triage_spent += retry_result.cost_usd
                    last_retry_text = retry_result.text
                    await record_cost(
                        retry_result,
                        phase="triage_retry",
                        issue=str(task.id),
                        task_run_id=ctx.task_run_id,
                        model_selection_reason=f"{model_reason}:retry" if model_reason else "retry",
                    )
                    return retry_result

                data, retry_cost = await validate_step_output(
                    raw_text=result.text,
                    schema=get_triage_schema(),
                    llm_invoke=retry_triage,
                    original_prompt=prompt,
                    max_retries=2,
                )
                ctx.add_cost(retry_cost)

                return self._assessment_from_dict(data)
            except ValidationError as ve:
                log.warning("triage.validation_failed", issue=task.id, error=str(ve), exc_info=True)
                ctx.add_cost(ve.retry_cost)
                assessment = self._parse_llm_assessment_strict(last_retry_text)
                if assessment:
                    return assessment

        except Exception as exc:  # noqa: BLE001 (LLM assessment falls back to the heuristic path on any failure)
            log.warning("triage.llm_fallback", error=str(exc), exc_info=True)

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
        llm_bound: list[tuple[int, Task]] = []

        triage_cfg = ctx.config.triage
        for i, task in enumerate(tasks):
            assessment = self.heuristic_assess(task, triage_cfg)
            has_body = bool(task.body and task.body.strip())
            if not has_body or assessment.suitability in _LLM_SHORT_CIRCUIT_SUITABILITIES:
                heuristic_results.append((i, task, assessment))
                continue
            llm_bound.append((i, task))
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
                cwd=ctx.project_dir,
                task_type="triage",
            )
        except Exception as exc:  # noqa: BLE001 (batch API falls back to sequential invocation on any failure)
            log.warning("triage.batch_fallback", error=str(exc), exc_info=True)
            # Only the LLM-bound subset falls back; heuristic_results was
            # already short-circuited out of the batch for a deterministic
            # reason (a config pattern or a sticky existing label), and
            # handing those tasks to the LLM here would let it override that
            # verdict, including erasing a human_only/interactive label.
            fallback_results = await self._sequential_fallback([t for _, t in llm_bound], ctx)
            llm_fallback_results = [(idx, t, a) for (idx, _), (t, a) in zip(llm_bound, fallback_results)]
            all_fallback_results = heuristic_results + llm_fallback_results
            all_fallback_results.sort(key=lambda x: x[0])
            return [(t, a) for _, t, a in all_fallback_results]

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
                except (OSError, RuntimeError, SQLAlchemyError):
                    log.warning("triage.batch_cost_record_failed", issue=task.id, exc_info=True)

                assessment = self._parse_llm_assessment(br.result.text)
                if assessment:
                    llm_results.append((idx, task, assessment))
                    continue

            llm_results.append((idx, task, self.heuristic_assess(task, triage_cfg)))

        covered_indices = {i for i, _, _ in heuristic_results} | {i for i, _, _ in llm_results}
        for i, task in enumerate(tasks):
            if i not in covered_indices:
                log.warning("triage.batch_missing_result", issue=task.id)
                llm_results.append((i, task, self.heuristic_assess(task, triage_cfg)))

        all_results = heuristic_results + llm_results
        all_results.sort(key=lambda x: x[0])
        return [(t, a) for _, t, a in all_results]

    async def _sequential_fallback(
        self,
        tasks: list[Task],
        ctx: ExecutionContext,
    ) -> list[tuple[Task, TaskAssessment]]:
        """Fall back to sequential LLM assessment for each task.

        Re-checks the same deterministic short-circuit `assess_tasks_batch()`
        applies before ever handing a task to the LLM here, as defense in
        depth for any caller that passes in a task the LLM must not
        re-litigate (a config pattern match, or an existing human_only/
        interactive label).
        """
        triage_cfg = ctx.config.triage
        results: list[tuple[Task, TaskAssessment]] = []
        for task in tasks:
            pre = self.heuristic_assess(task, triage_cfg)
            if pre.suitability in _LLM_SHORT_CIRCUIT_SUITABILITIES:
                results.append((task, pre))
                continue
            assessment = await self.assess_task_with_llm(task, ctx)
            results.append((task, assessment))
        return results

    def _heuristic_assess(self, task: Task) -> TaskAssessment:
        """Quick heuristic-based assessment without LLM.

        Uses structured signals (body length, section headings, code refs,
        labels) to produce high-confidence results, reducing LLM fallback.
        """
        # Check labels first: human-only takes priority regardless of body content
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
    def _assessment_from_dict(data: dict, *, strict: bool = True) -> TaskAssessment:
        """Build a TaskAssessment from a parsed LLM response dict.

        Args:
            data: Parsed LLM response.
            strict: When True, missing core fields (suitability, confidence,
                reasoning) raise KeyError. When False, defaults are used
                with lowered confidence.
        """
        _CORE_REQUIRED = ("suitability", "confidence", "reasoning")

        if strict:
            for key in _CORE_REQUIRED:
                if key not in data:
                    raise KeyError(f"Missing required field: {key}")

        core_defaults_used = any(key not in data for key in _CORE_REQUIRED)
        if core_defaults_used and not strict:
            missing = [k for k in _CORE_REQUIRED if k not in data]
            log.warning("triage.assessment_defaults_applied", missing_fields=missing)
        raw_confidence = float(data.get("confidence", 0.7))
        confidence = min(raw_confidence, 0.5) if core_defaults_used else raw_confidence

        return TaskAssessment(
            suitability=data.get("suitability", "needs_spec"),
            confidence=confidence,
            reasoning=data.get("reasoning", ""),
            missing_context=data.get("missing_context", []),
            estimated_complexity=data.get("estimated_complexity", "moderate"),
            suggested_role=data.get("suggested_role", "researcher"),
            sub_tasks=data.get("sub_tasks", []),
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
        """Parse Claude's JSON response into a TaskAssessment (lenient fallback)."""
        try:
            cleaned = _strip_markdown_fencing(text.strip())

            data = json.loads(cleaned)

            return self._assessment_from_dict(data, strict=False)
        except (KeyError, ValueError) as exc:
            log.warning("triage.parse_failed", error=str(exc), exc_info=True)
            return None

    def _parse_llm_assessment_strict(self, text: str) -> TaskAssessment | None:
        """Parse Claude's JSON response strictly (no defaults for required fields).

        Used after validation exhaustion to detect broken LLM output that should
        fall back to heuristic assessment.
        """
        try:
            cleaned = _strip_markdown_fencing(text.strip())
            data = json.loads(cleaned)
            return self._assessment_from_dict(data, strict=True)
        except (KeyError, ValueError) as exc:
            log.warning("triage.parse_strict_failed", error=str(exc), exc_info=True)
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
        effective_body = task.body
        quality_gate_ran = assessment.suitability == "ready" and not quality.meets_threshold(
            triage_cfg.min_quality_score
        )
        if quality_gate_ran:
            assessment, enriched_body, quality = await self._apply_quality_gate(
                ctx, task, quality, triage_cfg, assessment
            )
            if enriched_body is not None:
                effective_body = enriched_body

        # The enrichment write (post_comment/edit_body) happens before the
        # label write: apply_suitability_label() deliberately propagates an
        # add_label failure (see test_apply_suitability_label_add_failure_
        # preserves_previous_label), and when _apply_quality_gate() deferred
        # its own write to avoid writing the enriched body twice, that
        # failure must not be the thing that discards a successful, paid
        # enrichment. Labels are the cheaper side effect to lose: a dropped
        # label is corrected by the next re-triage, a dropped enrichment is
        # an LLM call paid for and thrown away.
        suggested_assignee = await self._resolve_assignee_suggestion(ctx, task, assessment)

        assessment_section = self._build_assessment_comment(
            task, assessment, quality, suggested_assignee, body=effective_body
        )
        if triage_cfg.mode == "comment":
            await ctx.adapter.post_comment(ctx.issue_number, assessment_section)
        elif triage_cfg.write_body:
            updated_body = (effective_body or "").rstrip() + "\n\n" + assessment_section
            await ctx.adapter.edit_body(ctx.issue_number, updated_body)

        if triage_cfg.auto_label:
            # known_labels=task.labels (skipping the refetch) is only safe when
            # no LLM round trip separated this snapshot from the write: the
            # quality gate is the one that can take tens of seconds.
            known_labels = None if quality_gate_ran else task.labels
            await self.apply_suitability_label(
                ctx.adapter, ctx.issue_number, assessment.suitability, triage_cfg, known_labels=known_labels
            )

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
    ) -> tuple[TaskAssessment, str | None, QualityScore]:
        """Apply quality gate: attempt enrichment, re-score, or downgrade to needs_spec.

        Returns the resulting assessment, and, when enrichment wrote a new
        body, that body plus the `QualityScore` re-scored against it (the
        original `quality` otherwise): callers must build any further body
        edit (e.g. the assessment-comment append in `execute()`), and any
        quality figure they report, on top of this pair rather than the
        stale pre-enrichment snapshot, or the enrichment is overwritten, and
        its score misreported, in the same run that produced it.

        When `execute()` is going to append the assessment section itself
        (`triage_cfg.mode != "comment" and triage_cfg.write_body`), the
        enrichment write here is skipped: that append already writes the
        enriched body, and writing it twice in a row is a redundant tracker
        call. For `mode == "comment"` or `write_body == False`, `execute()`
        never writes the body itself, so this is the only write that
        persists the enrichment and it must happen here.
        """
        llm_assessment: TaskAssessment | None = None
        defer_write = triage_cfg.mode != "comment" and triage_cfg.write_body

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
                        if not defer_write:
                            await ctx.adapter.edit_body(ctx.issue_number, result.enriched_body)
                        # A non-ready verdict here means the LLM identified the issue as
                        # needing a human or an interactive session, not just a better-written
                        # body; enrichment fixing the body's structure does not change that,
                        # so this wins ahead of the enrichment-success path, but the body it
                        # wrote is kept either way.
                        if llm_assessment.suitability in ("interactive", "human_only"):
                            return llm_assessment, result.enriched_body, new_quality
                        return original_assessment, result.enriched_body, new_quality
                    log.info(
                        "triage.enrichment_insufficient",
                        issue=ctx.issue_number,
                        old_score=quality.total,
                        new_score=new_quality.total,
                        threshold=triage_cfg.min_quality_score,
                    )
                # A non-ready verdict here means the LLM identified the issue as needing
                # a human or an interactive session, not just a better-written body; this
                # wins ahead of the needs_spec downgrade below.
                if llm_assessment.suitability in ("interactive", "human_only"):
                    return llm_assessment, None, quality

        missing = []
        if not quality.has_acceptance_criteria:
            missing.append(_ACCEPTANCE_CRITERIA)
        if not quality.has_objective:
            missing.append("objective section")
        if not quality.has_description:
            missing.append("detailed description")
        log.info("triage.quality_gate_blocked", issue=ctx.issue_number, score=quality.total)

        if llm_assessment:
            return (
                TaskAssessment(
                    suitability="needs_spec",
                    confidence=llm_assessment.confidence,
                    reasoning=llm_assessment.reasoning,
                    missing_context=llm_assessment.missing_context or missing,
                    estimated_complexity=llm_assessment.estimated_complexity,
                    suggested_role="triage",
                ),
                None,
                quality,
            )

        return (
            TaskAssessment(
                suitability="needs_spec",
                confidence=0.9,
                reasoning=f"Issue body quality score {quality.total}/8 is below threshold "
                f"{triage_cfg.min_quality_score}; needs specification work.",
                missing_context=missing,
                estimated_complexity=original_assessment.estimated_complexity,
                suggested_role="triage",
            ),
            None,
            quality,
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
                task_type="triage",
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
            assessment = self._assessment_from_dict(assessment_data, strict=False)

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
        except Exception as exc:  # noqa: BLE001 (enrichment is optional; the assessment stands without it)
            log.warning("triage.assess_enrich_failed", error=str(exc), exc_info=True)

        return None

    async def _resolve_assignee_suggestion(
        self, ctx: ExecutionContext, task: Task, assessment: TaskAssessment
    ) -> Person | None:
        """LDAP-based assignee suggestion; auto-assigns only on an unambiguous match.

        Returns the candidate (for display in the assessment comment) regardless of
        whether it was confirmed. Only a confirmed candidate is actually assigned.

        The assignment call is best-effort, like the rest of this LDAP path:
        a failed `assign_to_user` is logged and swallowed rather than raised,
        so it can never abort the caller's still-pending enrichment write.
        """
        if assessment.suitability != "ready" or not ctx.config.ldap.enabled:
            return None
        candidate, confirmed = await self._suggest_assignee(ctx, task)
        if candidate and confirmed:
            try:
                await ctx.adapter.assign_to_user(ctx.issue_number, candidate.uid)
            except Exception:  # noqa: BLE001 (best-effort assignment; must not block the enrichment write)
                log.warning("triage.assign_to_user_failed", issue=ctx.issue_number, exc_info=True)
        return candidate

    async def _suggest_assignee(self, ctx: ExecutionContext, task: Task) -> tuple[Person | None, bool]:
        """Query LDAP for a candidate assignee based on the issue's declared component.

        Returns ``(candidate, confirmed)``. ``confirmed`` is only True when the
        component/area label resolves through ``ldap.team_mapping`` to exactly one
        group member: a fuzzy name/uid/email match is never auto-assigned, since
        result ordering does not establish component or cost-center ownership.

        Best-effort: any missing precondition (LDAP disabled, unavailable, no
        VPN, no component to search on) returns ``(None, False)`` so triage
        proceeds without an assignee suggestion.
        """
        from sova.adapters.ldap_client import create_ldap_client

        client = create_ldap_client(ctx.config.ldap)
        if client is None:
            return None, False

        query = task.components[0] if task.components else None
        if not query:
            area_labels = [lbl.split(":", 1)[1] for lbl in task.labels if lbl.startswith("area:")]
            query = area_labels[0] if area_labels else None
        if not query:
            return None, False

        if not await client.check_connectivity():
            log.warning("triage.ldap_vpn_unavailable", issue=ctx.issue_number)
            return None, False

        group_cn = ctx.config.ldap.team_mapping.get(query)
        if group_cn:
            confirmed = await self._resolve_via_team_mapping(ctx, client, group_cn)
            if confirmed:
                return confirmed, True

        try:
            people = await client.search_people(query)
        except Exception:  # noqa: BLE001 (LDAP lookup is best-effort; triage must never block on it)
            log.warning("triage.ldap_search_failed", issue=ctx.issue_number, exc_info=True)
            return None, False

        return (people[0], False) if people else (None, False)

    async def _resolve_via_team_mapping(
        self, ctx: ExecutionContext, client: LdapClient, group_cn: str
    ) -> Person | None:
        """Resolve a single unambiguous owner from a configured component-to-group mapping."""
        try:
            member_uids = await client.get_group_members(group_cn)
        except Exception:  # noqa: BLE001 (LDAP lookup is best-effort; triage must never block on it)
            log.warning("triage.ldap_group_lookup_failed", issue=ctx.issue_number, group=group_cn, exc_info=True)
            return None

        if len(member_uids) != 1:
            if member_uids:
                log.info(
                    "triage.ldap_team_mapping_ambiguous",
                    issue=ctx.issue_number,
                    group=group_cn,
                    member_count=len(member_uids),
                )
            return None

        return await client.get_person(member_uids[0])

    def _build_assessment_comment(
        self,
        task: Task,
        assessment: TaskAssessment,
        quality: QualityScore | None = None,
        suggested_assignee: Person | None = None,
        body: str | None = None,
    ) -> str:
        """Build a triage assessment section to append to the issue body.

        `body` defaults to `task.body`; pass the post-enrichment body
        explicitly so "Has description" reflects what was actually written,
        not a stale pre-enrichment snapshot.
        """
        effective_body = body if body is not None else task.body
        has_body = bool(effective_body and effective_body.strip())
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

        if suggested_assignee:
            parts.extend(self._format_suggested_assignee(suggested_assignee))

        return "\n".join(parts)

    @staticmethod
    def _format_suggested_assignee(person: Person) -> list[str]:
        """Render the 'Suggested assignee (LDAP)' section for the assessment comment."""
        name = person.display_name or person.uid
        details = ", ".join(d for d in (person.job_title, person.cost_center_desc) if d)
        line = f"**{name}** (`{person.uid}`)"
        if details:
            line += f": {details}"
        return ["\n### Suggested assignee (LDAP)\n", line]
