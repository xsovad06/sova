"""Workflow step registry.

Provides ordered step lists for the Researcher and Developer pipeline variants.
"""

from __future__ import annotations

from sova.core.steps.address_external_findings import AddressExternalFindingsStep
from sova.core.steps.address_review import AddressReviewStep
from sova.core.steps.assess import AssessStep
from sova.core.steps.base import BaseStep
from sova.core.steps.commit import CommitStep
from sova.core.steps.create_pr import CreatePRStep
from sova.core.steps.create_worktree import WorktreeStep
from sova.core.steps.develop import DevelopStep
from sova.core.steps.extract_memory import ExtractMemoryStep
from sova.core.steps.fetch_task import FetchTaskStep
from sova.core.steps.handoff_to_reviewer import HandoffToReviewerStep
from sova.core.steps.handoff_to_user import HandoffToUserStep
from sova.core.steps.monitor_ci import MonitorCIStep
from sova.core.steps.push import PushStep
from sova.core.steps.rebase import RebaseStep
from sova.core.steps.research import ResearchStep
from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep
from sova.core.steps.self_review import SelfReviewStep
from sova.core.steps.simplify import SimplifyStep
from sova.core.steps.spec import SpecStep
from sova.core.steps.sync import SyncStep
from sova.core.steps.validate import ValidateStep
from sova.core.steps.wait_for_external_reviews import WaitForExternalReviewsStep


def get_developer_steps() -> list[BaseStep]:
    """Return the ordered step list for the Developer pipeline.

    Ends with a handoff to the Reviewer agent (auto-spawned by the
    dashboard's control service).
    """
    return [
        SyncStep(),
        AssessStep(),
        WorktreeStep(),
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
        ExtractMemoryStep(),
        HandoffToReviewerStep(),
    ]


def get_address_review_steps() -> list[BaseStep]:
    """Return the step list for a Developer respawned to address review findings.

    Picks up from the review findings, fixes them, pushes, and hands
    off to the user for final review.
    """
    return [
        RebaseStep(),
        AddressReviewStep(),
        CommitStep(),
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
