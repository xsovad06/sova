"""work_stage(): the one stage an issue gets on the Work board, and the precedence between stages."""

from __future__ import annotations

import itertools

import pytest

from sova.adapters.base import TaskState
from sova.core.state import TaskStatus
from sova.supervisor.suitability import SuitabilityMode
from sova.work.state import WorkItemState, WorkStage, work_stage

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
        (
            {"state": "merged", "mode": "unclassified", "run_status": "failed", "queued": True, "active_run": True},
            "done",
        ),
        ({"state": "on_qa", "mode": "human"}, "done"),
        # 2. running outranks review, needs and yours
        ({"state": "agent_running", "mode": "epic", "run_status": "failed", "queued": True}, "running"),
        ({"state": "in_progress"}, "running"),
        ({"state": "in_progress", "run_status": "done"}, "running"),
        # an in-progress issue whose latest run failed has no agent: it needs the user (Resume), not Running
        ({"state": "in_progress", "run_status": "failed"}, "needs"),
        ({"state": "agent_running", "run_status": "failed"}, "running"),
        ({"state": "pr_conflicted", "active_run": True}, "running"),
        # 3. review outranks a failed run and the queue
        ({"state": "pr_ci_failed", "run_status": "failed", "queued": True}, "review"),
        ({"state": "in_review", "mode": "unclassified"}, "review"),
        # 4. needs: merge-ready PR, spec awaiting approval, exhausted budget, stale in-review label or failed latest run
        ({"state": "pr_ready_to_merge", "mode": "interactive", "queued": True}, "needs"),
        ({"state": "spec_review", "mode": "autonomous"}, "needs"),
        ({"state": "pr_review_exhausted"}, "needs"),
        ({"state": "pr_merged_issue_open", "mode": "autonomous", "queued": True}, "needs"),
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
        work_stage(
            kwargs["state"],
            kwargs.get("mode"),
            kwargs.get("run_status"),
            queued=kwargs.get("queued", False),
            active_run=kwargs.get("active_run", False),
        )
        == expected
    )


def test_stage_order_is_the_board_column_order() -> None:
    assert [s.value for s in WorkStage] == ["next", "queued", "running", "review", "needs", "yours", "blocked", "done"]


class TestStartAction:
    """start_action(): the run a card's Start button launches is the one the issue's stage calls for (#1418)."""

    @pytest.mark.parametrize(
        ("state", "role", "label"),
        [
            ("backlog", "triage", "Triage"),
            ("triaged", "researcher", "Research"),
            ("researched", "developer", "Develop"),
            ("in_progress", "developer", "Resume"),
        ],
    )
    def test_a_pre_pr_state_starts_the_agent_its_stage_needs(self, state: str, role: str, label: str) -> None:
        from sova.work.state import start_action

        action = start_action(state, 1418)

        assert action is not None
        assert action["handler"] == "start_agent"
        assert action["handler_args"] == {"role": role, "issue": "1418"}
        assert action["label"] == label

    @pytest.mark.parametrize(
        "state", ["needs_spec", "human_only", "done", "agent_running", "pr_ci_running", "pr_ready_to_merge", "", "?"]
    )
    def test_no_run_starts_from_a_parked_pr_or_unknown_state(self, state: str) -> None:
        from sova.work.state import start_action

        assert start_action(state, 7) is None
