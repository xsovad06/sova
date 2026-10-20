"""Tests for sova.supervisor.queue_maintenance."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.adapters.base import AdapterError, Task, TaskState
from sova.config.models import NetworkGuardConfig, SupervisorConfig
from sova.supervisor.dependency_graph import DependencyGraph, invalidate_graph_cache
from sova.supervisor.gates import BlockReason
from sova.supervisor.queue_maintenance import (
    _discover_ready,
    _prune_done,
    _prune_unsuitable,
    apply_planner_queue_changes,
    maintain_queue,
)


@pytest.fixture(autouse=True)
def _clear_graph_cache():
    """`build_dependency_graph` caches per repo for 180s; the fake adapters all
    share one repo name, so without this a graph built by one test answers for
    the next."""
    invalidate_graph_cache()
    yield
    invalidate_graph_cache()


@pytest.fixture
def config() -> SupervisorConfig:
    return SupervisorConfig(
        enabled=True,
        task_queue=[10, 20, 30],
        max_queue_size=10,
    )


@pytest.fixture
def adapter() -> AsyncMock:
    mock = AsyncMock()
    mock.repo = "test/repo"
    return mock


def _make_tasks(
    state_map: dict[int, TaskState],
    labels_map: dict[int, list[str]] | None = None,
) -> list[Task]:
    labels_map = labels_map or {}
    return [
        Task(id=str(issue_id), title=f"Task #{issue_id}", state=state, labels=labels_map.get(issue_id, []))
        for issue_id, state in state_map.items()
    ]


def _make_task_objects(
    state_map: dict[int, TaskState],
    labels_map: dict[int, list[str]] | None = None,
) -> dict[int, Task]:
    tasks = _make_tasks(state_map, labels_map)
    return {int(t.id): t for t in tasks}


class TestPruneDone:
    def test_removes_done_issues(self) -> None:
        task_map = {10: TaskState.DONE, 20: TaskState.RESEARCHED, 30: TaskState.IN_PROGRESS}
        result = _prune_done([10, 20, 30], task_map)
        assert result == [20, 30]

    def test_removes_human_only_issues(self) -> None:
        task_map = {10: TaskState.HUMAN_ONLY, 20: TaskState.RESEARCHED}
        result = _prune_done([10, 20], task_map)
        assert result == [20]

    def test_removes_missing_issues(self) -> None:
        task_map = {20: TaskState.RESEARCHED}
        result = _prune_done([10, 20], task_map)
        assert result == [20]

    def test_empty_queue(self) -> None:
        result = _prune_done([], {10: TaskState.DONE})
        assert result == []

    def test_all_done(self) -> None:
        task_map = {10: TaskState.DONE, 20: TaskState.DONE}
        result = _prune_done([10, 20], task_map)
        assert result == []

    def test_preserves_order(self) -> None:
        task_map = {30: TaskState.RESEARCHED, 10: TaskState.TRIAGED, 20: TaskState.IN_PROGRESS}
        result = _prune_done([30, 10, 20], task_map)
        assert result == [30, 10, 20]


class TestPruneUnsuitable:
    """Already-queued entries must be re-checked, not just new candidates (issue #1227 follow-up)."""

    def test_epic_entry_queued_before_the_label_was_added_is_pruned(self) -> None:
        task_map = {10: TaskState.RESEARCHED}
        task_objects = {10: Task(id="10", title="x", state=TaskState.RESEARCHED, labels=["type: epic"])}

        result = _prune_unsuitable([10], task_map, task_objects, graph=None, local_assessments={})
        assert result == []

    def test_interactive_recommended_entry_is_pruned(self) -> None:
        task_map = {10: TaskState.RESEARCHED}
        task_objects = {
            10: Task(id="10", title="x", state=TaskState.RESEARCHED, labels=["agent:interactive-recommended"])
        }

        result = _prune_unsuitable([10], task_map, task_objects, graph=None, local_assessments={})
        assert result == []

    def test_suitable_entry_is_kept(self) -> None:
        task_map = {10: TaskState.RESEARCHED}
        task_objects = {10: Task(id="10", title="x", state=TaskState.RESEARCHED, labels=[])}

        result = _prune_unsuitable([10], task_map, task_objects, graph=None, local_assessments={})
        assert result == [10]

    def test_in_progress_entry_is_never_pruned_even_if_now_unsuitable(self) -> None:
        """An issue an agent is already developing must not be pulled out of
        the queue because its suitability drifted mid-flight."""
        task_map = {10: TaskState.IN_PROGRESS}
        task_objects = {10: Task(id="10", title="x", state=TaskState.IN_PROGRESS, labels=["type: epic"])}

        result = _prune_unsuitable([10], task_map, task_objects, graph=None, local_assessments={})
        assert result == [10]

    def test_missing_task_object_fails_open(self) -> None:
        task_map = {10: TaskState.RESEARCHED}
        result = _prune_unsuitable([10], task_map, task_objects={}, graph=None, local_assessments={})
        assert result == [10]

    def test_local_human_only_record_prunes_entry(self) -> None:
        task_map = {10: TaskState.RESEARCHED}
        task_objects = {10: Task(id="10", title="x", state=TaskState.RESEARCHED, labels=[])}

        result = _prune_unsuitable([10], task_map, task_objects, graph=None, local_assessments={10: "human_only"})
        assert result == []

    def test_pipeline_state_label_supersedes_local_record_and_is_kept(self) -> None:
        task_map = {10: TaskState.RESEARCHED}
        task_objects = {10: Task(id="10", title="x", state=TaskState.RESEARCHED, labels=["agent:researched"])}

        result = _prune_unsuitable([10], task_map, task_objects, graph=None, local_assessments={10: "human_only"})
        assert result == [10]

    def test_dependency_blocked_entry_is_not_pruned_even_with_graph_data(self) -> None:
        """Dependency blocking is `check_dependency_gate`'s job to withhold and
        report; evicting a queued entry for it would discard a deliberately
        queued, dependency-waiting issue and lose the gate's visible BlockReason."""
        task_map = {20: TaskState.RESEARCHED}
        blocker = Task(id="10", title="blocker", state=TaskState.TRIAGED)
        blocked = Task(id="20", title="blocked", body="## Dependencies\n- #10\n", state=TaskState.RESEARCHED)
        graph = DependencyGraph([blocker, blocked])

        result = _prune_unsuitable([20], task_map, {20: blocked}, graph, local_assessments={})
        assert result == [20]

    def test_backlog_epic_entry_is_pruned(self) -> None:
        """Epics are tracking containers that rarely reach RESEARCHED, so the
        re-check must not be scoped to that one state."""
        task_map = {10: TaskState.BACKLOG}
        task_objects = {10: Task(id="10", title="x", state=TaskState.BACKLOG, labels=["type: epic"])}

        result = _prune_unsuitable([10], task_map, task_objects, graph=None, local_assessments={})
        assert result == []

    def test_triaged_interactive_recommended_entry_is_pruned(self) -> None:
        task_map = {10: TaskState.TRIAGED}
        task_objects = {10: Task(id="10", title="x", state=TaskState.TRIAGED, labels=["agent:interactive-recommended"])}

        result = _prune_unsuitable([10], task_map, task_objects, graph=None, local_assessments={})
        assert result == []

    def test_in_review_and_on_qa_entries_are_never_pruned(self) -> None:
        """Every agent-in-flight state, not just IN_PROGRESS, must be exempt."""
        task_map = {10: TaskState.IN_REVIEW, 20: TaskState.ON_QA}
        task_objects = {
            10: Task(id="10", title="x", state=TaskState.IN_REVIEW, labels=["type: epic"]),
            20: Task(id="20", title="y", state=TaskState.ON_QA, labels=["type: epic"]),
        }

        result = _prune_unsuitable([10, 20], task_map, task_objects, graph=None, local_assessments={})
        assert result == [10, 20]


class TestDiscoverReady:
    def test_discovers_researched_issues(self) -> None:
        task_map = {10: TaskState.RESEARCHED, 20: TaskState.TRIAGED, 30: TaskState.RESEARCHED}
        result = _discover_ready([], task_map, max_queue_size=10)
        assert set(result) == {10, 30}

    def test_skips_already_queued(self) -> None:
        task_map = {10: TaskState.RESEARCHED, 20: TaskState.RESEARCHED}
        result = _discover_ready([10], task_map, max_queue_size=10)
        assert result == (20,)

    def test_respects_max_queue_size(self) -> None:
        task_map = {i: TaskState.RESEARCHED for i in range(1, 20)}
        result = _discover_ready([100, 200], task_map, max_queue_size=5)
        assert len(result) == 3  # 5 - 2 existing = 3 capacity

    def test_zero_max_queue_size_means_unlimited(self) -> None:
        task_map = {i: TaskState.RESEARCHED for i in range(1, 50)}
        result = _discover_ready([], task_map, max_queue_size=0)
        assert len(result) == 49

    def test_queue_at_capacity(self) -> None:
        task_map = {10: TaskState.RESEARCHED}
        result = _discover_ready([1, 2, 3, 4, 5], task_map, max_queue_size=5)
        assert result == ()

    def test_no_ready_issues(self) -> None:
        task_map = {10: TaskState.DONE, 20: TaskState.TRIAGED}
        result = _discover_ready([], task_map, max_queue_size=10)
        assert result == ()

    def test_sorted_by_issue_number_when_no_priority(self) -> None:
        task_map = {30: TaskState.RESEARCHED, 10: TaskState.RESEARCHED, 20: TaskState.RESEARCHED}
        result = _discover_ready([], task_map, max_queue_size=10)
        assert result == (10, 20, 30)

    def test_priority_ordering_selects_higher_priority_first(self) -> None:
        task_map = {100: TaskState.RESEARCHED, 10: TaskState.RESEARCHED, 50: TaskState.RESEARCHED}
        task_objects = _make_task_objects(
            task_map,
            labels_map={
                100: ["priority: critical"],
                10: ["priority: low"],
                50: ["priority: high"],
            },
        )
        result = _discover_ready([], task_map, max_queue_size=10, task_objects=task_objects)
        assert result == (100, 50, 10)

    def test_priority_ordering_with_capacity_selects_highest(self) -> None:
        task_map = {100: TaskState.RESEARCHED, 10: TaskState.RESEARCHED, 50: TaskState.RESEARCHED}
        task_objects = _make_task_objects(
            task_map,
            labels_map={
                100: ["priority: critical"],
                10: ["priority: low"],
                50: ["priority: high"],
            },
        )
        result = _discover_ready([], task_map, max_queue_size=2, task_objects=task_objects)
        assert result == (100, 50)

    def test_priority_tiebreak_by_issue_number(self) -> None:
        task_map = {30: TaskState.RESEARCHED, 10: TaskState.RESEARCHED, 20: TaskState.RESEARCHED}
        task_objects = _make_task_objects(
            task_map,
            labels_map={
                30: ["priority: high"],
                10: ["priority: high"],
                20: ["priority: high"],
            },
        )
        result = _discover_ready([], task_map, max_queue_size=10, task_objects=task_objects)
        assert result == (10, 20, 30)

    def test_skips_human_only_issues(self) -> None:
        task_map = {10: TaskState.RESEARCHED, 20: TaskState.RESEARCHED}
        task_objects = _make_task_objects(
            task_map,
            labels_map={10: ["agent:human-only"], 20: []},
        )
        result = _discover_ready([], task_map, max_queue_size=10, task_objects=task_objects)
        assert result == (20,)

    def test_skips_human_only_case_insensitive(self) -> None:
        task_map = {10: TaskState.RESEARCHED}
        task_objects = _make_task_objects(
            task_map,
            labels_map={10: ["Agent:Human-Only"]},
        )
        result = _discover_ready([], task_map, max_queue_size=10, task_objects=task_objects)
        assert result == ()

    def test_human_only_without_task_objects_still_included(self) -> None:
        task_map = {10: TaskState.RESEARCHED}
        result = _discover_ready([], task_map, max_queue_size=10, task_objects=None)
        assert result == (10,)

    def test_skips_epic_and_interactive_issues(self) -> None:
        task_map = dict.fromkeys((10, 20, 30), TaskState.RESEARCHED)
        task_objects = _make_task_objects(
            task_map,
            labels_map={10: ["type: epic"], 20: ["agent:interactive-recommended"], 30: []},
        )
        result = _discover_ready([], task_map, max_queue_size=10, task_objects=task_objects)
        assert result == (30,)

    def test_skips_issue_with_open_dependency(self) -> None:
        task_map = {20: TaskState.RESEARCHED}
        blocker = Task(id="10", title="blocker", state=TaskState.IN_PROGRESS)
        blocked = Task(id="20", title="blocked", body="## Dependencies\n- #10\n", state=TaskState.RESEARCHED)
        graph = DependencyGraph([blocker, blocked])
        result = _discover_ready([], task_map, max_queue_size=10, task_objects={20: blocked}, graph=graph)
        assert result == ()

    def test_done_dependency_does_not_withhold_the_issue(self) -> None:
        """A dependency closed *because it is done* must not read as unresolvable."""
        task_map = {20: TaskState.RESEARCHED}
        blocker = Task(id="10", title="blocker", state=TaskState.DONE)
        blocked = Task(id="20", title="blocked", body="## Dependencies\n- #10\n", state=TaskState.RESEARCHED)
        graph = DependencyGraph([blocker, blocked])
        result = _discover_ready([], task_map, max_queue_size=10, task_objects={20: blocked}, graph=graph)
        assert result == (20,)

    def test_unavailable_graph_does_not_withhold_the_issue(self) -> None:
        """A failed graph build must fail open, not silently stop the queue filling."""
        task_map = {20: TaskState.RESEARCHED}
        blocked = Task(id="20", title="blocked", body="## Dependencies\n- #10\n", state=TaskState.RESEARCHED)
        result = _discover_ready([], task_map, max_queue_size=10, task_objects={20: blocked}, graph=None)
        assert result == (20,)

    def test_unresolved_dependency_ref_fails_closed_like_get_ready_tasks(self) -> None:
        """A dependency id the graph never resolved must withhold the issue here too.

        `DependencyGraph.get_ready_tasks()` fails closed on an unresolvable
        reference; `_discover_ready` must agree, or the dashboard renders a
        `blocked` node for an issue the queue auto-fill just queued.
        """
        blocked = Task(id="20", title="blocked", body="## Dependencies\n- #999\n", state=TaskState.RESEARCHED)
        graph = DependencyGraph([blocked])
        task_map = {20: TaskState.RESEARCHED}

        result = _discover_ready([], task_map, max_queue_size=10, task_objects={20: blocked}, graph=graph)
        assert result == ()
        assert graph.get_ready_tasks() == []

    def test_local_human_only_record_excludes_candidate(self) -> None:
        """The local `task_assessments` fallback must apply here too, not just in the gate.

        `labels=[]` here stands for a tracker (e.g. Jira) whose RESEARCHED
        state is not itself label-derived, so no pipeline-state label is
        present to supersede the local record.
        """
        task_map = {10: TaskState.RESEARCHED}
        task_objects = {10: Task(id="10", title="x", state=TaskState.RESEARCHED, labels=[])}

        result = _discover_ready(
            [], task_map, max_queue_size=10, task_objects=task_objects, local_assessments={10: "human_only"}
        )
        assert result == ()

    def test_label_wins_over_conflicting_local_ready_record(self) -> None:
        task_map = {10: TaskState.RESEARCHED}
        task_objects = {10: Task(id="10", title="x", state=TaskState.RESEARCHED, labels=["agent:human-only"])}

        result = _discover_ready(
            [], task_map, max_queue_size=10, task_objects=task_objects, local_assessments={10: "ready"}
        )
        assert result == ()

    def test_pipeline_state_label_supersedes_local_human_only_record(self) -> None:
        """On GitHub, `TaskState.RESEARCHED` is derived solely from `agent:researched`,
        so every real RESEARCHED candidate here carries that label. A local
        record must not exclude it, or the queue disagrees with
        `check_human_involvement_gate()` (which treats `agent:researched` as
        past pre-development and never consults the record for this candidate).
        """
        task_map = {10: TaskState.RESEARCHED}
        task_objects = {10: Task(id="10", title="x", state=TaskState.RESEARCHED, labels=["agent:researched"])}

        result = _discover_ready(
            [], task_map, max_queue_size=10, task_objects=task_objects, local_assessments={10: "human_only"}
        )
        assert result == (10,)


class TestMaintainQueueDependencyData:
    """`maintain_queue` must resolve dependencies against open *and* closed issues."""

    @staticmethod
    def _adapter_with_closed_dependency() -> AsyncMock:
        """An adapter whose default (open-only) listing hides the DONE dependency.

        ``get_task`` has to be configured explicitly: ``build_dependency_graph``
        drops a DONE issue that is in no active milestone and lists no
        dependencies of its own, so #10 is re-fetched individually. Left as a
        bare ``AsyncMock``, that call returns a mock whose ``body`` is not a
        string, ``build_dependency_graph`` raises ``TypeError``, and the test
        would pass through the graph-unavailable fail-open branch instead of
        the closed-dependency resolution it is meant to cover.
        """
        blocked = Task(
            id="20",
            title="blocked",
            body="## Dependencies\n- #10\n",
            state=TaskState.RESEARCHED,
        )
        done_dep = Task(id="10", title="dep", state=TaskState.DONE)

        async def _list_tasks(filters=None):
            if filters is not None and filters.state == "all":
                return [done_dep, blocked]
            return [blocked]

        async def _get_task(task_id):
            return done_dep if str(task_id) == "10" else None

        mock = AsyncMock()
        mock.repo = "test/repo"
        mock.list_tasks.side_effect = _list_tasks
        mock.get_task.side_effect = _get_task
        return mock

    async def test_done_dependency_does_not_withhold_discovery(self, tmp_path: Path) -> None:
        adapter = self._adapter_with_closed_dependency()
        config = SupervisorConfig(enabled=True, task_queue=[], max_queue_size=10)
        seen: dict[str, DependencyGraph | None] = {}
        real_discover = _discover_ready

        def _spy(*args, **kwargs):
            seen["graph"] = kwargs.get("graph", args[4] if len(args) > 4 else None)
            return real_discover(*args, **kwargs)

        with (
            patch("sova.supervisor.queue_maintenance._discover_ready", side_effect=_spy),
            patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock),
        ):
            result = await maintain_queue(adapter, config, tmp_path)

        assert result.added == (20,)
        # Pin the path taken: the graph must have been built and must carry the
        # closed dependency, otherwise this asserts the same fail-open branch as
        # the next test rather than the behaviour it names.
        graph = seen["graph"]
        assert graph is not None
        dep = graph.get_task(10)
        assert dep is not None
        assert dep.state is TaskState.DONE

    async def test_graph_build_failure_falls_back_to_labels_only(self, tmp_path: Path) -> None:
        adapter = self._adapter_with_closed_dependency()
        config = SupervisorConfig(enabled=True, task_queue=[], max_queue_size=10)

        with (
            patch(
                "sova.supervisor.queue_maintenance.build_dependency_graph",
                new_callable=AsyncMock,
                side_effect=AdapterError("rate limited"),
            ),
            patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock),
        ):
            result = await maintain_queue(adapter, config, tmp_path)

        assert result.added == (20,)


