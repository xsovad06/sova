"""Panel review: focused dimension reviewers over a shared diff.

By default every dimension resolving to the same model is reviewed in a single
combined LLM call per diff chunk (``review.panel.combined``), so a five
dimension panel costs one call per chunk instead of five. A group holding a
single dimension keeps the focused single-dimension prompt, which costs the
same one call. Setting ``combined`` to false restores one call per dimension;
that path is also the automatic fallback when a combined response cannot be
parsed. Findings are deduplicated by (file, line proximity, category) and
aggregated into the same ReviewResult shape used by the single-reviewer path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from sova.adapters.base import Task
from sova.config.models import ReviewPanelConfig
from sova.llm.client import invoke
from sova.roles._review_comments import (
    ReviewFinding,
    ReviewResult,
    _chunk_diff,
    _compact_spec_ref,
    _findings_from_data,
    _format_addressed_findings,
    _load_findings_json,
    _parse_findings,
)
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger(component="role.panel_review")

_DEFAULT_MODEL = "sonnet"
_CRITICAL_SEVERITY = 9

# Dimensions ordered by priority (lowest-priority last, skipped first on budget).
_DIMENSION_PRIORITY: dict[str, int] = {
    "correctness": 1,
    "security": 2,
    "error_handling": 3,
    "design": 4,
    "test_coverage": 5,
}

# (role intro, focus bullets, exclusion note) per dimension. The intro and the
# exclusion note only make sense for a single-dimension call; the combined
# prompt reuses the focus bullets alone, since one reviewer covers every
# dimension there.
_DIMENSION_SPECS: dict[str, tuple[str, str, str]] = {
    "correctness": (
        "You are a senior engineer reviewing code ONLY for correctness.",
        "- Logic errors, off-by-one mistakes, incorrect conditions\n"
        "- Null/None handling, type mismatches, wrong API usage\n"
        "- Race conditions, data corruption, state inconsistencies\n"
        "- Missing return values, unreachable code, infinite loops",
        "Ignore style, docs, performance, and security; other reviewers handle those.",
    ),
    "security": (
        "You are a security engineer reviewing code ONLY for security vulnerabilities.",
        "- Injection attacks (SQL, command, format string, template)\n"
        "- Secrets or credentials in code, insecure deserialization\n"
        "- Authentication/authorization bypass, privilege escalation\n"
        "- Path traversal, SSRF, unsafe file operations",
        "Ignore style, docs, performance, and correctness; other reviewers handle those.",
    ),
    "error_handling": (
        "You are a reliability engineer reviewing code ONLY for error handling.",
        "- Uncaught exceptions at system boundaries (I/O, network, DB)\n"
        "- Silent failures, swallowed exceptions, missing logging\n"
        "- Missing input validation at public API boundaries\n"
        "- Resource leaks (files, connections, locks not released on error)",
        "Ignore style, docs, performance, and security; other reviewers handle those.",
    ),
    "design": (
        "You are a software architect reviewing code ONLY for design quality.",
        "- API contract violations (wrong param types, missing required args)\n"
        "- Hardcoded values that should be configurable\n"
        "- Tight coupling, module-level mutable state, circular dependencies\n"
        "- Violations of established project patterns and conventions",
        "Ignore style, docs, performance, and security; other reviewers handle those.",
    ),
    "test_coverage": (
        "You are a QA engineer reviewing code ONLY for testing gaps.",
        "- Untested error paths and edge cases in changed code\n"
        "- Assertions that don't actually verify behavior (e.g. assert True)\n"
        "- Missing negative test cases for validation logic\n"
        "- Test isolation issues (shared state, order-dependent tests)",
        "Ignore style, docs, performance, and security; other reviewers handle those.",
    ),
}

_DIMENSION_PROMPTS: dict[str, str] = {
    dim: f"{intro}\nFocus exclusively on:\n{focus}\n{exclusion}"
    for dim, (intro, focus, exclusion) in _DIMENSION_SPECS.items()
}


def _focus_block(dimension: str) -> str:
    """Return the focus bullets for *dimension*, or a generic line if unknown."""
    spec = _DIMENSION_SPECS.get(dimension)
    return spec[1] if spec else f"- Any {dimension} issues in the changed code"


def _context_block(
    task: Task,
    diff: str,
    files: list[str],
    spec_sections: dict[str, str] | None,
    addressed_findings: list[dict] | None,
) -> str:
    """Build the PR context, file list, and diff shared by every review prompt."""
    file_list = "\n".join(f"- {f}" for f in files)

    spec_block = ""
    if spec_sections:
        parts = [f"### {heading}\n{content}" for heading, content in spec_sections.items()]
        spec_block = "\n\n## Spec Context\n" + "\n\n".join(parts)

    addressed_block = _format_addressed_findings(addressed_findings)

    description_block = f"\n**Description**: {task.body}" if not spec_sections and task.body else ""

    return f"""## PR Context
