"""work_stage(): the one stage an issue gets on the Work board, and the precedence between stages."""

from __future__ import annotations

import itertools

import pytest

from sova.adapters.base import TaskState
from sova.core.state import TaskStatus
from sova.dashboard.services.work_state import WorkItemState, WorkStage, work_stage
from sova.supervisor.suitability import SuitabilityMode

_STATES = sorted({*(s.value for s in WorkItemState), *(s.value for s in TaskState), "not_a_state", ""})
_MODES = [None, *(m.value for m in SuitabilityMode), "not_a_mode"]
_RUN_STATUSES = [None, *(s.value for s in TaskStatus), "not_a_status"]


@pytest.mark.parametrize("state", _STATES)
def test_every_input_gets_exactly_one_stage(state: str) -> None:
    for mode, run_status, queued in itertools.product(_MODES, _RUN_STATUSES, (False, True)):
        assert work_stage(state, mode, run_status, queued=queued) in set(WorkStage), (state, mode, run_status, queued)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        # 1. done outranks everything
        ({"state": "merged", "mode": "unclassified", "run_status": "failed", "queued": True}, "done"),
        ({"state": "on_qa", "mode": "human"}, "done"),
        # 2. running outranks review, needs and yours
        ({"state": "agent_running", "mode": "epic", "run_status": "failed", "queued": True}, "running"),
        ({"state": "in_progress"}, "running"),
        # 3. review outranks a failed run and the queue
        ({"state": "pr_ci_failed", "run_status": "failed", "queued": True}, "review"),
        ({"state": "in_review", "mode": "unclassified"}, "review"),
        # 4. needs: merge-ready PR, spec awaiting approval, exhausted budget, or a failed latest run
        ({"state": "pr_ready_to_merge", "mode": "interactive", "queued": True}, "needs"),
        ({"state": "spec_review", "mode": "autonomous"}, "needs"),
        ({"state": "pr_review_exhausted"}, "needs"),
        ({"state": "triaged", "mode": "autonomous", "run_status": "failed", "queued": True}, "needs"),
        ({"state": "triaged", "mode": "unclassified", "run_status": "failed"}, "needs"),
        # a later successful run replaces the latest run, so it is no failure
        ({"state": "triaged", "mode": "autonomous", "run_status": "done"}, "next"),
        # 5. yours outranks blocked and queued
        ({"state": "triaged", "mode": "unclassified", "queued": True}, "yours"),
        ({"state": "triaged", "mode": "epic"}, "yours"),
        ({"state": "triaged", "mode": "human"}, "yours"),
        ({"state": "researched", "mode": "interactive", "queued": True}, "yours"),
        ({"state": "human_only", "mode": "blocked"}, "yours"),
        # 6. blocked outranks queued
        ({"state": "triaged", "mode": "blocked", "queued": True}, "blocked"),
        # 7. queued, then 8. next
        ({"state": "researched", "mode": "autonomous", "queued": True}, "queued"),
        ({"state": "researched", "mode": "autonomous"}, "next"),
        ({"state": "backlog", "mode": None}, "next"),
        ({"state": "never_heard_of_it", "mode": "never_heard_of_it"}, "next"),
    ],
)
def test_precedence(kwargs: dict, expected: str) -> None:
    assert (
        work_stage(kwargs["state"], kwargs.get("mode"), kwargs.get("run_status"), queued=kwargs.get("queued", False))
        == expected
    )


def test_stage_order_is_the_board_column_order() -> None:
    assert [s.value for s in WorkStage] == ["next", "queued", "running", "review", "needs", "yours", "blocked", "done"]