class TestMaintainQueueRespectDependencies:
    """`supervisor.respect_dependencies=False` must bypass dependency blocking here too.

    `check_dependency_gate` already honours this switch; the queue auto-fill
    is an *exclusive* filter, so bypassing it there but not here would still
    permanently withhold a dependency-blocked issue regardless of the switch.
    """

    async def test_disabled_keeps_dependency_blocked_issue_discovered(self, tmp_path: Path) -> None:
        blocker = Task(id="10", title="blocker", state=TaskState.IN_PROGRESS)
        blocked = Task(id="20", title="blocked", body="## Dependencies\n- #10\n", state=TaskState.RESEARCHED)
        adapter = AsyncMock()
        adapter.repo = "test/repo"
        adapter.list_tasks.return_value = [blocker, blocked]
        config = SupervisorConfig(enabled=True, task_queue=[], max_queue_size=10, respect_dependencies=False)

        with (
            patch("sova.supervisor.queue_maintenance.build_dependency_graph", new_callable=AsyncMock) as mock_build,
            patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock),
        ):
            result = await maintain_queue(adapter, config, tmp_path)

        mock_build.assert_not_called()
        assert result.added == (20,)

    async def test_enabled_still_withholds_dependency_blocked_issue(self, tmp_path: Path) -> None:
        """Default behavior (respect_dependencies=True) is unchanged."""
        blocker = Task(id="10", title="blocker", state=TaskState.IN_PROGRESS)
        blocked = Task(id="20", title="blocked", body="## Dependencies\n- #10\n", state=TaskState.RESEARCHED)
        adapter = AsyncMock()
        adapter.repo = "test/repo"
        adapter.list_tasks.return_value = [blocker, blocked]
        config = SupervisorConfig(enabled=True, task_queue=[], max_queue_size=10, respect_dependencies=True)

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await maintain_queue(adapter, config, tmp_path)

        assert result.added == ()


