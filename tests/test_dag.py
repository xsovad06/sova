"""Tests for sova.core.dag -- DAG executor and validation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sova.core.dag import DAGExecutor, _evaluate_condition, _topological_sort, validate_dag

# -- Validation tests ----------------------------------------------------------


class TestValidateDAG:
    def test_empty_graph(self):
        errors = validate_dag({"nodes": [], "edges": []})
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
        errors = validate_dag(graph)
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
        errors = validate_dag(graph)
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
        errors = validate_dag(graph)
        assert any("no command" in e.lower() for e in errors)

    def test_unknown_source_node(self):
        graph = {
            "nodes": [{"id": "a", "command": "develop"}],
            "edges": [{"id": "e1", "source": "nonexistent", "target": "a"}],
        }
        errors = validate_dag(graph)
        assert any("unknown source" in e.lower() for e in errors)

    def test_unknown_target_node(self):
        graph = {
            "nodes": [{"id": "a", "command": "develop"}],
            "edges": [{"id": "e1", "source": "a", "target": "nonexistent"}],
        }
        errors = validate_dag(graph)
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
        errors = validate_dag(graph)
        assert any("unreachable" in e.lower() for e in errors)

    def test_single_node_valid(self):
        graph = {
            "nodes": [{"id": "a", "command": "develop"}],
            "edges": [],
        }
        errors = validate_dag(graph)
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
        errors = validate_dag(graph)
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
        return DAGExecutor(definition, ctx)

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