**Issue**: {task.title}{description_block}
{spec_block}
{addressed_block}

## Changed Files
{file_list}

## Diff
```
{diff}
```"""


def _build_dimension_prompt(
    dimension: str,
    task: Task,
    diff: str,
    files: list[str],
    spec_sections: dict[str, str] | None = None,
    addressed_findings: list[dict] | None = None,
) -> str:
    """Build a focused review prompt for a single dimension."""
    preamble = _DIMENSION_PROMPTS.get(dimension, f"Review the code for {dimension} issues.")

    return f"""{preamble}

{_context_block(task, diff, files, spec_sections, addressed_findings)}

## Rules
- Report ALL findings for your dimension, regardless of severity.
- Score each finding 1-10 (10 = critical, 1 = nitpick).
- Be specific: exact file paths and line numbers from the diff.
- If nothing in your area is wrong, return an empty findings list.

## Output Format
Return ONLY a JSON object (no markdown fences, no extra text):
{{
  "findings": [
    {{
      "file": "path/to/file.py",
      "line": 42,
      "severity": 7,
      "category": "{dimension}",
      "description": "Concise description",
      "suggestion": "Specific fix"
    }}
  ],
  "summary": "1-sentence assessment for this dimension."
}}"""


def _build_combined_prompt(
    dimensions: list[str],
    task: Task,
    diff: str,
    files: list[str],
    spec_sections: dict[str, str] | None = None,
    addressed_findings: list[dict] | None = None,
) -> str:
    """Build one prompt covering every dimension in a model group."""
    sections = "\n\n".join(f"### {dim}\n{_focus_block(dim)}" for dim in dimensions)
    categories = "|".join(dimensions)
    # One concrete value: a pipe-joined placeholder here gets copied verbatim into
    # findings, which breaks category-keyed deduplication.
    example_category = dimensions[0]
    summaries = ",\n    ".join(f'"{dim}": "1-sentence assessment for {dim}."' for dim in dimensions)

    return f"""You are a review panel. Review the diff below against every dimension listed, \
one pass per dimension, and report all findings in a single response.

## Review Dimensions
{sections}

{_context_block(task, diff, files, spec_sections, addressed_findings)}

## Rules
- Cover EVERY dimension above. Do not stop because another dimension already found issues.
- Report ALL findings, regardless of severity.
- Set "category" to the dimension the finding belongs to: {categories}.
- Score each finding 1-10 (10 = critical, 1 = nitpick).
- Be specific: exact file paths and line numbers from the diff.
- If a dimension is clean, report no findings for it and say so in its summary.