class TestMaintainQueueCostControlGates:
    """Rate-limit/connectivity short-circuits and the lazy dependency-graph build guard."""

    async def test_rate_limited_skips_entire_pass(
        self, adapter: AsyncMock, config: SupervisorConfig, tmp_path: Path
    ) -> None:
        with patch(
            "sova.supervisor.queue_maintenance.check_github_rate_limit_gate",
            return_value=BlockReason(gate="rate_limit", detail="cooling down"),
        ):
            result = await maintain_queue(adapter, config, tmp_path)

        adapter.list_tasks.assert_not_called()
        assert result.current == result.previous == tuple(config.task_queue)
        assert result.changed is False

    async def test_network_unreachable_skips_entire_pass(
        self, adapter: AsyncMock, config: SupervisorConfig, tmp_path: Path
    ) -> None:
        guard = NetworkGuardConfig(enabled=True, block_spawns=True)

        with patch(
            "sova.supervisor.queue_maintenance.check_connectivity_gate",
            return_value=BlockReason(gate="connectivity", detail="network down"),
        ):
            result = await maintain_queue(adapter, config, tmp_path, guard)

        adapter.list_tasks.assert_not_called()
        assert result.changed is False

    async def test_graph_not_built_when_queue_already_at_capacity(self, adapter: AsyncMock, tmp_path: Path) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20], max_queue_size=2)
        adapter.list_tasks.return_value = [
            Task(id="10", title="x", state=TaskState.IN_PROGRESS),
            Task(id="20", title="y", state=TaskState.IN_PROGRESS),
            Task(id="30", title="z", state=TaskState.RESEARCHED),
        ]

        with (
            patch("sova.supervisor.queue_maintenance.build_dependency_graph", new_callable=AsyncMock) as mock_build,
            patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock),
        ):
            await maintain_queue(adapter, cfg, tmp_path)

        mock_build.assert_not_called()

    async def test_graph_not_built_when_no_researched_candidates(self, adapter: AsyncMock, tmp_path: Path) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[], max_queue_size=10)
        adapter.list_tasks.return_value = [Task(id="10", title="x", state=TaskState.IN_PROGRESS)]

        with (
            patch("sova.supervisor.queue_maintenance.build_dependency_graph", new_callable=AsyncMock) as mock_build,
            patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock),
        ):
            await maintain_queue(adapter, cfg, tmp_path)

        mock_build.assert_not_called()

    async def test_session_factory_not_called_when_queue_already_at_capacity(
        self, adapter: AsyncMock, tmp_path: Path
    ) -> None:
        """Mirrors `test_graph_not_built_when_queue_already_at_capacity`: resolving
        local suitability records must be gated on there being something to
        resolve them for, just like building the dependency graph."""
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20], max_queue_size=2)
        adapter.list_tasks.return_value = [
            Task(id="10", title="x", state=TaskState.IN_PROGRESS),
            Task(id="20", title="y", state=TaskState.IN_PROGRESS),
            Task(id="30", title="z", state=TaskState.RESEARCHED),
        ]

        def exploding_factory():
            raise AssertionError("session_factory must not be called when there is nothing to resolve")

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            await maintain_queue(adapter, cfg, tmp_path, session_factory=exploding_factory, project_slug="test/repo")


