"""Tests for sova.core.dag -- DAG executor and validation."""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from sova.core.dag import DAGExecutor, _evaluate_condition, _topological_sort, validate_dag
from sova.db.session import close_db, init_db

# -- Validation tests ----------------------------------------------------------


class TestValidateDAG:
    def test_empty_graph(self):
        errors, _ = validate_dag({"nodes": [], "edges": []})
        assert len(errors) == 1
        assert "no nodes" in errors[0]

    def test_valid_linear_graph(self):
        graph = {
            "nodes": [
                {"id": "a", "command": "develop", "label": "Develop"},
                {"id": "b", "command": "test", "label": "Test"},
            ],
            "edges": [
                {"id": "e1", "source": "a", "target": "b"},
            ],
        }
        errors, _ = validate_dag(graph)
        assert errors == []

    def test_cycle_detection(self):
        graph = {
            "nodes": [
                {"id": "a", "command": "develop"},
                {"id": "b", "command": "test"},
                {"id": "c", "command": "review"},
            ],
            "edges": [
                {"id": "e1", "source": "a", "target": "b"},
                {"id": "e2", "source": "b", "target": "c"},
                {"id": "e3", "source": "c", "target": "a"},
            ],
        }
        errors, _ = validate_dag(graph)
        assert any("cycle" in e.lower() for e in errors)

    def test_missing_command(self):
        graph = {
            "nodes": [
                {"id": "a", "command": ""},
                {"id": "b", "command": "test"},
            ],
            "edges": [
                {"id": "e1", "source": "a", "target": "b"},
            ],
        }
        errors, _ = validate_dag(graph)
        assert any("no command" in e.lower() for e in errors)

    def test_unknown_source_node(self):
        graph = {
            "nodes": [{"id": "a", "command": "develop"}],
            "edges": [{"id": "e1", "source": "nonexistent", "target": "a"}],
        }
        errors, _ = validate_dag(graph)
        assert any("unknown source" in e.lower() for e in errors)

    def test_unknown_target_node(self):
        graph = {
            "nodes": [{"id": "a", "command": "develop"}],
            "edges": [{"id": "e1", "source": "a", "target": "nonexistent"}],
        }
        errors, _ = validate_dag(graph)
        assert any("unknown target" in e.lower() for e in errors)

    def test_unreachable_nodes(self):
        graph = {
            "nodes": [
                {"id": "a", "command": "develop"},
                {"id": "b", "command": "test"},
                {"id": "c", "command": "review"},
            ],
            "edges": [
                {"id": "e1", "source": "a", "target": "b"},
                # c is not connected
            ],
        }
        errors, _ = validate_dag(graph)
        assert any("unreachable" in e.lower() for e in errors)

    def test_single_node_valid(self):
        graph = {
            "nodes": [{"id": "a", "command": "develop"}],
            "edges": [],
        }
        errors, _ = validate_dag(graph)
        assert errors == []

    def test_diamond_graph_valid(self):
        graph = {
            "nodes": [
                {"id": "a", "command": "develop"},
                {"id": "b", "command": "test"},
                {"id": "c", "command": "lint"},
                {"id": "d", "command": "deploy"},
            ],
            "edges": [
                {"id": "e1", "source": "a", "target": "b"},
                {"id": "e2", "source": "a", "target": "c"},
                {"id": "e3", "source": "b", "target": "d"},
                {"id": "e4", "source": "c", "target": "d"},
            ],
        }
        errors, _ = validate_dag(graph)
        assert errors == []


# -- Topological sort tests ----------------------------------------------------


class TestTopologicalSort:
    def test_linear(self):
        graph = {
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "edges": [
                {"source": "a", "target": "b"},
                {"source": "b", "target": "c"},
            ],
        }
        result = _topological_sort(graph)
        assert result == ["a", "b", "c"]

    def test_cycle_raises(self):
        graph = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b"},
                {"source": "b", "target": "a"},
            ],
        }
        with pytest.raises(ValueError, match="cycle"):
            _topological_sort(graph)

    def test_no_edges(self):
        graph = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [],
        }
        result = _topological_sort(graph)
        assert set(result) == {"a", "b"}


# -- Condition evaluation tests ------------------------------------------------


class TestEvaluateCondition:
    def test_equals_true(self):
        assert _evaluate_condition("x == y", {"x": "y"}) is True

    def test_equals_false(self):
        assert _evaluate_condition("x == y", {"x": "z"}) is False

    def test_not_equals_true(self):
        assert _evaluate_condition("x != y", {"x": "z"}) is True

    def test_not_equals_false(self):
        assert _evaluate_condition("x != y", {"x": "y"}) is False

    def test_missing_key(self):
        assert _evaluate_condition("x == y", {}) is False

    def test_unknown_format(self):
        assert _evaluate_condition("just a string", {}) is False


# -- _should_execute tests (conditional branch rejoin) -------------------------