## Output Format
Return ONLY a JSON object (no markdown fences, no extra text):
{{
  "findings": [
    {{
      "file": "path/to/file.py",
      "line": 42,
      "severity": 7,
      "category": "{example_category}",
      "description": "Concise description",
      "suggestion": "Specific fix"
    }}
  ],
  "summaries": {{
    {summaries}
  }}
}}"""


# Any one of these keys marks a JSON object as a review payload rather than an
# unrelated object recovered from prose (e.g. a refusal rendered as JSON).
_RESPONSE_KEYS = frozenset({"findings", "summaries", "summary"})


@dataclass
class _CombinedResponse:
    """Parsed payload of one combined multi-dimension review call."""

    findings: list[ReviewFinding] = field(default_factory=list)
    summaries: dict[str, str] = field(default_factory=dict)
    group_summary: str = ""
    parsed: bool = False


def _parse_multi_dimension_findings(text: str) -> _CombinedResponse:
    """Parse a combined response into findings plus per-dimension summaries.

    ``parsed`` is False only when no review payload could be recovered: that
    is what triggers the per-dimension fallback. A parsed response with zero
    findings is a genuinely clean review, but an object carrying none of
    ``_RESPONSE_KEYS`` is not a review at all and must not be read as clean.
    """
    data = _load_findings_json(text)
    if data is None or not _RESPONSE_KEYS & data.keys():
        log.warning("panel_review.parse_failed", text_preview=text[:200])
        return _CombinedResponse()

    raw_summaries = data.get("summaries")
    summaries = (
        {str(dim): str(value) for dim, value in raw_summaries.items() if value}
        if isinstance(raw_summaries, dict)
        else {}
    )

    return _CombinedResponse(
        findings=_findings_from_data(data),
        summaries=summaries,
        group_summary=str(data.get("summary") or ""),
        parsed=True,
    )


def _summary_fragments(dimensions: list[str], response: _CombinedResponse) -> list[str]:
    """Format per-dimension summaries, falling back to a group-level summary."""
    fragments = [f"{dim}: {response.summaries[dim]}" for dim in dimensions if response.summaries.get(dim)]
    if not fragments and response.group_summary:
        fragments.append(f"{'+'.join(dimensions)}: {response.group_summary}")
    return fragments


def _group_dimensions_by_model(
    dimensions: list[str],
    panel_config: ReviewPanelConfig,
) -> list[tuple[str, list[str]]]:
    """Group dimensions by resolved model, preserving the priority order."""
    groups: dict[str, list[str]] = {}
    for dim in dimensions:
        groups.setdefault(panel_config.dimension_models.get(dim, _DEFAULT_MODEL), []).append(dim)
    return list(groups.items())


def _is_duplicate(candidate: ReviewFinding, kept: list[ReviewFinding], line_proximity: int) -> bool:
    """Check if *candidate* duplicates any finding already in *kept*."""
    for existing in kept:
        if existing.file != candidate.file:
            continue
        if existing.category != candidate.category:
            continue
        if (
            existing.line is not None
            and candidate.line is not None
            and abs(existing.line - candidate.line) <= line_proximity
        ):
            return True
    return False


def deduplicate_findings(
    findings: list[ReviewFinding],
    line_proximity: int = 3,
) -> list[ReviewFinding]:
    """Deduplicate findings by (file, line proximity, category).

    When two findings have the same file and category, and their lines are
    within *line_proximity* of each other, keep the higher-severity one.
    """
    if not findings:
        return []

    # Sort by severity descending so we keep the highest-severity first
    sorted_findings = sorted(findings, key=lambda f: f.severity, reverse=True)
    kept: list[ReviewFinding] = []

    for candidate in sorted_findings:
        if not _is_duplicate(candidate, kept, line_proximity):
            kept.append(candidate)

    return kept


def _estimate_call_cost(model: str) -> Decimal:
    """Estimate the minimum cost of one review call based on model tier."""
    return {
        "opus": Decimal("0.05"),
        "sonnet": Decimal("0.01"),
        "haiku": Decimal("0.002"),
    }.get(model, Decimal("0.01"))


@dataclass
class _PanelState:
    """Aggregation state threaded across diff chunks and model groups."""

    result: ReviewResult
    budget_remaining: Decimal | None
    skipped_dimensions: set[str] = field(default_factory=set)
    findings: list[ReviewFinding] = field(default_factory=list)
    critical_exit: bool = False

    def record_call(self, cost: Decimal) -> None:
        """Charge one LLM call against the review total and remaining budget."""
        self.result.total_cost += cost
        if self.budget_remaining is not None:
            self.budget_remaining -= cost

    def add_summary(self, fragment: str) -> None:
        """Append a summary fragment to the aggregated review summary."""
        self.result.summary = f"{self.result.summary} | {fragment}" if self.result.summary else fragment

    def note_critical(self, findings: list[ReviewFinding], dimensions: list[str], chunk_index: int) -> bool:
        """Stop the review early when any finding is severe enough. Returns True when it did."""
        if not any(f.severity >= _CRITICAL_SEVERITY for f in findings):
            return False
        log.info("panel_review.critical_exit", dimensions=dimensions, chunk=chunk_index + 1)
        self.critical_exit = True
        return True


@dataclass
class _ChunkRequest:
    """Everything the review prompts need for one diff chunk."""

    task: Task
    diff: str
    files: list[str]
    panel_config: ReviewPanelConfig
    index: int
    spec_sections: dict[str, str] | None = None
    addressed_findings: list[dict] | None = None
    cwd: Path | str | None = None

    @property
    def is_first(self) -> bool:
        return self.index == 0


def _affordable_dimensions(state: _PanelState, dimensions: list[str], model: str) -> list[str]:
    """Return the dimensions still worth calling, marking unaffordable ones skipped.

    The per-model floor is scaled by group size: a combined call bundling
    several dimensions' focus sections produces proportionally more output
    (findings + summaries per dimension) than a single-dimension call, so the
    floor must grow with the group or the guard silently gets weaker as
    ``dimension_models`` grouping changes how many dimensions share a call.
    """
    active = [dim for dim in dimensions if dim not in state.skipped_dimensions]
    if not active:
        return []

    estimated_cost = _estimate_call_cost(model) * max(1, len(active))
    if state.budget_remaining is not None and state.budget_remaining < estimated_cost:
        state.skipped_dimensions.update(active)
        log.warning(
            "panel_review.budget_skip",
            dimensions=active,
            budget_remaining=str(state.budget_remaining),
            estimated_cost=str(estimated_cost),
        )
        return []

    return active


def _log_unknown_categories(findings: list[ReviewFinding], dimensions: list[str]) -> None:
    """Log findings labelled with a category outside the requested dimensions."""
    requested = set(dimensions)
    unknown = [f.category for f in findings if f.category not in requested]
    if unknown:
        log.info("panel_review.unknown_category", count=len(unknown), categories=sorted(set(unknown)))


async def _review_dimensions(state: _PanelState, dimensions: list[str], request: _ChunkRequest) -> None:
    """Review a chunk with one LLM call per dimension."""
    for dim in dimensions:
        model = request.panel_config.dimension_models.get(dim, _DEFAULT_MODEL)
        if not _affordable_dimensions(state, [dim], model):
            continue

        prompt = _build_dimension_prompt(
            dim,
            request.task,
            request.diff,
            request.files,
            spec_sections=request.spec_sections,
            addressed_findings=request.addressed_findings,
        )
        try:
            llm_result = await invoke(prompt, model=model, task_type=f"review_{dim}", cwd=request.cwd)
        except Exception as exc:
            log.warning("panel_review.dimension_failed", dimension=dim, error=str(exc))
            continue

        state.record_call(llm_result.cost_usd)
        findings, summary = _parse_findings(llm_result.text)
        state.findings.extend(findings)
        if request.is_first and summary:
            state.add_summary(f"{dim}: {summary}")

        if state.note_critical(findings, [dim], request.index):
            return


async def _review_groups(state: _PanelState, groups: list[tuple[str, list[str]]], request: _ChunkRequest) -> None:
    """Review a chunk with one combined LLM call per model group.

    A group holding a single dimension costs one call either way, so it takes
    the focused single-dimension prompt instead of the weaker panel framing.
    This is a deliberate deviation from a "no special-casing" combined path:
    a lone dimension gets the stronger single-dimension role framing (intro
    plus the "other reviewers handle those" exclusion) and its own
    ``review_{dim}`` task_type for ``llm.routing`` purposes, at the cost of
    an ``llm.routing.review_panel`` override never applying to it.
    """
    for model, group in groups:
        active = _affordable_dimensions(state, group, model)
        if not active:
            continue

        if len(active) == 1:
            await _review_dimensions(state, active, request)
            if state.critical_exit:
                return
            continue

        prompt = _build_combined_prompt(
            active,
            request.task,
            request.diff,
            request.files,
            spec_sections=request.spec_sections,
            addressed_findings=request.addressed_findings,
        )
        try:
            llm_result = await invoke(prompt, model=model, task_type="review_panel", cwd=request.cwd)
        except Exception as exc:
            log.warning("panel_review.group_failed", dimensions=active, error=str(exc))
            continue

        state.record_call(llm_result.cost_usd)
        response = _parse_multi_dimension_findings(llm_result.text)

        if not response.parsed:
            log.warning("panel_review.combined_fallback", dimensions=active, chunk=request.index + 1)
            await _review_dimensions(state, active, request)
            if state.critical_exit:
                return
            continue

        _log_unknown_categories(response.findings, active)
        state.findings.extend(response.findings)
        if request.is_first:
            for fragment in _summary_fragments(active, response):
                state.add_summary(fragment)

        if state.note_critical(response.findings, active, request.index):
            return


async def run_panel_review(
    task: Task,
    diff: str,
    files: list[str],
    panel_config: ReviewPanelConfig,
    spec_sections: dict[str, str] | None = None,
    cwd: Path | str | None = None,
    budget_remaining: Decimal | None = None,
    addressed_findings: list[dict] | None = None,
) -> ReviewResult:
    """Run the dimension reviewers and aggregate their results.

    In combined mode (the default) every dimension sharing a model is reviewed
    in a single call per diff chunk; otherwise each dimension gets its own call
    in priority order (correctness > security > ...). Findings are deduplicated
    and merged into a single ReviewResult.
    """
    state = _PanelState(result=ReviewResult(), budget_remaining=budget_remaining)
    chunks = _chunk_diff(diff)
    dimensions = sorted(panel_config.dimensions, key=lambda d: _DIMENSION_PRIORITY.get(d, 99))
    groups = _group_dimensions_by_model(dimensions, panel_config)

    for chunk_idx, chunk in enumerate(chunks):
        if state.critical_exit:
            break

        request = _ChunkRequest(
            task=task,
            diff=chunk,
            files=files,
            panel_config=panel_config,
            index=chunk_idx,
            spec_sections=spec_sections if chunk_idx == 0 else _compact_spec_ref(spec_sections),
            addressed_findings=addressed_findings if chunk_idx == 0 else None,
            cwd=cwd,
        )

        if panel_config.combined:
            await _review_groups(state, groups, request)
        else:
            await _review_dimensions(state, dimensions, request)

    if state.skipped_dimensions:
        log.info("panel_review.skipped_dimensions", dimensions=sorted(state.skipped_dimensions))

    result = state.result
    result.findings = deduplicate_findings(state.findings, line_proximity=panel_config.line_proximity)

    if not result.findings and not result.summary:
        result.summary = "All dimensions report no issues: code looks good."

    log.info(
        "panel_review.complete",
        raw_findings=len(state.findings),
        deduped_findings=len(result.findings),
        dimensions_run=len(dimensions) - len(state.skipped_dimensions),
        groups=[dims for _, dims in groups] if panel_config.combined else [[dim] for dim in dimensions],
        cost=str(result.total_cost),
    )

    return result