class TestMaintainQueueLocalAssessment:
    """`maintain_queue` must honour the local `task_assessments` fallback end to end."""

    @pytest.fixture
    async def session_factory(self, tmp_path: Path):
        from sova.db.session import close_db, get_session_factory, init_db

        await init_db(tmp_path, run_migrations=False)
        yield await get_session_factory(tmp_path)
        await close_db()

    async def test_local_human_only_record_excludes_candidate_from_queue(
        self, adapter: AsyncMock, session_factory, tmp_path: Path
    ) -> None:
        """`labels=[]` stands for a tracker whose RESEARCHED state is not
        itself label-derived (see `test_pipeline_state_label_does_not_exclude_...`
        for the GitHub-realistic case where a pipeline-state label is present)."""
        from sova.knowledge.assessments import set_assessment

        async with session_factory() as session:
            await set_assessment(
                session, issue_number="40", project_slug="test/repo", suitability="human_only", reasoning="test"
            )

        cfg = SupervisorConfig(enabled=True, task_queue=[], max_queue_size=10)
        adapter.list_tasks.return_value = [Task(id="40", title="x", state=TaskState.RESEARCHED, labels=[])]

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await maintain_queue(
                adapter, cfg, tmp_path, session_factory=session_factory, project_slug="test/repo"
            )

        assert result.added == ()

    async def test_no_local_record_still_discovers_the_candidate(
        self, adapter: AsyncMock, session_factory, tmp_path: Path
    ) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[], max_queue_size=10)
        adapter.list_tasks.return_value = [Task(id="40", title="x", state=TaskState.RESEARCHED, labels=[])]

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await maintain_queue(
                adapter, cfg, tmp_path, session_factory=session_factory, project_slug="test/repo"
            )

        assert result.added == (40,)

    async def test_pipeline_state_label_does_not_exclude_candidate_despite_local_record(
        self, adapter: AsyncMock, session_factory, tmp_path: Path
    ) -> None:
        """On GitHub, `TaskState.RESEARCHED` is derived solely from `agent:researched`:
        every real candidate here carries that label, which must supersede a
        stale local `human_only` record the same way it does for
        `check_human_involvement_gate()`, or the queue permanently withholds
        an issue the gate would happily let through.
        """
        from sova.knowledge.assessments import set_assessment

        async with session_factory() as session:
            await set_assessment(
                session, issue_number="40", project_slug="test/repo", suitability="human_only", reasoning="test"
            )

        cfg = SupervisorConfig(enabled=True, task_queue=[], max_queue_size=10)
        adapter.list_tasks.return_value = [
            Task(id="40", title="x", state=TaskState.RESEARCHED, labels=["agent:researched"])
        ]

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await maintain_queue(
                adapter, cfg, tmp_path, session_factory=session_factory, project_slug="test/repo"
            )

        assert result.added == (40,)

    async def test_capacity_freed_by_prune_still_resolves_discovery_local_assessment(
        self, adapter: AsyncMock, session_factory, tmp_path: Path
    ) -> None:
        """Capacity for discovery is measured before `_prune_unsuitable` runs,
        but a prune in the same pass can free a slot: a newly promoted
        candidate must still get its local-assessment record resolved, or an
        issue with a stored `human_only` record is queued anyway and only
        blocked by the gate on the next poll."""
        from sova.knowledge.assessments import set_assessment

        async with session_factory() as session:
            await set_assessment(
                session, issue_number="20", project_slug="test/repo", suitability="human_only", reasoning="test"
            )

        cfg = SupervisorConfig(enabled=True, task_queue=[10], max_queue_size=1)
        adapter.list_tasks.return_value = [
            Task(id="10", title="x", state=TaskState.RESEARCHED, labels=["type: epic"]),
            Task(id="20", title="y", state=TaskState.RESEARCHED, labels=[]),
        ]

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await maintain_queue(
                adapter, cfg, tmp_path, session_factory=session_factory, project_slug="test/repo"
            )

        assert 10 in result.removed
        assert result.added == ()