class TestShouldExecute:
    """Test that merge nodes after conditional branches execute correctly."""

    def _make_executor(self, graph: dict) -> DAGExecutor:
        definition = MagicMock()
        definition.graph_json = graph
        ctx = MagicMock()
        return DAGExecutor(definition, ctx, command_dispatcher=AsyncMock())

    def test_entry_node_always_executes(self):
        graph = {"nodes": [{"id": "a", "command": "x"}], "edges": []}
        exe = self._make_executor(graph)
        assert exe._should_execute("a", {}, set()) is True

    def test_unconditional_source_must_complete(self):
        graph = {
            "nodes": [{"id": "a", "command": "x"}, {"id": "b", "command": "y"}],
            "edges": [{"source": "a", "target": "b"}],
        }
        exe = self._make_executor(graph)
        assert exe._should_execute("b", {}, set()) is False
        assert exe._should_execute("b", {"a.done": "true"}, set()) is True

    def test_skipped_source_does_not_block_merge(self):
        """If A->B (cond), A->C (cond), B->D, C->D: skipping C should not block D."""
        graph = {
            "nodes": [
                {"id": "a", "command": "x"},
                {"id": "b", "command": "y"},
                {"id": "c", "command": "z"},
                {"id": "d", "command": "w"},
            ],
            "edges": [
                {"source": "a", "target": "b", "condition": "x == 1"},
                {"source": "a", "target": "c", "condition": "x != 1"},
                {"source": "b", "target": "d"},
                {"source": "c", "target": "d"},
            ],
        }
        exe = self._make_executor(graph)
        # B completed, C was skipped -- D should execute
        assert exe._should_execute("d", {"b.done": "true"}, {"c"}) is True

    def test_all_sources_skipped_blocks_node(self):
        """A node reachable only through skipped branches should not execute."""
        graph = {
            "nodes": [
                {"id": "b", "command": "y"},
                {"id": "c", "command": "z"},
                {"id": "d", "command": "w"},
            ],
            "edges": [
                {"source": "b", "target": "d"},
                {"source": "c", "target": "d"},
            ],
        }
        exe = self._make_executor(graph)
        assert exe._should_execute("d", {}, {"b", "c"}) is False

    def test_conditional_edge_condition_fails(self):
        """Conditional edge where condition does not pass should block."""
        graph = {
            "nodes": [
                {"id": "a", "command": "x"},
                {"id": "b", "command": "y"},
            ],
            "edges": [
                {"source": "a", "target": "b", "condition": "result == pass"},
            ],
        }
        exe = self._make_executor(graph)
        assert exe._should_execute("b", {"a.done": "true", "result": "fail"}, set()) is False

    def test_conditional_edge_condition_passes(self):
        """Conditional edge where condition passes should allow execution."""
        graph = {
            "nodes": [
                {"id": "a", "command": "x"},
                {"id": "b", "command": "y"},
            ],
            "edges": [
                {"source": "a", "target": "b", "condition": "result == pass"},
            ],
        }
        exe = self._make_executor(graph)
        assert exe._should_execute("b", {"a.done": "true", "result": "pass"}, set()) is True


# -- Constructor validation tests ----------------------------------------------


class TestDAGExecutorConstructor:
    def test_raises_without_dispatcher(self):
        definition = MagicMock()
        definition.graph_json = {"nodes": [], "edges": []}
        ctx = MagicMock()
        with pytest.raises(TypeError):
            DAGExecutor(definition, ctx)

    def test_raises_with_none_dispatcher(self):
        definition = MagicMock()
        definition.graph_json = {"nodes": [], "edges": []}
        ctx = MagicMock()
        with pytest.raises(ValueError, match="cannot be None"):
            DAGExecutor(definition, ctx, command_dispatcher=None)


# -- DAG executor execution tests ---------------------------------------------


