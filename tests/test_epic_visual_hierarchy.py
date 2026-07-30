"""Tests for epic visual hierarchy (issue #558)."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.supervisor.dependency_graph import DependencyGraph, is_epic


def _task(
    issue_id: int,
    title: str = "",
    body: str = "",
    state: TaskState = TaskState.BACKLOG,
    labels: list[str] | None = None,
) -> Task:
    return Task(
        id=str(issue_id),
        title=title or f"Issue #{issue_id}",
        body=body,
        state=state,
        labels=labels or [],
    )


class TestEpicDetection:
    """Test is_epic() helper function."""

    def test_epic_label_exact_match(self) -> None:
        """Detects 'type: epic' label."""
        labels = ["priority: high", "type: epic", "area: dashboard"]
        assert is_epic(labels) is True

    def test_epic_label_case_insensitive(self) -> None:
        """Detects 'type: epic' case-insensitively."""
        assert is_epic(["TYPE: EPIC"]) is True
        assert is_epic(["Type: Epic"]) is True

    def test_not_epic_no_type_label(self) -> None:
        """Returns False when no type: label exists."""
        labels = ["priority: high", "area: dashboard"]
        assert is_epic(labels) is False

    def test_not_epic_different_type(self) -> None:
        """Returns False for non-epic type labels."""
        assert is_epic(["type: feature"]) is False
        assert is_epic(["type: bug"]) is False
        assert is_epic(["type: task"]) is False

    def test_empty_labels(self) -> None:
        """Returns False for empty label list."""
        assert is_epic([]) is False


class TestEpicExclusionFromReady:
    """Test that epics are excluded from ready tasks."""

    def test_epic_excluded_from_ready(self) -> None:
        """Epic issues are excluded from get_ready_tasks() even when dependencies are met."""
        tasks = [
            _task(1, state=TaskState.DONE, body=""),
            _task(
                2,
                state=TaskState.TRIAGED,
                labels=["type: epic"],
                body="## Dependencies\n- #1\n",
            ),
        ]
        graph = DependencyGraph(tasks)
        ready = graph.get_ready_tasks()
        assert 2 not in ready, "Epic should be excluded from ready tasks"

    def test_epic_with_no_deps_excluded(self) -> None:
        """Epic with no dependencies is still excluded from ready."""
        tasks = [
            _task(1, state=TaskState.TRIAGED, labels=["type: epic"], body=""),
        ]
        graph = DependencyGraph(tasks)
        ready = graph.get_ready_tasks()
        assert 1 not in ready, "Epic with no deps should be excluded"

    def test_regular_task_with_met_deps_included(self) -> None:
        """Regular (non-epic) tasks are included when deps are met."""
        tasks = [
            _task(1, state=TaskState.DONE, body=""),
            _task(2, state=TaskState.TRIAGED, labels=[], body="## Dependencies\n- #1\n"),
        ]
        graph = DependencyGraph(tasks)
        ready = graph.get_ready_tasks()
        assert 2 in ready, "Non-epic task with met deps should be ready"


class TestEpicNodeSerialization:
    """Test that to_dict() includes is_epic field for graph API."""

    def test_epic_node_serialized_with_flag(self) -> None:
        """Epic nodes have is_epic=True in serialized output."""
        tasks = [_task(1, labels=["type: epic"])]
        graph = DependencyGraph(tasks)
        data = graph.to_dict()
        node = data["nodes"][0]
        assert node["is_epic"] is True

    def test_non_epic_node_serialized_without_flag(self) -> None:
        """Non-epic nodes have is_epic=False."""
        tasks = [_task(1, labels=["type: feature"])]
        graph = DependencyGraph(tasks)
        data = graph.to_dict()
        node = data["nodes"][0]
        assert node["is_epic"] is False

    def test_epic_preserves_other_fields(self) -> None:
        """Epic nodes still include all standard fields."""
        tasks = [
            _task(
                1,
                title="My Epic",
                state=TaskState.TRIAGED,
                labels=["type: epic", "priority: high"],
                body="Epic body\n## Dependencies\n- #2\n",
            )
        ]
        graph = DependencyGraph(tasks)
        data = graph.to_dict()
        node = data["nodes"][0]
        assert node["is_epic"] is True
        assert node["title"] == "My Epic"
        assert node["priority"] == "high"
        assert node["dependencies"] == [2]


class TestEpicAutoClose:
    """Test that epics are auto-closed when all children are done."""

    @pytest.mark.asyncio
    async def test_epic_closed_when_all_children_done(self) -> None:
        """Epic is closed when all dependent issues are DONE."""
        from sova.supervisor.progression import TaskProgressionEngine

        # Epic #1 with children #2 and #3
        tasks = [
            _task(1, labels=["type: epic"], state=TaskState.TRIAGED, body=""),
            _task(2, state=TaskState.DONE, body="## Dependencies\n- #1\n"),
            _task(3, state=TaskState.DONE, body="## Dependencies\n- #1\n"),
        ]

        mock_adapter = AsyncMock()
        mock_adapter.list_tasks = AsyncMock(return_value=tasks)
        mock_adapter.get_task = AsyncMock(
            side_effect=lambda issue_id: next((t for t in tasks if t.id == issue_id), None)
        )
        mock_adapter.transition_state = AsyncMock()

        engine = TaskProgressionEngine(
            config=Mock(),
            adapter=mock_adapter,
            project_dir=Mock(),
            session_factory=Mock(),
        )

        with patch("sova.supervisor.progression.build_dependency_graph") as mock_build:
            mock_build.return_value = DependencyGraph(tasks)
            results = await engine.auto_close_epics()

        assert len(results) == 1
        assert results[0]["issue"] == 1
        assert results[0]["closed"] is True
        mock_adapter.transition_state.assert_called_once_with("1", TaskState.DONE)

    @pytest.mark.asyncio
    async def test_epic_not_closed_when_child_open(self) -> None:
        """Epic remains open when any child is not DONE."""
        from sova.supervisor.progression import TaskProgressionEngine

        tasks = [
            _task(1, labels=["type: epic"], state=TaskState.TRIAGED, body=""),
            _task(2, state=TaskState.DONE, body="## Dependencies\n- #1\n"),
            _task(3, state=TaskState.IN_PROGRESS, body="## Dependencies\n- #1\n"),
        ]

        mock_adapter = AsyncMock()
        mock_adapter.transition_state = AsyncMock()

        engine = TaskProgressionEngine(
            config=Mock(),
            adapter=mock_adapter,
            project_dir=Mock(),
            session_factory=Mock(),
        )

        with patch("sova.supervisor.progression.build_dependency_graph") as mock_build:
            mock_build.return_value = DependencyGraph(tasks)
            results = await engine.auto_close_epics()

        assert len(results) == 0
        mock_adapter.transition_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_epic_without_children_not_closed(self) -> None:
        """Epic with no children is never auto-closed."""
        from sova.supervisor.progression import TaskProgressionEngine

        tasks = [_task(1, labels=["type: epic"], state=TaskState.TRIAGED, body="")]

        mock_adapter = AsyncMock()
        mock_adapter.transition_state = AsyncMock()

        engine = TaskProgressionEngine(
            config=Mock(),
            adapter=mock_adapter,
            project_dir=Mock(),
            session_factory=Mock(),
        )

        with patch("sova.supervisor.progression.build_dependency_graph") as mock_build:
            mock_build.return_value = DependencyGraph(tasks)
            results = await engine.auto_close_epics()

        assert len(results) == 0
        mock_adapter.transition_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_closed_epic_skipped(self) -> None:
        """Epic that is already DONE is not re-closed."""
        from sova.supervisor.progression import TaskProgressionEngine

        tasks = [
            _task(1, labels=["type: epic"], state=TaskState.DONE, body=""),
            _task(2, state=TaskState.DONE, body="## Dependencies\n- #1\n"),
        ]

        mock_adapter = AsyncMock()
        mock_adapter.transition_state = AsyncMock()

        engine = TaskProgressionEngine(
            config=Mock(),
            adapter=mock_adapter,
            project_dir=Mock(),
            session_factory=Mock(),
        )

        with patch("sova.supervisor.progression.build_dependency_graph") as mock_build:
            mock_build.return_value = DependencyGraph(tasks)
            results = await engine.auto_close_epics()

        assert len(results) == 0
        mock_adapter.transition_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_epic_closed_with_unmilestoned_children(self) -> None:
        """Epic is closed when all unmilestoned children are DONE.

        This tests the fix for issue #558 where unmilestoned DONE children
        were being filtered out of the dependency graph, causing epics to
        never auto-close.
        """
        from sova.supervisor.progression import TaskProgressionEngine

        # Epic #1 with unmilestoned DONE children #2 and #3
        tasks = [
            _task(1, labels=["type: epic"], state=TaskState.TRIAGED, body=""),
            _task(2, state=TaskState.DONE, body="## Dependencies\n- #1\n"),
            _task(3, state=TaskState.DONE, body="## Dependencies\n- #1\n"),
        ]

        mock_adapter = AsyncMock()
        mock_adapter.list_tasks = AsyncMock(return_value=tasks)
        mock_adapter.get_task = AsyncMock(
            side_effect=lambda issue_id: next((t for t in tasks if t.id == issue_id), None)
        )
        mock_adapter.transition_state = AsyncMock()

        engine = TaskProgressionEngine(
            config=Mock(),
            adapter=mock_adapter,
            project_dir=Mock(),
            session_factory=Mock(),
        )

        # Don't mock build_dependency_graph to test the actual filtering logic
        results = await engine.auto_close_epics()

        assert len(results) == 1
        assert results[0]["issue"] == 1
        assert results[0]["closed"] is True
        mock_adapter.transition_state.assert_called_once_with("1", TaskState.DONE)

    @pytest.mark.asyncio
    async def test_epic_close_transition_failure(self) -> None:
        """Epic transition failure is logged but doesn't raise."""
        from sova.supervisor.progression import TaskProgressionEngine

        tasks = [
            _task(1, labels=["type: epic"], state=TaskState.TRIAGED, body=""),
            _task(2, state=TaskState.DONE, body="## Dependencies\n- #1\n"),
        ]

        mock_adapter = AsyncMock()
        mock_adapter.transition_state = AsyncMock(side_effect=Exception("API error"))

        engine = TaskProgressionEngine(
            config=Mock(),
            adapter=mock_adapter,
            project_dir=Mock(),
            session_factory=Mock(),
        )

        with patch("sova.supervisor.progression.build_dependency_graph") as mock_build:
            mock_build.return_value = DependencyGraph(tasks)
            results = await engine.auto_close_epics()

        assert len(results) == 1
        assert results[0]["issue"] == 1
        assert results[0]["closed"] is False
        mock_adapter.transition_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_epic_graph_build_failure_returns_empty(self) -> None:
        """Graph build failure fails open with empty result list."""
        from sova.supervisor.progression import TaskProgressionEngine

        mock_adapter = AsyncMock()

        engine = TaskProgressionEngine(
            config=Mock(),
            adapter=mock_adapter,
            project_dir=Mock(),
            session_factory=Mock(),
        )

        with patch("sova.supervisor.progression.build_dependency_graph") as mock_build:
            mock_build.side_effect = Exception("Graph build failed")
            results = await engine.auto_close_epics()

        assert results == []


class TestDependencyGraphFiltering:
    """Test dependency graph filtering logic."""

    @pytest.mark.asyncio
    async def test_unmilestoned_done_tasks_with_deps_preserved(self) -> None:
        """DONE tasks with dependencies are kept even when unmilestoned."""
        from sova.supervisor.dependency_graph import build_dependency_graph

        tasks = [
            _task(1, labels=["type: epic"], state=TaskState.TRIAGED, body=""),
            _task(2, state=TaskState.DONE, body="## Dependencies\n- #1\n"),
            _task(3, state=TaskState.DONE, body=""),  # No deps, should be filtered
        ]

        mock_adapter = AsyncMock()
        mock_adapter.list_tasks = AsyncMock(return_value=tasks)
        mock_adapter.get_task = AsyncMock(
            side_effect=lambda issue_id: next((t for t in tasks if t.id == issue_id), None)
        )

        graph = await build_dependency_graph(mock_adapter)

        # Task 2 should be in graph because it has dependencies
        assert 2 in graph.nodes
        # Task 3 should be filtered out (DONE, no milestone, no dependencies)
        assert 3 not in graph.nodes