class TestMaintainQueuePrunesPreExistingUnsuitable:
    """An already-queued entry that drifted unsuitable must be pruned, not just excluded at discovery time."""

    async def test_pre_existing_epic_entry_is_pruned(self, adapter: AsyncMock, tmp_path: Path) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[10], max_queue_size=10)
        adapter.list_tasks.return_value = [Task(id="10", title="x", state=TaskState.RESEARCHED, labels=["type: epic"])]

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await maintain_queue(adapter, cfg, tmp_path)

        assert 10 in result.removed
        assert 10 not in result.current

    async def test_pre_existing_interactive_recommended_entry_is_pruned(
        self, adapter: AsyncMock, tmp_path: Path
    ) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[10], max_queue_size=10)
        adapter.list_tasks.return_value = [
            Task(id="10", title="x", state=TaskState.RESEARCHED, labels=["agent:interactive-recommended"])
        ]

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await maintain_queue(adapter, cfg, tmp_path)

        assert 10 in result.removed
        assert 10 not in result.current

    async def test_in_progress_entry_is_not_pruned_for_suitability(self, adapter: AsyncMock, tmp_path: Path) -> None:
        """An issue already picked up by an agent must stay queued regardless
        of its current suitability verdict."""
        cfg = SupervisorConfig(enabled=True, task_queue=[10], max_queue_size=10)
        adapter.list_tasks.return_value = [Task(id="10", title="x", state=TaskState.IN_PROGRESS, labels=["type: epic"])]

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await maintain_queue(adapter, cfg, tmp_path)

        assert 10 in result.current
        assert 10 not in result.removed


