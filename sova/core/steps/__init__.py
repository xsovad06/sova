"""Workflow step registry.

Provides ordered step lists for the Developer pipeline variants.
"""

from __future__ import annotations

from sova.core.steps.address_review import AddressReviewStep
from sova.core.steps.assess import AssessStep
from sova.core.steps.base import BaseStep
from sova.core.steps.commit import CommitStep
from sova.core.steps.create_pr import CreatePRStep
from sova.core.steps.create_worktree import WorktreeStep
from sova.core.steps.develop import DevelopStep
from sova.core.steps.handoff_to_reviewer import HandoffToReviewerStep
from sova.core.steps.handoff_to_user import HandoffToUserStep
from sova.core.steps.monitor_ci import MonitorCIStep
from sova.core.steps.push import PushStep
from sova.core.steps.self_review import SelfReviewStep
from sova.core.steps.simplify import SimplifyStep
from sova.core.steps.sync import SyncStep
from sova.core.steps.validate import ValidateStep


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
        MonitorCIStep(),
        HandoffToReviewerStep(),
    ]


def get_address_review_steps() -> list[BaseStep]:
    """Return the step list for a Developer respawned to address review findings.

    Picks up from the review findings, fixes them, pushes, and hands
    off to the user for final review.
    """
    return [
        AddressReviewStep(),
        CommitStep(),
        ValidateStep(),
        PushStep(),
        HandoffToUserStep(),
    ]


def get_developer_step_names() -> list[str]:
    """Return the ordered step name list for the Developer pipeline."""
    return [s.name for s in get_developer_steps()]


def get_address_review_step_names() -> list[str]:
    """Return the ordered step name list for the address-review pipeline."""
    return [s.name for s in get_address_review_steps()]
