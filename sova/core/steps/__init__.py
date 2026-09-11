"""Workflow step registry.

Provides the built-in ordered step lists for each pipeline variant, a
name-to-class registry (STEP_REGISTRY), and builders that turn user-configured
step name lists into instantiated pipelines.
"""

from __future__ import annotations

from sova.core.steps.address_external_findings import AddressExternalFindingsStep
from sova.core.steps.address_review import AddressReviewStep
from sova.core.steps.assess import AssessStep
from sova.core.steps.base import BaseStep
from sova.core.steps.capture_baseline import CaptureBaselineStep
from sova.core.steps.commit import CommitStep
from sova.core.steps.confidence_score import ConfidenceScoreStep
from sova.core.steps.create_pr import CreatePRStep
from sova.core.steps.create_worktree import WorktreeStep
from sova.core.steps.develop import DevelopStep
from sova.core.steps.ensure_worktree import EnsureWorktreeStep
from sova.core.steps.extract_memory import ExtractMemoryStep
from sova.core.steps.fetch_task import FetchTaskStep
from sova.core.steps.generate_tasks import GenerateTasksStep
from sova.core.steps.handoff_to_reviewer import HandoffToReviewerStep
from sova.core.steps.handoff_to_user import HandoffToUserStep
from sova.core.steps.monitor_ci import MonitorCIStep
from sova.core.steps.push import PushStep
from sova.core.steps.rearrange_commits import RearrangeCommitsStep
from sova.core.steps.rebase import RebaseStep
from sova.core.steps.research import ResearchStep
from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep
from sova.core.steps.scan_project import ScanProjectStep
from sova.core.steps.self_review import SelfReviewStep
from sova.core.steps.simplify import SimplifyStep
from sova.core.steps.spec import SpecStep
from sova.core.steps.sync import SyncStep
from sova.core.steps.validate import ValidateStep
from sova.core.steps.validate_tasks import ValidateTasksStep
from sova.core.steps.wait_for_external_reviews import WaitForExternalReviewsStep
from sova.utils.logging import get_logger

log = get_logger(component="core.steps")


def get_developer_steps() -> list[BaseStep]:
    """Return the ordered step list for the Developer pipeline.

    Ends with a handoff to the Reviewer agent (auto-spawned by the
    dashboard's control service).
    """
    return [
        SyncStep(),
        AssessStep(),
        WorktreeStep(),
        CaptureBaselineStep(),
        DevelopStep(),
        SimplifyStep(),
        SelfReviewStep(),
        CommitStep(),
        ValidateStep(),
        PushStep(),
        CreatePRStep(),
        WaitForExternalReviewsStep(),
        AddressExternalFindingsStep(),
        MonitorCIStep(),
        ConfidenceScoreStep(),
        ExtractMemoryStep(),
        HandoffToReviewerStep(),
    ]


def get_address_review_steps() -> list[BaseStep]:
    """Return the step list for a Developer respawned to address review findings.

    Picks up from the review findings, fixes them, pushes, and hands
    off to the user for final review.
    """
    return [
        EnsureWorktreeStep(),
        RebaseStep(),
        AddressReviewStep(),
        RearrangeCommitsStep(),
        ValidateStep(),
        PushStep(),
        MonitorCIStep(),
        ResolveExternalReviewsStep(),
        ExtractMemoryStep(),
        HandoffToUserStep(),
    ]


def get_researcher_steps() -> list[BaseStep]:
    """Return the ordered step list for the Researcher pipeline.

    Fetches the task, runs interactive codebase research via the /research
    command, and extracts learnings. No worktree or git operations.
    """
    return [
        FetchTaskStep(),
        ResearchStep(),
        SpecStep(),
        ExtractMemoryStep(),
    ]


def get_researcher_step_names() -> list[str]:
    """Return the ordered step name list for the Researcher pipeline."""
    return [s.name for s in get_researcher_steps()]


def get_developer_step_names() -> list[str]:
    """Return the ordered step name list for the Developer pipeline."""
    return [s.name for s in get_developer_steps()]


def get_address_review_step_names() -> list[str]:
    """Return the ordered step name list for the address-review pipeline."""
    return [s.name for s in get_address_review_steps()]


def get_planner_steps() -> list[BaseStep]:
    """Return the ordered step list for the Planner pipeline."""
    return [
        ScanProjectStep(),
        GenerateTasksStep(),
        ValidateTasksStep(),
        ExtractMemoryStep(),
    ]


def get_planner_step_names() -> list[str]:
    """Return the ordered step name list for the Planner pipeline."""
    return [s.name for s in get_planner_steps()]


STEP_REGISTRY: dict[str, type[BaseStep]] = {
    step_cls.name: step_cls
    for step_cls in (
        AddressExternalFindingsStep,
        AddressReviewStep,
        AssessStep,
        CaptureBaselineStep,
        CommitStep,
        ConfidenceScoreStep,
        CreatePRStep,
        WorktreeStep,
        DevelopStep,
        EnsureWorktreeStep,
        ExtractMemoryStep,
        FetchTaskStep,
        GenerateTasksStep,
        HandoffToReviewerStep,
        HandoffToUserStep,
        MonitorCIStep,
        PushStep,
        RearrangeCommitsStep,
        RebaseStep,
        ResearchStep,
        ResolveExternalReviewsStep,
        ScanProjectStep,
        SelfReviewStep,
        SimplifyStep,
        SpecStep,
        SyncStep,
        ValidateStep,
        ValidateTasksStep,
        WaitForExternalReviewsStep,
    )
}

# Ordering constraints: (earlier_step, later_step). Both steps must be present
# in the pipeline for the constraint to apply; violated constraints produce
# non-fatal warnings from validate_pipeline().
_ORDERING_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("create_worktree", "develop"),
    ("commit", "push"),
    ("push", "create_pr"),
)


def build_pipeline(step_names: list[str]) -> list[BaseStep]:
    """Instantiate a pipeline from an ordered list of step names.

    Raises ValueError listing available step names if any name is unknown,
    or listing duplicated names if any step appears more than once.
    """
    unknown = [name for name in step_names if name not in STEP_REGISTRY]
    if unknown:
        available = ", ".join(sorted(STEP_REGISTRY))
        raise ValueError(f"Unknown pipeline step(s): {', '.join(unknown)}. Available steps: {available}")
    dupes = sorted({name for name in step_names if step_names.count(name) > 1})
    if dupes:
        raise ValueError(f"Duplicate pipeline step(s): {', '.join(dupes)}")
    return [STEP_REGISTRY[name]() for name in step_names]


def validate_pipeline(step_names: list[str]) -> list[str]:
    """Check ordering constraints and return a list of warning messages.

    Unknown step names are not checked here; build_pipeline() raises for
    those. Ordering violations are non-fatal (logged, not raised).
    """
    warnings: list[str] = []
    for earlier, later in _ORDERING_CONSTRAINTS:
        if earlier not in step_names or later not in step_names:
            continue
        if step_names.index(earlier) > step_names.index(later):
            warnings.append(f"step '{earlier}' should come before '{later}'")
    return warnings


def build_configured_pipeline(default_names: list[str], configured_names: list[str]) -> list[BaseStep]:
    """Build a pipeline from user-configured step names, falling back to defaults when empty.

    Ordering warnings are logged (non-fatal); unknown step names raise via
    build_pipeline().
    """
    names = configured_names if configured_names else default_names
    for warning in validate_pipeline(names):
        log.warning("pipeline.ordering_warning", warning=warning)
    return build_pipeline(names)
