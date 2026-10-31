"""Tests for sova.supervisor.suitability: the unified suitability classifier."""

from __future__ import annotations

from sova.adapters.base import Task, TaskState
from sova.supervisor.dependency_graph import DependencyGraph
from sova.supervisor.suitability import (
    AGENT_ELIGIBLE_MODES,
    SuitabilityMode,
    assess_suitability,
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

    def test_graph_missing_task_is_blocked(self) -> None:
        task = _task(1)
        other_graph = DependencyGraph([_task(2)])
        result = assess_suitability(task, other_graph)
        assert result.mode == "blocked"
        assert result.reasons == [{"source": "graph", "value": "unavailable"}]

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
