"""Data models for the planner pipeline.

Holds scan results, proposed tasks, and validation outcomes that flow
through the planner steps via ExecutionContext.plan_result.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProjectScanResult:
    """Aggregated project state gathered by ScanProjectStep."""

    open_issues: list[dict] = field(default_factory=list)
    recent_commits: list[str] = field(default_factory=list)
    project_structure: list[str] = field(default_factory=list)
    tech_stack: list[str] = field(default_factory=list)
    label_summary: dict[str, int] = field(default_factory=dict)
    milestone_summary: list[str] = field(default_factory=list)
    raw_summary: str = ""


@dataclass
class PlannedTask:
    """A single task proposed by the planner LLM."""

    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    priority: str = "medium"
    complexity: str = "medium"
    rationale: str = ""


@dataclass
class PlanResult:
    """Accumulator for planner pipeline state."""

    scan: ProjectScanResult | None = None
    proposed_tasks: list[PlannedTask] = field(default_factory=list)
    valid_tasks: list[PlannedTask] = field(default_factory=list)
    rejected_reasons: list[str] = field(default_factory=list)
