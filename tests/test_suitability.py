"""Tests for sova.supervisor.suitability: the unified suitability classifier."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.adapters.priority import Priority
from sova.supervisor.dependency_graph import DependencyGraph
from sova.supervisor.suitability import (
    AGENT_ELIGIBLE_MODES,
    RankedTask,
    SuitabilityMode,
    assess_suitability,
    rank_and_count_next_tasks,
    rank_next_tasks,
    suitability_payload,
    unsatisfied_dependencies,
)


def _task(
    issue_id: int,
    labels: list[str] | None = None,
    body: str = "",
    state: TaskState = TaskState.BACKLOG,
) -> Task:
    return Task(id=str(issue_id), title=f"Issue #{issue_id}", body=body, state=state, labels=labels or [])


# ---------------------------------------------------------------------------
# assess_suitability: precedence and label-based modes
# ---------------------------------------------------------------------------


class TestAssessSuitabilityLabels:
    def test_no_label_no_local_record_is_unclassified(self) -> None:
        task = _task(1)
        graph = DependencyGraph([task])
        result = assess_suitability(task, graph)
        assert result.mode == "unclassified"
        assert result.reasons == []
        assert result.blocked_by == []

    def test_epic_label_takes_precedence(self) -> None:
        task = _task(1, labels=["type: epic"])
        graph = DependencyGraph([task])
        result = assess_suitability(task, graph)
        assert result.mode == "epic"

    def test_epic_label_wins_over_human_only_label(self) -> None:
        task = _task(1, labels=["type: epic", "agent:human-only"])
        graph = DependencyGraph([task])
        result = assess_suitability(task, graph)
        assert result.mode == "epic"

    def test_human_only_label_blocks(self) -> None:
        task = _task(1, labels=["agent:human-only"])
        graph = DependencyGraph([task])
        result = assess_suitability(task, graph)
        assert result.mode == "human"
        assert result.reasons == [{"source": "label", "value": "agent:human-only"}]

    def test_interactive_recommended_label(self) -> None:
        task = _task(1, labels=["agent:interactive-recommended"])
        graph = DependencyGraph([task])
        result = assess_suitability(task, graph)
        assert result.mode == "interactive"

    def test_human_only_wins_over_interactive_recommended(self) -> None:
        task = _task(1, labels=["agent:human-only", "agent:interactive-recommended"])
        graph = DependencyGraph([task])
        result = assess_suitability(task, graph)
        assert result.mode == "human"

    def test_ready_label_is_autonomous(self) -> None:
        task = _task(1, labels=["agent:ready"])
        graph = DependencyGraph([task])
        result = assess_suitability(task, graph)
        assert result.mode == "autonomous"
        assert result.reasons == [{"source": "label", "value": "agent:ready"}]


# ---------------------------------------------------------------------------
# assess_suitability: dependency-based blocking
# ---------------------------------------------------------------------------


class TestAssessSuitabilityDependencies:
    def test_open_non_epic_dependency_blocks(self) -> None:
        tasks = [
            _task(1, state=TaskState.BACKLOG),
            _task(2, body="## Dependencies\n- #1\n", state=TaskState.BACKLOG),
        ]
        graph = DependencyGraph(tasks)
        result = assess_suitability(graph.get_task(2), graph)
        assert result.mode == "blocked"
        assert result.blocked_by == [1]
        assert result.reasons == [{"source": "dependency", "value": 1}]

    def test_done_dependency_does_not_block(self) -> None:
        tasks = [
            _task(1, state=TaskState.DONE),
            _task(2, body="## Dependencies\n- #1\n", state=TaskState.BACKLOG),
        ]
        graph = DependencyGraph(tasks)
        result = assess_suitability(graph.get_task(2), graph)
        assert result.mode == "unclassified"
        assert result.blocked_by == []

    def test_epic_dependency_does_not_block(self) -> None:
        tasks = [
            _task(1, labels=["type: epic"], state=TaskState.BACKLOG),
            _task(2, body="## Dependencies\n- #1\n", state=TaskState.BACKLOG),
        ]
        graph = DependencyGraph(tasks)
        result = assess_suitability(graph.get_task(2), graph)
        assert result.mode == "unclassified"
        assert result.blocked_by == []

    def test_missing_dependency_blocks(self) -> None:
        task = _task(2, body="## Dependencies\n- #999\n", state=TaskState.BACKLOG)
        graph = DependencyGraph([task])
        result = assess_suitability(task, graph)
        assert result.mode == "blocked"
        assert result.blocked_by == [999]

    def test_blocked_precedes_interactive(self) -> None:
        tasks = [
            _task(1, state=TaskState.BACKLOG),
            _task(
                2,
                labels=["agent:interactive-recommended"],
                body="## Dependencies\n- #1\n",
                state=TaskState.BACKLOG,
            ),
        ]
        graph = DependencyGraph(tasks)
        result = assess_suitability(graph.get_task(2), graph)
        assert result.mode == "blocked"

    def test_blocked_precedes_unclassified(self) -> None:
        tasks = [
            _task(1, state=TaskState.BACKLOG),
            _task(2, body="## Dependencies\n- #1\n", state=TaskState.BACKLOG),
        ]
        graph = DependencyGraph(tasks)
        result = assess_suitability(graph.get_task(2), graph)
        assert result.mode == "blocked"

    def test_human_label_wins_over_blocked(self) -> None:
        tasks = [
            _task(1, state=TaskState.BACKLOG),
            _task(
                2,
                labels=["agent:human-only"],
                body="## Dependencies\n- #1\n",
                state=TaskState.BACKLOG,
            ),
        ]
        graph = DependencyGraph(tasks)
        result = assess_suitability(graph.get_task(2), graph)
        assert result.mode == "human"


class TestAssessSuitabilityOnHold:
    """`agent:on-hold` is a non-state label: it reports `blocked` without any open dependency."""

    _ON_HOLD_REASON = [{"source": "label", "value": "agent:on-hold"}]

    def test_on_hold_label_is_blocked_with_no_dependencies(self) -> None:
        task = _task(1, labels=["agent:on-hold"])
        result = assess_suitability(task, DependencyGraph([task]))
        assert result.mode == "blocked"
        assert result.reasons == self._ON_HOLD_REASON
        assert result.blocked_by == []

    def test_on_hold_label_is_case_insensitive(self) -> None:
        task = _task(1, labels=["Agent:On-Hold"])
        result = assess_suitability(task, DependencyGraph([task]))
        assert result.mode == "blocked"

    def test_on_hold_overrides_ready_label(self) -> None:
        task = _task(1, labels=["agent:ready", "agent:on-hold"])
        result = assess_suitability(task, DependencyGraph([task]))
        assert result.mode == "blocked"
        assert result.reasons == self._ON_HOLD_REASON

    def test_on_hold_overrides_interactive_recommended_label(self) -> None:
        task = _task(1, labels=["agent:interactive-recommended", "agent:on-hold"])
        result = assess_suitability(task, DependencyGraph([task]))
        assert result.mode == "blocked"

    def test_on_hold_overrides_local_ready_record(self) -> None:
        task = _task(1, labels=["agent:on-hold"])
        result = assess_suitability(task, DependencyGraph([task]), local_assessment="ready")
        assert result.mode == "blocked"

    def test_on_hold_overrides_local_interactive_record(self) -> None:
        task = _task(1, labels=["agent:on-hold"])
        result = assess_suitability(task, DependencyGraph([task]), local_assessment="interactive_recommended")
        assert result.mode == "blocked"

    def test_human_only_wins_over_on_hold(self) -> None:
        task = _task(1, labels=["agent:on-hold", "agent:human-only"])
        result = assess_suitability(task, DependencyGraph([task]))
        assert result.mode == "human"
        assert result.reasons == [{"source": "label", "value": "agent:human-only"}]

    def test_local_human_only_record_wins_over_on_hold(self) -> None:
        task = _task(1, labels=["agent:on-hold"])
        result = assess_suitability(task, DependencyGraph([task]), local_assessment="human_only")
        assert result.mode == "human"

    def test_epic_wins_over_on_hold(self) -> None:
        task = _task(1, labels=["type: epic", "agent:on-hold"])
        result = assess_suitability(task, DependencyGraph([task]))
        assert result.mode == "epic"

    def test_on_hold_reported_alongside_open_dependency_names_the_hold(self) -> None:
        """Both causes block; the label survives the blocker closing, so the hold is the reported reason."""
        tasks = [
            _task(1, state=TaskState.BACKLOG),
            _task(2, labels=["agent:on-hold"], body="## Dependencies\n- #1\n", state=TaskState.BACKLOG),
        ]
        graph = DependencyGraph(tasks)
        result = assess_suitability(graph.get_task(2), graph)
        assert result.mode == "blocked"
        assert result.reasons == self._ON_HOLD_REASON

    def test_hold_survives_the_blocker_closing(self) -> None:
        tasks = [
            _task(1, state=TaskState.DONE),
            _task(2, labels=["agent:on-hold"], body="## Dependencies\n- #1\n", state=TaskState.BACKLOG),
        ]
        graph = DependencyGraph(tasks)
        assert assess_suitability(graph.get_task(2), graph).mode == "blocked"

    def test_on_hold_blocks_even_when_dependency_data_is_ignored(self) -> None:
        """The human-involvement gate passes `consider_dependencies=False`; a hold is not a dependency."""
        task = _task(1, labels=["agent:on-hold"])
        result = assess_suitability(task, consider_dependencies=False)
        assert result.mode == "blocked"
        assert result.reasons == self._ON_HOLD_REASON

    def test_on_hold_ticket_is_not_agent_eligible(self) -> None:
        task = _task(1, labels=["agent:ready", "agent:on-hold"])
        result = assess_suitability(task, DependencyGraph([task]))
        assert result.mode not in AGENT_ELIGIBLE_MODES


class TestAssessSuitabilityGraphUnavailable:
    def test_none_graph_is_blocked(self) -> None:
        task = _task(1)
        result = assess_suitability(task, None)
        assert result.mode == "blocked"
        assert result.blocked_by == []
        assert result.reasons == [{"source": "graph", "value": "unavailable"}]

    def test_graph_missing_task_is_blocked_as_unavailable(self) -> None:
        """The classifier makes no gap-versus-outage distinction; the work-items endpoint refines it."""
        task = _task(1)
        other_graph = DependencyGraph([_task(2)])
        result = assess_suitability(task, other_graph)
        assert result.mode == "blocked"
        assert result.blocked_by == []
        assert result.reasons == [{"source": "graph", "value": "unavailable"}]

    def test_non_numeric_id_is_unavailable_even_with_a_graph(self) -> None:
        """An id that does not parse as an issue number can never be a graph node."""
        task = Task(id="PROJ-7", title="t", labels=[])
        result = assess_suitability(task, DependencyGraph([_task(2)]))
        assert result.mode == "blocked"
        assert result.reasons == [{"source": "graph", "value": "unavailable"}]

    def test_epic_label_wins_over_a_missing_task(self) -> None:
        result = assess_suitability(_task(1, labels=["type: epic"]), DependencyGraph([_task(2)]))
        assert result.mode == "epic"

    def test_epic_label_wins_over_unavailable_graph(self) -> None:
        task = _task(1, labels=["type: epic"])
        result = assess_suitability(task, None)
        assert result.mode == "epic"

    def test_human_only_label_wins_over_unavailable_graph(self) -> None:
        task = _task(1, labels=["agent:human-only"])
        result = assess_suitability(task, None)
        assert result.mode == "human"


# ---------------------------------------------------------------------------
# assess_suitability: local assessment fallback
# ---------------------------------------------------------------------------


class TestAssessSuitabilityLocalAssessment:
    def test_local_human_only_blocks_when_no_label(self) -> None:
        task = _task(1)
        graph = DependencyGraph([task])
        result = assess_suitability(task, graph, local_assessment="human_only")
        assert result.mode == "human"
        assert result.reasons == [{"source": "local_assessment", "value": "human_only"}]

    def test_local_interactive_recommended_blocks_when_no_label(self) -> None:
        task = _task(1)
        graph = DependencyGraph([task])
        result = assess_suitability(task, graph, local_assessment="interactive_recommended")
        assert result.mode == "interactive"

    def test_local_ready_is_autonomous(self) -> None:
        task = _task(1)
        graph = DependencyGraph([task])
        result = assess_suitability(task, graph, local_assessment="ready")
        assert result.mode == "autonomous"
        assert result.reasons == [{"source": "local_assessment", "value": "ready"}]

    def test_ready_label_wins_over_conflicting_local_human_only_record(self) -> None:
        """A tracker label always wins over a conflicting local record."""
        task = _task(1, labels=["agent:ready"])
        graph = DependencyGraph([task])
        result = assess_suitability(task, graph, local_assessment="human_only")
        assert result.mode == "autonomous"

    def test_ready_label_wins_over_conflicting_local_interactive_record(self) -> None:
        task = _task(1, labels=["agent:ready"])
        graph = DependencyGraph([task])
        result = assess_suitability(task, graph, local_assessment="interactive_recommended")
        assert result.mode == "autonomous"

    def test_blocked_dependency_wins_over_local_ready_record(self) -> None:
        tasks = [
            _task(1, state=TaskState.BACKLOG),
            _task(2, body="## Dependencies\n- #1\n", state=TaskState.BACKLOG),
        ]
        graph = DependencyGraph(tasks)
        result = assess_suitability(graph.get_task(2), graph, local_assessment="ready")
        assert result.mode == "blocked"


# ---------------------------------------------------------------------------
# consider_dependencies=False: the escape hatch for callers with no dependency data
# ---------------------------------------------------------------------------


class TestConsiderDependenciesFalse:
    def test_dependencies_are_ignored_so_interactive_survives(self) -> None:
        """`blocked` outranks `interactive`, so a label-only caller must not see deps.

        The human-involvement gate acts on `interactive` but not on `blocked`;
        classifying it against a graph that resolves its dependencies would
        turn a blockable issue into an unblockable one.
        """
        task = _task(2, labels=["agent:interactive-recommended"], body="## Dependencies\n- #1\n")
        result = assess_suitability(task, consider_dependencies=False)
        assert result.mode == SuitabilityMode.INTERACTIVE
        assert result.blocked_by == []

    def test_unclassified_rather_than_blocked(self) -> None:
        task = _task(2, body="## Dependencies\n- #1\n")
        assert assess_suitability(task, consider_dependencies=False).mode == SuitabilityMode.UNCLASSIFIED

    def test_epic_and_human_labels_still_win(self) -> None:
        epic = _task(1, labels=["type: epic"])
        human = _task(2, labels=["agent:human-only"])
        assert assess_suitability(epic, consider_dependencies=False).mode == SuitabilityMode.EPIC
        assert assess_suitability(human, consider_dependencies=False).mode == SuitabilityMode.HUMAN

    def test_no_graph_allocated_for_a_non_numeric_task_id(self) -> None:
        """A Jira-style id can never be a `DependencyGraph` node (it does a bare

        `int(t.id)`), so a caller that does not consider dependencies must
        never construct one: this must not raise.
        """
        task = Task(id="RHCLOUD-42", title="x", labels=["agent:human-only"])
        assert assess_suitability(task, consider_dependencies=False).mode == SuitabilityMode.HUMAN


class TestSuitabilityMode:
    def test_mode_is_a_str_enum_comparable_to_its_value(self) -> None:
        task = _task(1, labels=["agent:ready"])
        mode = assess_suitability(task, consider_dependencies=False).mode
        assert isinstance(mode, SuitabilityMode)
        assert mode == "autonomous"

    def test_agent_eligible_modes_are_exactly_autonomous_and_unclassified(self) -> None:
        assert AGENT_ELIGIBLE_MODES == {SuitabilityMode.AUTONOMOUS, SuitabilityMode.UNCLASSIFIED}

    def test_non_numeric_task_id_is_blocked_not_crashing(self) -> None:
        """A Jira-style id can never be a graph node, so dependency data is unavailable."""
        task = Task(id="RHCLOUD-42", title="x", labels=[])
        assert assess_suitability(task, DependencyGraph([])).mode == SuitabilityMode.BLOCKED

    def test_non_numeric_task_id_still_honours_labels(self) -> None:
        task = Task(id="RHCLOUD-42", title="x", labels=["agent:human-only"])
        assert assess_suitability(task, DependencyGraph([])).mode == SuitabilityMode.HUMAN


class TestUnsatisfiedDependenciesSharedWithDependencyGate:
    """`unsatisfied_dependencies()` is the one walk shared with `check_dependency_gate()`."""

    def test_none_graph_is_unavailable(self) -> None:
        assert unsatisfied_dependencies(1, None) is None

    def test_issue_not_in_graph_is_unavailable(self) -> None:
        graph = DependencyGraph([_task(2)])
        assert unsatisfied_dependencies(1, graph) is None

    def test_no_dependencies_is_empty_list(self) -> None:
        graph = DependencyGraph([_task(1)])
        assert unsatisfied_dependencies(1, graph) == []

    def test_open_non_epic_dependency_is_listed(self) -> None:
        tasks = [_task(1, state=TaskState.BACKLOG), _task(2, body="## Dependencies\n- #1\n")]
        graph = DependencyGraph(tasks)
        assert unsatisfied_dependencies(2, graph) == [1]

    def test_epic_dependency_is_skipped(self) -> None:
        tasks = [_task(1, labels=["type: epic"], state=TaskState.BACKLOG), _task(2, body="## Dependencies\n- #1\n")]
        graph = DependencyGraph(tasks)
        assert unsatisfied_dependencies(2, graph) == []

    def test_missing_reference_is_listed(self) -> None:
        task = _task(2, body="## Dependencies\n- #999\n")
        graph = DependencyGraph([task])
        assert unsatisfied_dependencies(2, graph) == [999]


class TestLocalAssessmentValuesMatchStorage:
    def test_local_assessment_literals_pinned_to_sole_writer(self) -> None:
        """A rename of a stored `task_assessments.suitability` value must fail

        CI here rather than silently falling through to `unclassified` (every
        unmatched `local_assessment` value fails open, since it stops
        blocking the issue).
        """
        from sova.knowledge.assessments import SUITABILITY_SHORTHANDS
        from sova.supervisor.suitability import _LOCAL_HUMAN_ONLY, _LOCAL_INTERACTIVE, _LOCAL_READY

        assert set(SUITABILITY_SHORTHANDS.values()) == {_LOCAL_READY, _LOCAL_INTERACTIVE, _LOCAL_HUMAN_ONLY}


# ---------------------------------------------------------------------------
# rank_next_tasks: the ranked "Up next" list
# ---------------------------------------------------------------------------


def _ranked_task(
    issue_id: int,
    *,
    labels: list[str] | None = None,
    state: TaskState = TaskState.RESEARCHED,
    body: str = "",
    milestone: str = "",
    ready: bool = True,
) -> Task:
    all_labels = list(labels or [])
    if ready:
        all_labels.append("agent:ready")
    return Task(
        id=str(issue_id),
        title=f"Issue #{issue_id}",
        body=body,
        state=state,
        labels=all_labels,
        milestone=milestone,
        priority=Priority.from_labels(all_labels),
    )


def _rank(
    tasks: list[Task],
    *,
    running: set[int] | None = None,
    open_prs: set[int] | None = None,
    limit: int | None = None,
) -> list[RankedTask]:
    graph = DependencyGraph(tasks)
    return rank_next_tasks(tasks, graph, running=running or set(), open_prs=open_prs or set(), limit=limit)


def _ids(ranked: list[RankedTask]) -> list[int]:
    return [int(r.task.id) for r in ranked]


class TestSuitabilityPayload:
    def test_exposes_exactly_mode_reasons_and_blocked_by(self) -> None:
        task = _task(1, labels=["agent:ready"])
        payload = suitability_payload(assess_suitability(task, DependencyGraph([task])))
        assert payload == {
            "mode": "autonomous",
            "reasons": [{"source": "label", "value": "agent:ready"}],
            "blocked_by": [],
        }

    def test_carries_blocked_by(self) -> None:
        dep = _task(1)
        task = _task(2, body="## Dependencies\n- #1\n")
        payload = suitability_payload(assess_suitability(task, DependencyGraph([dep, task])))
        assert payload["mode"] == "blocked"
        assert payload["blocked_by"] == [1]


class TestRankNextTasksPool:
    def test_keeps_autonomous_triaged_and_researched(self) -> None:
        tasks = [
            _ranked_task(1, state=TaskState.TRIAGED),
            _ranked_task(2, state=TaskState.RESEARCHED),
        ]
        assert _ids(_rank(tasks)) == [1, 2]

    @pytest.mark.parametrize(
        "state",
        [
            TaskState.BACKLOG,
            TaskState.NEEDS_SPEC,
            TaskState.IN_PROGRESS,
            TaskState.IN_REVIEW,
            TaskState.ON_QA,
            TaskState.DONE,
            TaskState.HUMAN_ONLY,
        ],
    )
    def test_other_states_are_excluded(self, state: TaskState) -> None:
        assert _rank([_ranked_task(1, state=state)]) == []

    def test_unclassified_is_not_ranked(self) -> None:
        assert _rank([_ranked_task(1, ready=False)]) == []

    @pytest.mark.parametrize(
        "labels",
        [["agent:human-only"], ["agent:interactive-recommended"], ["agent:on-hold"]],
    )
    def test_non_autonomous_modes_are_excluded(self, labels: list[str]) -> None:
        assert _rank([_ranked_task(1, labels=labels)]) == []

    def test_open_dependency_blocks_the_candidate(self) -> None:
        dep = _ranked_task(1, state=TaskState.BACKLOG, ready=False)
        blocked = _ranked_task(2, body="## Dependencies\n- #1\n")
        assert _ids(_rank([dep, blocked])) == []

    def test_done_dependency_does_not_block(self) -> None:
        dep = _ranked_task(1, state=TaskState.DONE, ready=False)
        task = _ranked_task(2, body="## Dependencies\n- #1\n")
        assert _ids(_rank([dep, task])) == [2]

    def test_epic_with_ready_label_stays_an_epic_and_is_not_ranked(self) -> None:
        assert _rank([_ranked_task(1, labels=["type: epic"])]) == []

    def test_running_issue_is_excluded(self) -> None:
        tasks = [_ranked_task(1), _ranked_task(2)]
        assert _ids(_rank(tasks, running={1})) == [2]

    def test_issue_with_open_pr_is_excluded(self) -> None:
        tasks = [_ranked_task(1), _ranked_task(2)]
        assert _ids(_rank(tasks, open_prs={2})) == [1]

    def test_issue_in_both_exclusion_sets_is_excluded_once(self) -> None:
        tasks = [_ranked_task(1), _ranked_task(2)]
        assert _ids(_rank(tasks, running={1}, open_prs={1})) == [2]

    def test_non_numeric_tracker_id_is_skipped(self) -> None:
        jira = Task(id="PROJ-7", title="Jira", state=TaskState.RESEARCHED, labels=["agent:ready"])
        numeric = _ranked_task(1)
        graph = DependencyGraph([numeric])
        assert _ids(rank_next_tasks([jira, numeric], graph, running=set(), open_prs=set())) == [1]

    def test_task_absent_from_graph_is_not_ranked(self) -> None:
        graph = DependencyGraph([_ranked_task(1)])
        stray = _ranked_task(2)
        assert rank_next_tasks([stray], graph, running=set(), open_prs=set()) == []

    def test_empty_pool_returns_empty_list(self) -> None:
        assert rank_next_tasks([], DependencyGraph([]), running=set(), open_prs=set()) == []

    def test_local_assessment_record_is_not_read(self) -> None:
        """agent:researched with a local `ready` record and no agent:ready stays unclassified."""
        from sova.knowledge.assessments import SUITABILITY_SHORTHANDS

        task = _ranked_task(1, labels=["agent:researched"], ready=False)
        graph = DependencyGraph([task])
        assert assess_suitability(task, graph, SUITABILITY_SHORTHANDS["ready"]).mode == "autonomous"
        assert assess_suitability(task, graph).mode == "unclassified"
        assert rank_next_tasks([task], graph, running=set(), open_prs=set()) == []
        assert rank_and_count_next_tasks([task], graph, running=set(), open_prs=set()) == ([], 1)


class TestRankNextTasksOrdering:
    def test_priority_orders_first(self) -> None:
        tasks = [
            _ranked_task(1, labels=["priority: low"]),
            _ranked_task(2, labels=["priority: blocker"]),
            _ranked_task(3, labels=["priority: high"]),
            _ranked_task(4, labels=["priority: medium"]),
            _ranked_task(5, labels=["priority: critical"]),
        ]
        assert _ids(_rank(tasks)) == [2, 5, 3, 4, 1]

    def test_unlabelled_and_unrecognised_priority_rank_below_low(self) -> None:
        tasks = [
            _ranked_task(1),
            _ranked_task(2, labels=["priority: urgent-ish"]),
            _ranked_task(3, labels=["priority: low"]),
        ]
        assert _ids(_rank(tasks)) == [3, 1, 2]

    def test_multiple_priority_labels_resolve_to_the_most_urgent(self) -> None:
        tasks = [
            _ranked_task(1, labels=["priority: medium"]),
            _ranked_task(2, labels=["priority: low", "priority: critical"]),
        ]
        assert _ids(_rank(tasks)) == [2, 1]

    def test_unblock_count_breaks_a_priority_tie(self) -> None:
        tasks = [
            _ranked_task(1, labels=["priority: high"]),
            _ranked_task(2, labels=["priority: high"]),
            _ranked_task(3, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #2\n"),
        ]
        assert _ids(_rank(tasks)) == [2, 1]

    def test_priority_outranks_unblock_count(self) -> None:
        tasks = [
            _ranked_task(1, labels=["priority: low"]),
            _ranked_task(2, labels=["priority: critical"]),
            _ranked_task(3, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #1\n"),
            _ranked_task(4, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #1\n"),
        ]
        assert _ids(_rank(tasks)) == [2, 1]

    def test_phase_breaks_a_priority_and_unblock_tie(self) -> None:
        tasks = [
            _ranked_task(1, milestone="Phase 3: Scale"),
            _ranked_task(2, milestone="Phase 1: Ship It"),
        ]
        assert _ids(_rank(tasks)) == [2, 1]

    def test_phase_zero_sorts_before_phase_one(self) -> None:
        tasks = [
            _ranked_task(1, milestone="Phase 1: Ship It"),
            _ranked_task(2, milestone="Phase 0: Bootstrap"),
        ]
        assert _ids(_rank(tasks)) == [2, 1]

    @pytest.mark.parametrize("milestone", ["", "Beta Release"])
    def test_missing_milestone_sorts_last(self, milestone: str) -> None:
        tasks = [_ranked_task(1, milestone=milestone), _ranked_task(2, milestone="Phase 9")]
        assert _ids(_rank(tasks)) == [2, 1]

    def test_issue_number_is_the_final_tiebreak(self) -> None:
        tasks = [_ranked_task(n) for n in (5, 3, 9, 1)]
        assert _ids(_rank(tasks)) == [1, 3, 5, 9]

    def test_order_is_independent_of_input_order(self) -> None:
        tasks = [
            _ranked_task(1, labels=["priority: high"], milestone="Phase 2"),
            _ranked_task(2, labels=["priority: high"], milestone="Phase 2"),
            _ranked_task(3, labels=["priority: high"], milestone="Phase 1"),
            _ranked_task(4, labels=["priority: low"]),
            _ranked_task(5),
        ]
        expected = _ids(_rank(tasks))
        for shuffled in (tasks[::-1], tasks[2:] + tasks[:2], [tasks[i] for i in (3, 0, 4, 2, 1)]):
            assert _ids(_rank(shuffled)) == expected

    def test_rank_is_the_one_based_position(self) -> None:
        ranked = _rank([_ranked_task(n) for n in (1, 2, 3)])
        assert [r.rank for r in ranked] == [1, 2, 3]


class TestRankNextTasksLimit:
    def test_none_is_unbounded(self) -> None:
        assert len(_rank([_ranked_task(n) for n in range(1, 8)], limit=None)) == 7

    def test_limit_truncates_but_keeps_full_ordering_ranks(self) -> None:
        ranked = _rank([_ranked_task(n) for n in range(1, 8)], limit=3)
        assert _ids(ranked) == [1, 2, 3]
        assert [r.rank for r in ranked] == [1, 2, 3]

    def test_zero_is_an_empty_list_not_unbounded(self) -> None:
        assert _rank([_ranked_task(1)], limit=0) == []

    def test_limit_larger_than_pool_returns_everything(self) -> None:
        assert len(_rank([_ranked_task(1), _ranked_task(2)], limit=10)) == 2


class TestUnblockCount:
    def _unblocks(self, tasks: list[Task], issue: int) -> int:
        ranked = {int(r.task.id): r for r in _rank(tasks)}
        return ranked[issue].unblocks

    def test_counts_direct_dependents(self) -> None:
        tasks = [
            _ranked_task(1),
            _ranked_task(2, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #1\n"),
            _ranked_task(3, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #1\n"),
        ]
        assert self._unblocks(tasks, 1) == 2

    def test_counts_transitive_dependents(self) -> None:
        tasks = [
            _ranked_task(1),
            _ranked_task(2, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #1\n"),
            _ranked_task(3, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #2\n"),
        ]
        assert self._unblocks(tasks, 1) == 2

    def test_a_shared_dependent_is_counted_once(self) -> None:
        tasks = [
            _ranked_task(1),
            _ranked_task(2, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #1\n"),
            _ranked_task(3, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #1\n"),
            _ranked_task(4, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #2\n- #3\n"),
        ]
        assert self._unblocks(tasks, 1) == 3

    def test_done_dependents_are_not_counted(self) -> None:
        tasks = [
            _ranked_task(1),
            _ranked_task(2, state=TaskState.DONE, ready=False, body="## Dependencies\n- #1\n"),
        ]
        assert self._unblocks(tasks, 1) == 0

    def test_work_behind_a_done_dependent_is_not_counted(self) -> None:
        tasks = [
            _ranked_task(1),
            _ranked_task(2, state=TaskState.DONE, ready=False, body="## Dependencies\n- #1\n"),
            _ranked_task(3, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #2\n"),
        ]
        assert self._unblocks(tasks, 1) == 0

    def test_epic_dependents_are_not_counted(self) -> None:
        tasks = [
            _ranked_task(1),
            _ranked_task(
                2,
                labels=["type: epic"],
                state=TaskState.BACKLOG,
                ready=False,
                body="## Dependencies\n- #1\n",
            ),
        ]
        assert self._unblocks(tasks, 1) == 0

    def test_work_behind_an_epic_dependent_is_not_counted(self) -> None:
        tasks = [
            _ranked_task(1),
            _ranked_task(
                2,
                labels=["type: epic"],
                state=TaskState.BACKLOG,
                ready=False,
                body="## Dependencies\n- #1\n",
            ),
            _ranked_task(3, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #2\n"),
        ]
        assert self._unblocks(tasks, 1) == 0

    def test_dependency_cycle_downstream_terminates_and_counts_each_member_once(self) -> None:
        tasks = [
            _ranked_task(1),
            _ranked_task(2, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #1\n- #3\n"),
            _ranked_task(3, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #2\n"),
        ]
        assert self._unblocks(tasks, 1) == 2

    def test_mutually_dependent_issues_are_blocked_and_never_ranked(self) -> None:
        tasks = [
            _ranked_task(1, body="## Dependencies\n- #2\n"),
            _ranked_task(2, body="## Dependencies\n- #1\n"),
        ]
        assert _rank(tasks) == []


class TestWhy:
    def test_priority_unblocks_and_state_in_fixed_order(self) -> None:
        tasks = [
            _ranked_task(1, labels=["priority: high"]),
            _ranked_task(2, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #1\n"),
            _ranked_task(3, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #1\n"),
            _ranked_task(4, state=TaskState.BACKLOG, ready=False, body="## Dependencies\n- #2\n"),
        ]
        assert _rank(tasks)[0].why == ["priority:high", "unblocks:3", "state:researched"]

    def test_unblocks_token_is_omitted_at_zero(self) -> None:
        assert _rank([_ranked_task(1, labels=["priority: low"])])[0].why == ["priority:low", "state:researched"]

    def test_phase_token_present_for_a_parsed_milestone(self) -> None:
        why = _rank([_ranked_task(1, milestone="Phase 2: Scale")])[0].why
        assert why == ["priority:undefined", "phase:2", "state:researched"]

    def test_phase_zero_keeps_its_token(self) -> None:
        assert "phase:0" in _rank([_ranked_task(1, milestone="Phase 0: Bootstrap")])[0].why

    def test_phase_token_is_omitted_for_unphased(self) -> None:
        assert not any(t.startswith("phase:") for t in _rank([_ranked_task(1)])[0].why)

    def test_state_token_follows_the_task_state(self) -> None:
        assert _rank([_ranked_task(1, state=TaskState.TRIAGED)])[0].why[-1] == "state:triaged"

    def test_mode_is_autonomous(self) -> None:
        assert _rank([_ranked_task(1)])[0].mode == SuitabilityMode.AUTONOMOUS


class TestUnclassifiedCount:
    """The count half of `rank_and_count_next_tasks()`."""

    @staticmethod
    def _count(tasks: list[Task], *, running: set[int] | None = None, open_prs: set[int] | None = None) -> int:
        _ranked, unclassified = rank_and_count_next_tasks(
            tasks, DependencyGraph(tasks), running=running or set(), open_prs=open_prs or set()
        )
        return unclassified

    def test_counts_unclassified_in_the_candidate_pool(self) -> None:
        tasks = [
            _ranked_task(1, ready=False),
            _ranked_task(2, ready=False, state=TaskState.TRIAGED),
            _ranked_task(3),
        ]
        assert self._count(tasks) == 2

    def test_backlog_and_needs_spec_are_not_counted(self) -> None:
        tasks = [
            _ranked_task(1, ready=False, state=TaskState.BACKLOG),
            _ranked_task(2, ready=False, state=TaskState.NEEDS_SPEC),
        ]
        assert self._count(tasks) == 0

    def test_running_and_open_pr_issues_are_not_counted(self) -> None:
        tasks = [_ranked_task(n, ready=False) for n in (1, 2, 3)]
        assert self._count(tasks, running={1}, open_prs={2}) == 1

    def test_other_modes_are_not_counted(self) -> None:
        tasks = [
            _ranked_task(1, labels=["agent:human-only"], ready=False),
            _ranked_task(2, labels=["agent:on-hold"], ready=False),
            _ranked_task(3, labels=["type: epic"], ready=False),
        ]
        assert self._count(tasks) == 0

    def test_empty_pool_is_zero(self) -> None:
        assert self._count([]) == 0


class TestRankAndCountNextTasks:
    """One classification pass yields the ranking `rank_next_tasks()` returns plus the unclassified count."""

    @staticmethod
    def _pool() -> list[Task]:
        return [
            _ranked_task(1, labels=["priority: low"]),
            _ranked_task(2, labels=["priority: high"]),
            _ranked_task(3, ready=False),
            _ranked_task(4, ready=False, state=TaskState.TRIAGED),
            _ranked_task(5, labels=["agent:human-only"], ready=False),
            _ranked_task(6),
        ]

    def test_ranking_equals_rank_next_tasks_and_counts_unclassified(self) -> None:
        tasks = self._pool()
        graph = DependencyGraph(tasks)
        kwargs = {"running": {6}, "open_prs": set()}

        ranked, unclassified = rank_and_count_next_tasks(tasks, graph, **kwargs)

        assert ranked == rank_next_tasks(tasks, graph, **kwargs)
        assert unclassified == 2

    def test_limit_truncates_the_ranking_but_not_the_count(self) -> None:
        tasks = self._pool()

        ranked, unclassified = rank_and_count_next_tasks(
            tasks, DependencyGraph(tasks), running=set(), open_prs=set(), limit=1
        )

        assert [r.task.id for r in ranked] == ["2"]
        assert unclassified == 2

    def test_classifies_each_candidate_once(self) -> None:
        tasks = self._pool()
        graph = DependencyGraph(tasks)

        with patch("sova.supervisor.suitability.assess_suitability", wraps=assess_suitability) as spy:
            rank_and_count_next_tasks(tasks, graph, running=set(), open_prs=set())

        assert spy.call_count == len(tasks)

    def test_empty_pool(self) -> None:
        assert rank_and_count_next_tasks([], DependencyGraph([]), running=set(), open_prs=set()) == ([], 0)
