"""Workflow step registry.

Provides the ordered list of steps for the Developer pipeline.
"""

from __future__ import annotations

from sova.core.steps.address_review import AddressReviewStep
from sova.core.steps.assess import AssessStep
from sova.core.steps.automated_review import AutomatedReviewStep
from sova.core.steps.base import BaseStep
from sova.core.steps.commit import CommitStep
from sova.core.steps.complete import CompleteStep
from sova.core.steps.create_pr import CreatePRStep
from sova.core.steps.create_worktree import WorktreeStep
from sova.core.steps.develop import DevelopStep
from sova.core.steps.monitor_ci import MonitorCIStep
from sova.core.steps.push import PushStep
from sova.core.steps.self_review import SelfReviewStep
from sova.core.steps.simplify import SimplifyStep
from sova.core.steps.sync import SyncStep
from sova.core.steps.validate import ValidateStep


def get_developer_steps() -> list[BaseStep]:
    """Return the ordered step list for the Developer pipeline."""
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
        AutomatedReviewStep(),
        AddressReviewStep(),
        CompleteStep(),
    ]


def get_developer_step_names() -> list[str]:
    """Return the ordered step name list for the Developer pipeline."""
    return [s.name for s in get_developer_steps()]