class TestMaintainQueue:
    async def test_prunes_done_and_discovers_ready(self, adapter: AsyncMock, config: SupervisorConfig) -> None:
        adapter.list_tasks.return_value = _make_tasks(
            {
                10: TaskState.DONE,
                20: TaskState.RESEARCHED,
                30: TaskState.IN_PROGRESS,
                40: TaskState.RESEARCHED,
            }
        )

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock) as mock_save:
            result = await maintain_queue(adapter, config, Path("/tmp/test"))

        assert result.changed is True
        assert 10 in result.removed
        assert 40 in result.added
        assert 20 in result.current
        assert 30 in result.current
        assert 40 in result.current
        assert 10 not in result.current
        mock_save.assert_called_once()

    async def test_no_changes(self, adapter: AsyncMock, config: SupervisorConfig) -> None:
        adapter.list_tasks.return_value = _make_tasks(
            {
                10: TaskState.TRIAGED,
                20: TaskState.IN_PROGRESS,
                30: TaskState.RESEARCHED,
            }
        )

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock) as mock_save:
            result = await maintain_queue(adapter, config, Path("/tmp/test"))

        assert result.changed is False
        assert result.current == (10, 20, 30)
        mock_save.assert_not_called()

    async def test_all_done_then_discover(self, adapter: AsyncMock) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20], max_queue_size=5)
        adapter.list_tasks.return_value = _make_tasks(
            {
                10: TaskState.DONE,
                20: TaskState.DONE,
                30: TaskState.RESEARCHED,
            }
        )

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await maintain_queue(adapter, cfg, Path("/tmp/test"))

        assert result.removed == (10, 20)
        assert result.added == (30,)
        assert result.current == (30,)

    async def test_list_tasks_failure_skips_maintenance(self, adapter: AsyncMock, config: SupervisorConfig) -> None:
        adapter.list_tasks.side_effect = RuntimeError("rate limited")

        result = await maintain_queue(adapter, config, Path("/tmp/test"))

        assert result.changed is False
        assert result.current == (10, 20, 30)

    async def test_adapter_error_skips_maintenance(self, adapter: AsyncMock, config: SupervisorConfig) -> None:
        """AdapterError is what the real adapters raise on rate limit / auth / bad JSON.

        It subclasses Exception directly, not RuntimeError, so a narrowed handler
        would let it escape and abort the poll cycle instead of skipping the pass.
        """
        adapter.list_tasks.side_effect = AdapterError("Failed to fetch issues from test/repo: rate limited")

        result = await maintain_queue(adapter, config, Path("/tmp/test"))

        assert result.changed is False
        assert result.current == (10, 20, 30)

    async def test_empty_queue_discovers_ready(self, adapter: AsyncMock) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[], max_queue_size=10)
        adapter.list_tasks.return_value = _make_tasks(
            {
                5: TaskState.RESEARCHED,
                15: TaskState.TRIAGED,
            }
        )

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await maintain_queue(adapter, cfg, Path("/tmp/test"))

        assert result.added == (5,)
        assert result.current == (5,)

    async def test_max_queue_size_caps_additions(self, adapter: AsyncMock) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[1, 2], max_queue_size=3)
        adapter.list_tasks.return_value = _make_tasks(
            {
                1: TaskState.IN_PROGRESS,
                2: TaskState.IN_PROGRESS,
                10: TaskState.RESEARCHED,
                20: TaskState.RESEARCHED,
                30: TaskState.RESEARCHED,
            }
        )

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await maintain_queue(adapter, cfg, Path("/tmp/test"))

        assert len(result.current) == 3
        assert len(result.added) == 1

    async def test_updates_config_in_memory(self, adapter: AsyncMock) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[10], max_queue_size=10)
        adapter.list_tasks.return_value = _make_tasks(
            {
                10: TaskState.DONE,
                20: TaskState.RESEARCHED,
            }
        )

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            await maintain_queue(adapter, cfg, Path("/tmp/test"))

        assert cfg.task_queue == [20]

    async def test_deleted_issues_pruned(self, adapter: AsyncMock) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20, 30], max_queue_size=10)
        adapter.list_tasks.return_value = _make_tasks(
            {
                20: TaskState.RESEARCHED,
            }
        )

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await maintain_queue(adapter, cfg, Path("/tmp/test"))

        assert 10 not in result.current
        assert 30 not in result.current
        assert result.removed == (10, 30)

    async def test_empty_list_guard_prevents_data_loss(self, adapter: AsyncMock) -> None:
        """When adapter returns [] but queue is non-empty, skip maintenance to prevent data loss."""
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20, 30], max_queue_size=10)
        adapter.list_tasks.return_value = []

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock) as mock_save:
            result = await maintain_queue(adapter, cfg, Path("/tmp/test"))

        assert result.changed is False
        assert result.current == (10, 20, 30)
        assert result.removed == ()
        assert result.added == ()
        mock_save.assert_not_called()

    async def test_empty_list_with_empty_queue_proceeds(self, adapter: AsyncMock) -> None:
        """When both adapter and queue are empty, guard should not trigger."""
        cfg = SupervisorConfig(enabled=True, task_queue=[], max_queue_size=10)
        adapter.list_tasks.return_value = []

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock) as mock_save:
            result = await maintain_queue(adapter, cfg, Path("/tmp/test"))

        assert result.changed is False
        assert result.current == ()
        mock_save.assert_not_called()


