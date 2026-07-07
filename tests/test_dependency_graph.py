"""Tests for sova.supervisor.dependency_graph -- DAG engine and API endpoints."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.adapters.base import Task, TaskState
from sova.db.session import close_db, init_db
from sova.supervisor.dependency_graph import (
    DependencyGraph,
    build_dependency_graph,
    parse_dependencies,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    issue_id: int,
    title: str = "",
    body: str = "",
    state: TaskState = TaskState.BACKLOG,
    milestone: str = "",
) -> Task:
    return Task(
        id=str(issue_id),
        title=title or f"Issue #{issue_id}",
        body=body,
        state=state,
        milestone=milestone,
    )


# ---------------------------------------------------------------------------
# parse_dependencies
# ---------------------------------------------------------------------------


class TestParseDependencies:
    def test_no_body(self) -> None:
        assert parse_dependencies("") == set()

    def test_no_dependencies_section(self) -> None:
        body = "## Summary\nSome text\n## Tasks\n- Do stuff"
        assert parse_dependencies(body) == set()

    def test_empty_dependencies_section(self) -> None:
        body = "## Dependencies\n\n## Next Section\nStuff"
        assert parse_dependencies(body) == set()

    def test_single_dep(self) -> None:
        body = "## Dependencies\n- #100\n"
        assert parse_dependencies(body) == {100}

    def test_multiple_deps(self) -> None:
        body = "## Dependencies\n- #100\n- #101\n- #102\n"
        assert parse_dependencies(body) == {100, 101, 102}

    def test_inline_refs(self) -> None:
        body = "## Dependencies\n- Depends on #100 and #101\n"
        assert parse_dependencies(body) == {100, 101}

    def test_stops_at_next_heading(self) -> None:
        body = "## Dependencies\n- #100\n## Other\n- #200\n"
        assert parse_dependencies(body) == {100}

    def test_self_reference_excluded(self) -> None:
        body = "## Dependencies\n- #50\n- #100\n"
        assert parse_dependencies(body, exclude_self=50) == {100}

    def test_malformed_refs_ignored(self) -> None:
        body = "## Dependencies\n- depends on issue 100\n- see number 200\n"
        assert parse_dependencies(body) == set()

    def test_case_insensitive_heading(self) -> None:
        body = "## dependencies\n- #42\n"
        assert parse_dependencies(body) == {42}

    def test_multiple_sections_uses_first(self) -> None:
        body = "## Dependencies\n- #10\n## Other\ntext\n## Dependencies\n- #20\n"
        assert parse_dependencies(body) == {10}

    def test_mixed_valid_and_invalid(self) -> None:
        body = "## Dependencies\n- #100 (blocker)\n- see issue 200\n- #300\n"
        assert parse_dependencies(body) == {100, 300}


# ---------------------------------------------------------------------------
# DependencyGraph
# ---------------------------------------------------------------------------


class TestDependencyGraph:
    def test_empty_graph(self) -> None:
        graph = DependencyGraph([])
        assert graph.nodes == []
        assert graph.edges == []
        assert graph.validate().valid is True
        assert graph.get_ready_tasks() == []
        assert graph.get_parallel_groups() == []

    def test_single_task_no_deps(self) -> None:
        graph = DependencyGraph([_task(1)])
        assert graph.nodes == [1]
        assert graph.edges == []
        assert graph.get_ready_tasks() == [1]
        assert graph.validate().valid is True

    def test_linear_chain(self) -> None:
        # 3 depends on 2, 2 depends on 1
        tasks = [
            _task(1, body=""),
            _task(2, body="## Dependencies\n- #1\n"),
            _task(3, body="## Dependencies\n- #2\n"),
        ]
        graph = DependencyGraph(tasks)
        assert graph.edges == [(2, 1), (3, 2)]
        assert graph.get_dependencies(3) == {2}
        assert graph.get_dependencies(2) == {1}
        assert graph.get_dependents(1) == {2}
        assert graph.get_dependents(2) == {3}

    def test_ready_tasks_all_deps_open(self) -> None:
        tasks = [
            _task(1, state=TaskState.BACKLOG),
            _task(2, body="## Dependencies\n- #1\n", state=TaskState.BACKLOG),
        ]
        graph = DependencyGraph(tasks)
        # Only task 1 is ready (no deps)
        assert graph.get_ready_tasks() == [1]

    def test_ready_tasks_dep_done(self) -> None:
        tasks = [
            _task(1, state=TaskState.DONE),
            _task(2, body="## Dependencies\n- #1\n", state=TaskState.BACKLOG),
        ]
        graph = DependencyGraph(tasks)
        # Task 1 is done (excluded), task 2 is ready
        assert graph.get_ready_tasks() == [2]

    def test_ready_tasks_missing_dep_blocks(self) -> None:
        # Task 2 depends on #999 which is not in the graph
        tasks = [
            _task(2, body="## Dependencies\n- #999\n", state=TaskState.BACKLOG),
        ]
        graph = DependencyGraph(tasks)
        # Missing dep blocks readiness (fail-closed)
        assert graph.get_ready_tasks() == []

    def test_done_tasks_excluded_from_ready(self) -> None:
        tasks = [_task(1, state=TaskState.DONE)]
        graph = DependencyGraph(tasks)
        assert graph.get_ready_tasks() == []

    def test_cycle_detection(self) -> None:
        tasks = [
            _task(1, body="## Dependencies\n- #2\n"),
            _task(2, body="## Dependencies\n- #1\n"),
        ]
        graph = DependencyGraph(tasks)
        result = graph.validate()
        assert result.valid is False
        assert sorted(result.cycle_members) == [1, 2]

    def test_three_node_cycle(self) -> None:
        tasks = [
            _task(1, body="## Dependencies\n- #3\n"),
            _task(2, body="## Dependencies\n- #1\n"),
            _task(3, body="## Dependencies\n- #2\n"),
        ]
        graph = DependencyGraph(tasks)
        result = graph.validate()
        assert result.valid is False
        assert sorted(result.cycle_members) == [1, 2, 3]

    def test_missing_refs_detected(self) -> None:
        tasks = [
            _task(1, body="## Dependencies\n- #999\n"),
        ]
        graph = DependencyGraph(tasks)
        result = graph.validate()
        assert result.valid is False
        assert result.missing_refs == [999]

    def test_chain_linear(self) -> None:
        tasks = [
            _task(1),
            _task(2, body="## Dependencies\n- #1\n"),
            _task(3, body="## Dependencies\n- #2\n"),
        ]
        graph = DependencyGraph(tasks)
        chain = graph.get_chain(3)
        assert chain == [1, 2, 3]

    def test_chain_diamond(self) -> None:
        # 4 depends on 2 and 3; both 2 and 3 depend on 1
        tasks = [
            _task(1),
            _task(2, body="## Dependencies\n- #1\n"),
            _task(3, body="## Dependencies\n- #1\n"),
            _task(4, body="## Dependencies\n- #2\n- #3\n"),
        ]
        graph = DependencyGraph(tasks)
        chain = graph.get_chain(4)
        # 1 must come before 2 and 3, all before 4
        assert chain[0] == 1
        assert chain[-1] == 4
        assert set(chain) == {1, 2, 3, 4}

    def test_chain_single_node(self) -> None:
        graph = DependencyGraph([_task(5)])
        assert graph.get_chain(5) == [5]

    def test_parallel_groups_linear(self) -> None:
        tasks = [
            _task(1),
            _task(2, body="## Dependencies\n- #1\n"),
            _task(3, body="## Dependencies\n- #2\n"),
        ]
        graph = DependencyGraph(tasks)
        groups = graph.get_parallel_groups()
        assert len(groups) == 3
        assert groups[0].tier == 0
        assert groups[0].task_ids == [1]
        assert groups[1].tier == 1
        assert groups[1].task_ids == [2]
        assert groups[2].tier == 2
        assert groups[2].task_ids == [3]

    def test_parallel_groups_wide(self) -> None:
        # 3 independent tasks
        tasks = [_task(1), _task(2), _task(3)]
        graph = DependencyGraph(tasks)
        groups = graph.get_parallel_groups()
        assert len(groups) == 1
        assert groups[0].tier == 0
        assert groups[0].task_ids == [1, 2, 3]

    def test_parallel_groups_diamond(self) -> None:
        tasks = [
            _task(1),
            _task(2, body="## Dependencies\n- #1\n"),
            _task(3, body="## Dependencies\n- #1\n"),
            _task(4, body="## Dependencies\n- #2\n- #3\n"),
        ]
        graph = DependencyGraph(tasks)
        groups = graph.get_parallel_groups()
        assert len(groups) == 3
        assert groups[0].task_ids == [1]
        assert sorted(groups[1].task_ids) == [2, 3]
        assert groups[2].task_ids == [4]

    def test_to_dict_structure(self) -> None:
        tasks = [
            _task(1, title="Base", state=TaskState.DONE),
            _task(2, title="Feature", body="## Dependencies\n- #1\n"),
        ]
        graph = DependencyGraph(tasks)
        d = graph.to_dict()
        assert "nodes" in d
        assert "edges" in d
        assert "ready" in d
        assert "parallel_groups" in d
        assert "validation" in d
        assert len(d["nodes"]) == 2
        assert d["edges"] == [{"from": 2, "to": 1}]
        assert d["ready"] == [2]
        assert d["validation"]["valid"] is True

    def test_self_reference_filtered(self) -> None:
        tasks = [_task(5, body="## Dependencies\n- #5\n- #10\n")]
        graph = DependencyGraph(tasks)
        assert graph.get_dependencies(5) == {10}


# ---------------------------------------------------------------------------
# build_dependency_graph (async)
# ---------------------------------------------------------------------------


class TestBuildDependencyGraph:
    @pytest.mark.asyncio
    async def test_fetches_missing_deps(self) -> None:
        adapter = AsyncMock()
        adapter.list_tasks.return_value = [
            _task(10, body="## Dependencies\n- #5\n"),
        ]
        adapter.get_task.return_value = _task(5, state=TaskState.DONE)

        graph = await build_dependency_graph(adapter)

        adapter.get_task.assert_called_once_with("5")
        assert 5 in graph._tasks
        assert graph.get_ready_tasks() == [10]

    @pytest.mark.asyncio
    async def test_missing_dep_fetch_failure(self) -> None:
        adapter = AsyncMock()
        adapter.list_tasks.return_value = [
            _task(10, body="## Dependencies\n- #999\n"),
        ]
        adapter.get_task.side_effect = Exception("Not found")

        graph = await build_dependency_graph(adapter)

        # Missing dep still shows in validation
        result = graph.validate()
        assert 999 in result.missing_refs

    @pytest.mark.asyncio
    async def test_milestone_filter_passed(self) -> None:
        adapter = AsyncMock()
        adapter.list_tasks.return_value = []

        await build_dependency_graph(adapter, milestone="Phase 7")

        call_args = adapter.list_tasks.call_args
        filters = call_args[0][0] if call_args[0] else call_args[1].get("filters")
        assert filters is not None
        assert filters.milestone == "Phase 7"


# ---------------------------------------------------------------------------
# Dashboard API endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _mock_graph_context(tasks: list[Task]):
    """Return context managers that patch config+adapter+build for API tests."""
    from unittest.mock import MagicMock

    mock_cfg = MagicMock()

    async def _build(adapter, *, milestone=""):
        return DependencyGraph(tasks)

    return (
        patch("sova.config.loader.load_config", return_value=mock_cfg),
        patch("sova.adapters.create_adapter", return_value=AsyncMock()),
        patch("sova.supervisor.dependency_graph.build_dependency_graph", side_effect=_build),
    )


@pytest.fixture
async def client(setup_db):
    from sova.dashboard.app import create_app

    app = create_app(multi_project=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestDependencyAPI:
    @pytest.mark.asyncio
    async def test_graph_endpoint(self, client: AsyncClient) -> None:
        tasks = [
            _task(1, title="Base", state=TaskState.DONE),
            _task(2, title="Feature", body="## Dependencies\n- #1\n"),
        ]
        p1, p2, p3 = _mock_graph_context(tasks)
        with p1, p2, p3:
            resp = await client.get("/api/dependencies/graph")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert "validation" in data

    @pytest.mark.asyncio
    async def test_ready_endpoint(self, client: AsyncClient) -> None:
        tasks = [
            _task(1, title="Ready Task", state=TaskState.BACKLOG),
        ]
        p1, p2, p3 = _mock_graph_context(tasks)
        with p1, p2, p3:
            resp = await client.get("/api/dependencies/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert "ready" in data
        assert len(data["ready"]) == 1
        assert data["ready"][0]["id"] == 1

    @pytest.mark.asyncio
    async def test_chain_endpoint(self, client: AsyncClient) -> None:
        tasks = [
            _task(1, title="Base"),
            _task(2, title="Middle", body="## Dependencies\n- #1\n"),
            _task(3, title="Top", body="## Dependencies\n- #2\n"),
        ]
        p1, p2, p3 = _mock_graph_context(tasks)
        with p1, p2, p3:
            resp = await client.get("/api/dependencies/chain/3")
        assert resp.status_code == 200
        data = resp.json()
        assert data["issue"] == 3
        assert len(data["chain"]) == 3

    @pytest.mark.asyncio
    async def test_chain_unknown_issue(self, client: AsyncClient) -> None:
        p1, p2, p3 = _mock_graph_context([])
        with p1, p2, p3:
            resp = await client.get("/api/dependencies/chain/999")
        assert resp.status_code == 404
