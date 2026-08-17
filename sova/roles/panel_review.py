"""Panel review -- sequential focused dimension reviewers.

Runs multiple LLM calls sequentially, each focused on a single review
dimension (correctness, security, error_handling, design, test_coverage).
Findings are deduplicated by (file, line proximity, category) and aggregated
into the same ReviewResult shape used by the single-reviewer path.
"""

from __future__ import annotations

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
    _format_addressed_findings,
    _parse_findings,
)
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger(component="role.panel_review")

# Dimensions ordered by priority (lowest-priority last, skipped first on budget).
_DIMENSION_PRIORITY: dict[str, int] = {
    "correctness": 1,
    "security": 2,
    "error_handling": 3,
    "design": 4,
    "test_coverage": 5,
}

_DIMENSION_PROMPTS: dict[str, str] = {
    "correctness": (
        "You are a senior engineer reviewing code ONLY for correctness.\n"
        "Focus exclusively on:\n"
        "- Logic errors, off-by-one mistakes, incorrect conditions\n"
        "- Null/None handling, type mismatches, wrong API usage\n"
        "- Race conditions, data corruption, state inconsistencies\n"
        "- Missing return values, unreachable code, infinite loops\n"
        "Ignore style, docs, performance, and security -- other reviewers handle those."
    ),
    "security": (
        "You are a security engineer reviewing code ONLY for security vulnerabilities.\n"
        "Focus exclusively on:\n"
        "- Injection attacks (SQL, command, format string, template)\n"
        "- Secrets or credentials in code, insecure deserialization\n"
        "- Authentication/authorization bypass, privilege escalation\n"
        "- Path traversal, SSRF, unsafe file operations\n"
        "Ignore style, docs, performance, and correctness -- other reviewers handle those."
    ),
    "error_handling": (
        "You are a reliability engineer reviewing code ONLY for error handling.\n"
        "Focus exclusively on:\n"
        "- Uncaught exceptions at system boundaries (I/O, network, DB)\n"
        "- Silent failures, swallowed exceptions, missing logging\n"
        "- Missing input validation at public API boundaries\n"
        "- Resource leaks (files, connections, locks not released on error)\n"
        "Ignore style, docs, performance, and security -- other reviewers handle those."
    ),
    "design": (
        "You are a software architect reviewing code ONLY for design quality.\n"
        "Focus exclusively on:\n"
        "- API contract violations (wrong param types, missing required args)\n"
        "- Hardcoded values that should be configurable\n"
        "- Tight coupling, module-level mutable state, circular dependencies\n"
        "- Violations of established project patterns and conventions\n"
        "Ignore style, docs, performance, and security -- other reviewers handle those."
    ),
    "test_coverage": (
        "You are a QA engineer reviewing code ONLY for testing gaps.\n"
        "Focus exclusively on:\n"
        "- Untested error paths and edge cases in changed code\n"
        "- Assertions that don't actually verify behavior (e.g. assert True)\n"
        "- Missing negative test cases for validation logic\n"
        "- Test isolation issues (shared state, order-dependent tests)\n"
        "Ignore style, docs, performance, and security -- other reviewers handle those."
    ),
}


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
    file_list = "\n".join(f"- {f}" for f in files)

    spec_block = ""
    if spec_sections:
        parts = [f"### {heading}\n{content}" for heading, content in spec_sections.items()]
        spec_block = "\n\n## Spec Context\n" + "\n\n".join(parts)

    addressed_block = _format_addressed_findings(addressed_findings)

    description_block = f"\n**Description**: {task.body}" if not spec_sections and task.body else ""

    return f"""{preamble}

## PR Context
**Issue**: {task.title}{description_block}
{spec_block}
{addressed_block}

## Changed Files
{file_list}

## Diff
```
{diff}
```

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


def _estimate_dimension_cost(model: str) -> Decimal:
    """Estimate minimum cost for a dimension call based on model tier."""
    return {
        "opus": Decimal("0.05"),
        "sonnet": Decimal("0.01"),
        "haiku": Decimal("0.002"),
    }.get(model, Decimal("0.01"))


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
    """Run dimension reviewers sequentially and aggregate results.

    Each dimension gets its own LLM call in priority order
    (correctness > security > ...). Findings are deduplicated and
    merged into a single ReviewResult.
    """
    result = ReviewResult()
    chunks = _chunk_diff(diff)
    dimensions = list(panel_config.dimensions)
    dimensions.sort(key=lambda d: _DIMENSION_PRIORITY.get(d, 99))

    skipped_dimensions: set[str] = set()
    all_findings: list[ReviewFinding] = []
    critical_exit = False

    for chunk_idx, chunk in enumerate(chunks):
        if critical_exit:
            break
        chunk_spec = spec_sections if chunk_idx == 0 else _compact_spec_ref(spec_sections)

        for dim in dimensions:
            if dim in skipped_dimensions:
                continue

            model = panel_config.dimension_models.get(dim, "sonnet")
            estimated_cost = _estimate_dimension_cost(model)
            if budget_remaining is not None and budget_remaining < estimated_cost:
                skipped_dimensions.add(dim)
                log.warning(
                    "panel_review.budget_skip",
                    dimension=dim,
                    budget_remaining=str(budget_remaining),
                    estimated_cost=str(estimated_cost),
                )
                continue

            chunk_addressed = addressed_findings if chunk_idx == 0 else None
            prompt = _build_dimension_prompt(
                dim,
                task,
                chunk,
                files,
                spec_sections=chunk_spec,
                addressed_findings=chunk_addressed,
            )
            try:
                dim_findings, dim_summary, dim_cost = await _run_dimension(dim, prompt, model=model, cwd=cwd)
            except Exception as exc:
                log.warning("panel_review.dimension_failed", dimension=dim, error=str(exc))
                continue

            result.total_cost += dim_cost
            all_findings.extend(dim_findings)
            if budget_remaining is not None:
                budget_remaining -= dim_cost

            if chunk_idx == 0 and dim_summary:
                result.summary = (
                    f"{result.summary} | {dim}: {dim_summary}" if result.summary else f"{dim}: {dim_summary}"
                )

            if any(f.severity >= 9 for f in dim_findings):
                log.info("panel_review.critical_exit", dimension=dim, chunk=chunk_idx + 1)
                critical_exit = True
                break

    if skipped_dimensions:
        log.info("panel_review.skipped_dimensions", dimensions=sorted(skipped_dimensions))

    result.findings = deduplicate_findings(all_findings, line_proximity=panel_config.line_proximity)

    if not result.findings and not result.summary:
        result.summary = "All dimensions report no issues -- code looks good."

    log.info(
        "panel_review.complete",
        raw_findings=len(all_findings),
        deduped_findings=len(result.findings),
        dimensions_run=len(dimensions) - len(skipped_dimensions),
        cost=str(result.total_cost),
    )

    return result


async def _run_dimension(
    dimension: str,
    prompt: str,
    *,
    model: str | None = None,
    cwd: Path | str | None = None,
) -> tuple[list[ReviewFinding], str, Decimal]:
    """Run a single dimension review. Returns (findings, summary, cost)."""
    llm_result = await invoke(
        prompt,
        model=model,
        task_type=f"review_{dimension}",
        cwd=cwd,
    )
    findings, summary = _parse_findings(llm_result.text)
    return findings, summary, llm_result.cost_usd