class TestApplyPlannerQueueChanges:
    async def test_removals(self) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20, 30])

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await apply_planner_queue_changes(cfg, Path("/tmp"), removals=[20])

        assert result == [10, 30]
        assert cfg.task_queue == [10, 30]

    async def test_reorder_full(self) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20, 30])

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await apply_planner_queue_changes(cfg, Path("/tmp"), reorder=[30, 10, 20])

        assert result == [30, 10, 20]

    async def test_reorder_partial(self) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20, 30, 40])

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await apply_planner_queue_changes(cfg, Path("/tmp"), reorder=[30, 10])

        assert result == [30, 10, 20, 40]

    async def test_removals_and_reorder(self) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20, 30, 40])

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await apply_planner_queue_changes(cfg, Path("/tmp"), removals=[20], reorder=[40, 10, 30])

        assert result == [40, 10, 30]

    async def test_no_changes(self) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20])

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock) as mock_save:
            result = await apply_planner_queue_changes(cfg, Path("/tmp"))

        assert result == [10, 20]
        mock_save.assert_not_called()

    async def test_removal_of_nonexistent_issue(self) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20])

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock) as mock_save:
            result = await apply_planner_queue_changes(cfg, Path("/tmp"), removals=[99])

        assert result == [10, 20]
        mock_save.assert_not_called()

    async def test_reorder_with_extra_issues_ignored(self) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20])

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock) as mock_save:
            result = await apply_planner_queue_changes(cfg, Path("/tmp"), reorder=[99, 10, 20])

        assert result == [10, 20]
        mock_save.assert_not_called()

    async def test_removal_plus_reorder_normalizes(self) -> None:
        """Reorder list is normalized by removing planned removals before validation."""
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20, 30, 40])

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await apply_planner_queue_changes(cfg, Path("/tmp"), removals=[20], reorder=[40, 20, 30, 10])

        assert result == [40, 30, 10]

    async def test_reorder_with_duplicates_deduplicates(self) -> None:
        """Duplicate values in reorder list are deduplicated (first occurrence wins)."""
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20, 30])

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await apply_planner_queue_changes(cfg, Path("/tmp"), reorder=[30, 10, 30, 20])

        assert result == [30, 10, 20]

    async def test_persist_failure_reverts_config(self) -> None:
        """When persistence fails, in-memory config stays unchanged."""
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20, 30])
        fail = AsyncMock(side_effect=Exception("persist failed"))

        with patch("sova.config.db_loader.save_task_queue", fail):
            result = await apply_planner_queue_changes(cfg, Path("/tmp"), removals=[20])

        assert result == [10, 20, 30]
        assert cfg.task_queue == [10, 20, 30]