@pytest.fixture(autouse=True)
async def _dag_db():
    """Initialize an in-memory DB for executor tests requiring StepExecution."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


class TestDAGExecutorExecution:
    def _make_executor(self, graph: dict, dispatcher: AsyncMock | None = None) -> DAGExecutor:
        definition = MagicMock()
        definition.graph_json = graph
        ctx = MagicMock()
        ctx.task_run_id = None
        return DAGExecutor(definition, ctx, command_dispatcher=dispatcher or AsyncMock())

    async def test_execute_single_node_success(self):
        graph = {
            "nodes": [{"id": "a", "command": "develop", "label": "Develop"}],
            "edges": [],
        }
        mock_cmd = AsyncMock(return_value={"status": "ok", "message": "done", "cost_usd": 0.5})
        exe = self._make_executor(graph, dispatcher=mock_cmd)
        result = await exe.execute()
        assert result.success
        assert len(result.node_results) == 1
        assert result.node_results[0].success
        assert result.total_cost_usd == Decimal("0.5")

    async def test_execute_linear_chain(self):
        graph = {
            "nodes": [
                {"id": "a", "command": "develop"},
                {"id": "b", "command": "test"},
            ],
            "edges": [{"source": "a", "target": "b"}],
        }
        mock_cmd = AsyncMock(return_value={"status": "ok", "message": "done", "cost_usd": 0.1})
        exe = self._make_executor(graph, dispatcher=mock_cmd)
        result = await exe.execute()
        assert result.success
        assert len(result.node_results) == 2
        assert result.total_cost_usd == Decimal("0.2")

    async def test_execute_stops_on_failure(self):
        graph = {
            "nodes": [
                {"id": "a", "command": "develop"},
                {"id": "b", "command": "test"},
            ],
            "edges": [{"source": "a", "target": "b"}],
        }
        mock_cmd = AsyncMock(side_effect=RuntimeError("develop crashed"))
        exe = self._make_executor(graph, dispatcher=mock_cmd)
        result = await exe.execute()
        assert not result.success
        assert len(result.node_results) == 1
        assert "develop crashed" in result.error

    async def test_execute_invalid_dag_returns_error(self):
        graph = {"nodes": [], "edges": []}
        exe = self._make_executor(graph)
        result = await exe.execute()
        assert not result.success
        assert "validation failed" in result.summary.lower()

    async def test_execute_skips_conditional_nodes(self):
        graph = {
            "nodes": [
                {"id": "a", "command": "check"},
                {"id": "b", "command": "fix"},
            ],
            "edges": [{"source": "a", "target": "b", "condition": "error == true"}],
        }
        mock_cmd = AsyncMock(return_value={"status": "ok", "message": "checked"})
        exe = self._make_executor(graph, dispatcher=mock_cmd)
        result = await exe.execute()
        assert result.success
        assert len(result.node_results) == 1
        assert result.node_results[0].node_id == "a"


class TestExecuteNodeDetails:
    def _make_executor(self, *, task_run_id: int | None = None, dispatcher: AsyncMock | None = None) -> DAGExecutor:
        graph = {"nodes": [{"id": "a", "command": "develop"}], "edges": []}
        definition = MagicMock()
        definition.graph_json = graph
        ctx = MagicMock()
        ctx.task_run_id = task_run_id
        return DAGExecutor(definition, ctx, command_dispatcher=dispatcher or AsyncMock())

    async def test_node_with_cost_none(self):
        """cost_usd=None should not crash (the fix for the CodeRabbit finding)."""
        mock_cmd = AsyncMock(return_value={"status": "ok", "message": "done", "cost_usd": None})
        exe = self._make_executor(dispatcher=mock_cmd)
        node = {"id": "a", "command": "develop"}
        nr = await exe._execute_node(node)
        assert nr.success
        assert nr.cost_usd == Decimal("0")

    async def test_node_with_non_numeric_cost(self):
        """Non-numeric cost_usd should fall back to 0."""
        mock_cmd = AsyncMock(return_value={"status": "ok", "message": "done", "cost_usd": "not-a-number"})
        exe = self._make_executor(dispatcher=mock_cmd)
        node = {"id": "a", "command": "develop"}
        nr = await exe._execute_node(node)
        assert nr.success
        assert nr.cost_usd == Decimal("0")

    async def test_node_with_non_dict_result(self):
        """When the command returns a non-dict, success=True and cost=0."""
        mock_cmd = AsyncMock(return_value="plain string result")
        exe = self._make_executor(dispatcher=mock_cmd)
        node = {"id": "a", "command": "develop"}
        nr = await exe._execute_node(node)
        assert nr.success
        assert nr.cost_usd == Decimal("0")

    async def test_node_exception_returns_failure(self):
        mock_cmd = AsyncMock(side_effect=RuntimeError("boom"))
        exe = self._make_executor(dispatcher=mock_cmd)
        node = {"id": "a", "command": "develop"}
        nr = await exe._execute_node(node)
        assert not nr.success
        assert "boom" in nr.error

    async def test_node_records_step_execution(self):
        """When task_run_id is set, StepExecution is persisted."""
        from sova.db.models import StepExecution, TaskRun
        from sova.db.session import get_session

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="1", role="custom", status="running", current_step="test")
                session.add(run)
                await session.flush()
                run_id = run.id

        mock_cmd = AsyncMock(return_value={"status": "ok", "message": "done", "cost_usd": 0.1})
        exe = self._make_executor(task_run_id=run_id, dispatcher=mock_cmd)
        node = {"id": "a", "command": "develop", "label": "Develop"}
        nr = await exe._execute_node(node)

        assert nr.success

        from sqlalchemy import select

        async with await get_session() as session:
            stmt = select(StepExecution).where(StepExecution.task_run_id == run_id)
            result = await session.execute(stmt)
            steps = list(result.scalars().all())
        assert len(steps) == 1
        assert steps[0].step_name == "develop"
        assert steps[0].status == "done"