class TestMaintainQueuePersistenceFailure:
    async def test_persist_failure_reverts_changes(self, adapter: AsyncMock) -> None:
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20], max_queue_size=10)
        adapter.list_tasks.return_value = _make_tasks(
            {
                10: TaskState.DONE,
                20: TaskState.RESEARCHED,
                30: TaskState.RESEARCHED,
            }
        )
        fail = AsyncMock(side_effect=Exception("persist failed"))

        with patch("sova.config.db_loader.save_task_queue", fail):
            result = await maintain_queue(adapter, cfg, Path("/tmp/test"))

        assert result.changed is False
        assert result.current == (10, 20)
        assert cfg.task_queue == [10, 20]


class TestMaintainQueueEmptyListGuard:
    """Test the empty-list defense guard that prevents queue pruning on API failures."""

    async def test_empty_list_with_non_empty_queue_returns_unchanged(self, adapter: AsyncMock) -> None:
        """When API returns [] but queue is non-empty, return queue unchanged."""
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20, 30], max_queue_size=10)
        adapter.list_tasks.return_value = []

        result = await maintain_queue(adapter, cfg, Path("/tmp/test"))

        assert result.changed is False
        assert result.current == (10, 20, 30)
        assert cfg.task_queue == [10, 20, 30]

    async def test_empty_list_with_empty_queue_returns_empty(self, adapter: AsyncMock) -> None:
        """When API returns [] and queue is already empty, no guard triggers."""
        cfg = SupervisorConfig(enabled=True, task_queue=[], max_queue_size=10)
        adapter.list_tasks.return_value = []

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock) as mock_save:
            result = await maintain_queue(adapter, cfg, Path("/tmp/test"))

        assert result.changed is False
        assert result.current == ()
        mock_save.assert_not_called()

    async def test_genuinely_empty_tracker_after_pruning_clears_queue(self, adapter: AsyncMock) -> None:
        """When all queued issues are DONE and tracker has no other issues, queue clears."""
        cfg = SupervisorConfig(enabled=True, task_queue=[10, 20], max_queue_size=10)
        adapter.list_tasks.return_value = _make_tasks({10: TaskState.DONE, 20: TaskState.DONE})

        with patch("sova.config.db_loader.save_task_queue", new_callable=AsyncMock):
            result = await maintain_queue(adapter, cfg, Path("/tmp/test"))

        assert result.changed is True
        assert result.current == ()
        assert cfg.task_queue == []


class TestSaveQueueToDB:
    """Test DB-based queue persistence via save_setting (low-level)."""

    @pytest.mark.asyncio
    async def test_saves_queue_to_db(self) -> None:
        """Queue is persisted to ProjectSetting table via supervisor.task_queue key."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from sova.config.db_loader import get_setting, save_setting
        from sova.db.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with factory() as session:
            await save_setting(session, "supervisor.task_queue", [10, 20, 30])
            await session.commit()

        async with factory() as session:
            queue = await get_setting(session, "supervisor.task_queue")
            assert queue == [10, 20, 30]

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_updates_existing_queue(self) -> None:
        """Subsequent saves update the existing queue value."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from sova.config.db_loader import get_setting, save_setting
        from sova.db.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with factory() as session:
            await save_setting(session, "supervisor.task_queue", [1, 2])
            await session.commit()

        async with factory() as session:
            await save_setting(session, "supervisor.task_queue", [10, 20, 30])
            await session.commit()

        async with factory() as session:
            queue = await get_setting(session, "supervisor.task_queue")
            assert queue == [10, 20, 30]

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_empty_queue_saves_empty_list(self) -> None:
        """An empty queue can be saved to DB."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from sova.config.db_loader import get_setting, save_setting
        from sova.db.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with factory() as session:
            await save_setting(session, "supervisor.task_queue", [])
            await session.commit()

        async with factory() as session:
            queue = await get_setting(session, "supervisor.task_queue")
            assert queue == []

        await engine.dispose()
