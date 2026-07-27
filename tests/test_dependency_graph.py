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
    _get_spec_meta,
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

    def test_multiple_sections_logs_warning(self) -> None:
        body = "## Dependencies\n- #10\n## Other\ntext\n## Dependencies\n- #20\n"
        with patch("sova.supervisor.dependency_graph.log") as mock_log:
            result = parse_dependencies(body)
        assert result == {10}
        mock_log.warning.assert_called_once()
        assert "Multiple" in mock_log.warning.call_args[0][0]


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

    def test_in_progress_excluded_from_ready(self) -> None:
        tasks = [_task(1, state=TaskState.IN_PROGRESS)]
        graph = DependencyGraph(tasks)
        assert graph.get_ready_tasks() == []

    def test_in_review_excluded_from_ready(self) -> None:
        tasks = [_task(1, state=TaskState.IN_REVIEW)]
        graph = DependencyGraph(tasks)
        assert graph.get_ready_tasks() == []

    def test_human_only_excluded_from_ready(self) -> None:
        tasks = [_task(1, state=TaskState.HUMAN_ONLY)]
        graph = DependencyGraph(tasks)
        assert graph.get_ready_tasks() == []

    def test_triaged_included_in_ready(self) -> None:
        tasks = [_task(1, state=TaskState.TRIAGED)]
        graph = DependencyGraph(tasks)
        assert graph.get_ready_tasks() == [1]

    def test_mixed_states_ready_filtering(self) -> None:
        tasks = [
            _task(1, state=TaskState.DONE),
            _task(2, state=TaskState.IN_PROGRESS),
            _task(3, state=TaskState.BACKLOG),
            _task(4, state=TaskState.IN_REVIEW),
            _task(5, state=TaskState.TRIAGED),
        ]
        graph = DependencyGraph(tasks)
        assert graph.get_ready_tasks() == [3, 5]

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

    def test_to_dict_node_has_body_excerpt(self) -> None:
        """Each node should include a short body excerpt for the action drawer."""
        long_body = "x" * 200
        tasks = [_task(1, body=long_body)]
        graph = DependencyGraph(tasks)
        node = graph.to_dict()["nodes"][0]
        assert "body_excerpt" in node
        assert len(node["body_excerpt"]) <= 100

    def test_to_dict_node_body_excerpt_strips_deps_section(self) -> None:
        """body_excerpt should not include the Dependencies header noise."""
        body = "Fix the login page.\n\n## Dependencies\n- #2\n"
        tasks = [_task(1, body=body)]
        node = DependencyGraph(tasks).to_dict()["nodes"][0]
        assert node["body_excerpt"].startswith("Fix")

    def test_to_dict_node_body_excerpt_empty_body(self) -> None:
        tasks = [_task(1, body="")]
        node = DependencyGraph(tasks).to_dict()["nodes"][0]
        assert node["body_excerpt"] == ""

    def test_to_dict_node_has_available_actions(self) -> None:
        """Each node should expose the actions available given its current state."""
        tasks = [
            _task(1, state=TaskState.TRIAGED),
            _task(2, state=TaskState.RESEARCHED),
            _task(3, state=TaskState.IN_REVIEW),
            _task(4, state=TaskState.IN_PROGRESS),
            _task(5, state=TaskState.DONE),
        ]
        d = DependencyGraph(tasks).to_dict()
        by_id = {n["id"]: n["available_actions"] for n in d["nodes"]}
        assert any(a["role"] == "researcher" for a in by_id[1])
        assert any(a["role"] == "developer" for a in by_id[2])
        assert any(a["role"] == "integrate-pr" for a in by_id[3])
        assert by_id[4] == []
        assert by_id[5] == []

    def test_to_dict_no_pr_map_backward_compatible(self) -> None:
        """to_dict without pr_map returns nodes without PR fields."""
        tasks = [_task(1, state=TaskState.IN_REVIEW)]
        node = DependencyGraph(tasks).to_dict()["nodes"][0]
        assert "pr_number" not in node
        assert "pr_state" not in node

    def test_to_dict_pr_enrichment(self) -> None:
        """Nodes with linked PRs include pr_number, pr_url, pr_state, pr_state_label."""
        tasks = [_task(1, state=TaskState.IN_REVIEW), _task(2, state=TaskState.BACKLOG)]
        pr_map = {
            1: {
                "pr_number": 42,
                "pr_url": "https://github.com/test/pull/42",
                "pr_state": "approved_ci_green",
                "pr_state_label": "Ready to Merge",
            },
        }
        d = DependencyGraph(tasks).to_dict(pr_map=pr_map)
        by_id = {n["id"]: n for n in d["nodes"]}
        assert by_id[1]["pr_number"] == 42
        assert by_id[1]["pr_state"] == "approved_ci_green"
        assert by_id[1]["pr_url"] == "https://github.com/test/pull/42"
        # Node without PR should not have PR fields
        assert "pr_number" not in by_id[2]

    def test_to_dict_pr_state_overrides_actions_for_in_review(self) -> None:
        """IN_REVIEW nodes with PR state get PR-aware actions."""
        tasks = [_task(1, state=TaskState.IN_REVIEW)]
        pr_map = {1: {"pr_number": 42, "pr_url": "", "pr_state": "approved_ci_green", "pr_state_label": ""}}
        pr_actions = {
            "approved_ci_green": [{"id": "integrate-pr", "label": "Integrate PR", "role": "integrate-pr"}],
            "changes_requested": [{"id": "address-pr", "label": "Address PR", "role": "address-pr"}],
        }
        d = DependencyGraph(tasks).to_dict(pr_map=pr_map, pr_state_actions=pr_actions)
        node = d["nodes"][0]
        assert len(node["available_actions"]) == 1
        assert node["available_actions"][0]["role"] == "integrate-pr"

    def test_to_dict_pr_state_no_override_for_non_in_review(self) -> None:
        """Nodes not IN_REVIEW keep state-based actions even if PR is linked."""
        tasks = [_task(1, state=TaskState.IN_PROGRESS)]
        pr_map = {1: {"pr_number": 42, "pr_url": "", "pr_state": "ci_running", "pr_state_label": ""}}
        pr_actions = {"ci_running": []}
        d = DependencyGraph(tasks).to_dict(pr_map=pr_map, pr_state_actions=pr_actions)
        node = d["nodes"][0]
        # IN_PROGRESS has no static actions, and PR should not override
        assert node["available_actions"] == []

    def test_self_reference_filtered(self) -> None:
        tasks = [_task(5, body="## Dependencies\n- #5\n- #10\n")]
        graph = DependencyGraph(tasks)
        assert graph.get_dependencies(5) == {10}

    def test_parallel_groups_with_cycle(self) -> None:
        # Cycle between 1 and 2; node 3 depends on 1
        tasks = [
            _task(1, body="## Dependencies\n- #2\n"),
            _task(2, body="## Dependencies\n- #1\n"),
            _task(3),
        ]
        graph = DependencyGraph(tasks)
        groups = graph.get_parallel_groups()
        # Only node 3 (no deps) should appear; cycle nodes are skipped
        assert len(groups) == 1
        assert groups[0].task_ids == [3]

    def test_build_in_degree(self) -> None:
        tasks = [
            _task(1),
            _task(2, body="## Dependencies\n- #1\n"),
        ]
        graph = DependencyGraph(tasks)
        in_deg = graph._build_in_degree({1, 2})
        assert in_deg[1] == 0
        assert in_deg[2] == 1

    def test_collect_transitive_deps(self) -> None:
        tasks = [
            _task(1),
            _task(2, body="## Dependencies\n- #1\n"),
            _task(3, body="## Dependencies\n- #2\n"),
            _task(4),  # unrelated
        ]
        graph = DependencyGraph(tasks)
        visited = graph._collect_transitive_deps(3)
        assert visited == {1, 2, 3}

    def test_topo_sort(self) -> None:
        tasks = [
            _task(1),
            _task(2, body="## Dependencies\n- #1\n"),
        ]
        graph = DependencyGraph(tasks)
        node_ids = {1, 2}
        in_deg = graph._build_in_degree(node_ids)
        order = graph._topo_sort(node_ids, in_deg)
        assert order == [1, 2]

    def test_get_ready_tasks_partial_deps_done(self) -> None:
        # Task 3 depends on 1 (done) and 2 (not done) -- not ready
        tasks = [
            _task(1, state=TaskState.DONE),
            _task(2, state=TaskState.BACKLOG),
            _task(3, body="## Dependencies\n- #1\n- #2\n", state=TaskState.BACKLOG),
        ]
        graph = DependencyGraph(tasks)
        assert graph.get_ready_tasks() == [2]

    def test_has_task(self) -> None:
        graph = DependencyGraph([_task(42)])
        assert graph.has_task(42) is True
        assert graph.has_task(99) is False

    def test_get_task_none(self) -> None:
        graph = DependencyGraph([_task(1)])
        assert graph.get_task(1) is not None
        assert graph.get_task(99) is None

    def test_to_dict_agent_enrichment(self) -> None:
        tasks = [_task(1, state=TaskState.IN_PROGRESS), _task(2, state=TaskState.BACKLOG)]
        agent_map = {1: {"run_id": 42, "role": "developer", "status": "running", "elapsed_seconds": 120}}
        d = DependencyGraph(tasks).to_dict(agent_map=agent_map)
        by_id = {n["id"]: n for n in d["nodes"]}
        assert by_id[1]["agent_running"] is True
        assert by_id[1]["agent_run_id"] == 42
        assert by_id[1]["agent_role"] == "developer"
        assert by_id[1]["agent_elapsed_seconds"] == 120
        assert "agent_running" not in by_id[2]

    def test_to_dict_handoff_enrichment(self) -> None:
        tasks = [_task(1, state=TaskState.IN_REVIEW), _task(2, state=TaskState.BACKLOG)]
        handoff_map = {1: {"next_action": "Address PR"}}
        d = DependencyGraph(tasks).to_dict(handoff_map=handoff_map)
        by_id = {n["id"]: n for n in d["nodes"]}
        assert by_id[1]["handoff_pending"] is True
        assert by_id[1]["handoff_action"] == "Address PR"
        assert "handoff_pending" not in by_id[2]

    def test_to_dict_last_run_enrichment(self) -> None:
        tasks = [_task(1, state=TaskState.BACKLOG), _task(2, state=TaskState.BACKLOG)]
        last_run_map = {1: {"run_id": 99, "status": "failed"}}
        d = DependencyGraph(tasks).to_dict(last_run_map=last_run_map)
        by_id = {n["id"]: n for n in d["nodes"]}
        assert by_id[1]["last_run_status"] == "failed"
        assert by_id[1]["last_run_id"] == 99
        assert "last_run_status" not in by_id[2]

    def test_to_dict_no_enrichment_maps_backward_compatible(self) -> None:
        tasks = [_task(1, state=TaskState.IN_PROGRESS)]
        node = DependencyGraph(tasks).to_dict()["nodes"][0]
        assert "agent_running" not in node
        assert "handoff_pending" not in node
        assert "last_run_status" not in node

    def test_to_dict_all_enrichments_combined(self) -> None:
        tasks = [_task(1, state=TaskState.IN_PROGRESS)]
        pr_map = {1: {"pr_number": 10, "pr_url": "", "pr_state": "ci_running", "pr_state_label": ""}}
        agent_map = {1: {"run_id": 5, "role": "developer", "status": "running", "elapsed_seconds": 60}}
        handoff_map = {1: {"next_action": "Review"}}
        last_run_map = {1: {"run_id": 3, "status": "done"}}
        d = DependencyGraph(tasks).to_dict(
            pr_map=pr_map,
            agent_map=agent_map,
            handoff_map=handoff_map,
            last_run_map=last_run_map,
        )
        node = d["nodes"][0]
        assert node["pr_number"] == 10
        assert node["agent_running"] is True
        assert node["agent_elapsed_seconds"] == 60
        assert node["handoff_pending"] is True
        assert node["last_run_status"] == "done"


# ---------------------------------------------------------------------------
# _get_spec_meta
# ---------------------------------------------------------------------------


class TestGetSpecMeta:
    def test_returns_none_when_no_spec_file(self) -> None:
        """Returns None when read_spec finds no file for the issue."""
        with patch("sova.dashboard.services.spec_service.read_spec", return_value=None):
            result = _get_spec_meta(42)
        assert result is None

    def test_returns_meta_dict_when_spec_exists(self) -> None:
        """Returns a dict with url, status, complexity, open_questions when spec found."""
        spec = {
            "status": "draft",
            "complexity": "medium",
            "open_questions": [{"id": 0, "text": "Q1", "answer": ""}],
        }
        with patch("sova.dashboard.services.spec_service.read_spec", return_value=spec):
            result = _get_spec_meta(42)
        assert result is not None
        assert result["url"] == "/spec/42"
        assert result["status"] == "draft"
        assert result["complexity"] == "medium"
        assert result["open_questions"] == 1

    def test_open_questions_count_zero_when_none(self) -> None:
        """open_questions is 0 when spec has no open questions."""
        spec = {"status": "approved", "complexity": "low", "open_questions": []}
        with patch("sova.dashboard.services.spec_service.read_spec", return_value=spec):
            result = _get_spec_meta(7)
        assert result is not None
        assert result["open_questions"] == 0

    def test_passes_project_dir_to_read_spec(self) -> None:
        """project_dir is forwarded to spec_service.read_spec."""
        from pathlib import Path

        sentinel = Path("/custom/project")
        with patch("sova.dashboard.services.spec_service.read_spec", return_value=None) as mock_read:
            _get_spec_meta(10, project_dir=sentinel)
        mock_read.assert_called_once_with("10", sentinel)

    def test_defaults_to_approved_status(self) -> None:
        """Status defaults to 'draft' when spec dict has no status key."""
        spec = {"complexity": "high", "open_questions": []}
        with patch("sova.dashboard.services.spec_service.read_spec", return_value=spec):
            result = _get_spec_meta(1)
        assert result is not None
        assert result["status"] == "draft"


# ---------------------------------------------------------------------------
# DependencyGraph.to_dict -- RESEARCHED node spec enrichment
# ---------------------------------------------------------------------------


class TestToDict_SpecEnrichment:
    def test_researched_without_spec_keeps_developer_action(self) -> None:
        """RESEARCHED node with no spec file keeps the default 'Run Developer' action."""
        tasks = [_task(1, state=TaskState.RESEARCHED)]
        with patch("sova.dashboard.services.spec_service.read_spec", return_value=None):
            d = DependencyGraph(tasks).to_dict()
        node = d["nodes"][0]
        assert any(a.get("role") == "developer" for a in node["available_actions"])
        assert "spec_meta" not in node

    def test_researched_with_spec_replaces_actions(self) -> None:
        """RESEARCHED node with a spec file gets three spec-aware actions instead of developer."""
        tasks = [_task(1, state=TaskState.RESEARCHED)]
        spec = {"status": "draft", "complexity": "medium", "open_questions": []}
        with patch("sova.dashboard.services.spec_service.read_spec", return_value=spec):
            d = DependencyGraph(tasks).to_dict()
        node = d["nodes"][0]
        action_ids = [a["id"] for a in node["available_actions"]]
        assert "view-spec" in action_ids
        assert "approve-spec" in action_ids
        assert "revise-spec" in action_ids
        # Original developer action must be gone
        assert not any(a.get("role") == "developer" for a in node["available_actions"])

    def test_researched_with_spec_adds_spec_meta_to_node(self) -> None:
        """Node dict includes spec_meta when a spec exists for the issue."""
        tasks = [_task(5, state=TaskState.RESEARCHED)]
        spec = {
            "status": "approved",
            "complexity": "high",
            "open_questions": [{"id": 0, "text": "something?", "answer": ""}],
        }
        with patch("sova.dashboard.services.spec_service.read_spec", return_value=spec):
            d = DependencyGraph(tasks).to_dict()
        node = d["nodes"][0]
        assert "spec_meta" in node
        assert node["spec_meta"]["status"] == "approved"
        assert node["spec_meta"]["complexity"] == "high"
        assert node["spec_meta"]["open_questions"] == 1
        assert node["spec_meta"]["url"] == "/spec/5"

    def test_spec_meta_absent_for_non_researched_states(self) -> None:
        """spec_meta is not added for nodes in states other than RESEARCHED."""
        for state in (TaskState.TRIAGED, TaskState.IN_PROGRESS, TaskState.IN_REVIEW, TaskState.DONE):
            tasks = [_task(1, state=state)]
            spec = {"status": "draft", "complexity": "low", "open_questions": []}
            with patch("sova.dashboard.services.spec_service.read_spec", return_value=spec):
                d = DependencyGraph(tasks).to_dict()
            node = d["nodes"][0]
            assert "spec_meta" not in node, f"spec_meta should be absent for state {state}"

    def test_researched_spec_meta_error_falls_back_gracefully(self) -> None:
        """If _get_spec_meta raises, the node falls back to the default developer action."""
        tasks = [_task(1, state=TaskState.RESEARCHED)]
        with patch(
            "sova.dashboard.services.spec_service.read_spec",
            side_effect=RuntimeError("disk error"),
        ):
            d = DependencyGraph(tasks).to_dict()
        node = d["nodes"][0]
        assert any(a.get("role") == "developer" for a in node["available_actions"])
        assert "spec_meta" not in node

    def test_spec_action_urls_include_issue_id(self) -> None:
        """View-spec link URL and API action URLs contain the correct issue ID."""
        tasks = [_task(99, state=TaskState.RESEARCHED)]
        spec = {"status": "draft", "complexity": "low", "open_questions": []}
        with patch("sova.dashboard.services.spec_service.read_spec", return_value=spec):
            d = DependencyGraph(tasks).to_dict()
        node = d["nodes"][0]
        actions_by_id = {a["id"]: a for a in node["available_actions"]}
        assert actions_by_id["view-spec"]["url"] == "/spec/99"
        assert actions_by_id["view-spec"]["type"] == "link"
        assert actions_by_id["approve-spec"]["url"] == "/spec/99/approve"
        assert actions_by_id["approve-spec"]["type"] == "api"
        assert actions_by_id["revise-spec"]["url"] == "/spec/99/revise"
        assert actions_by_id["revise-spec"]["type"] == "api"


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


def _mock_fetch_context(pr_map=None):
    """Return context managers that patch all fetch functions for graph API tests."""

    async def _agent_map():
        return {}

    async def _last_run_map():
        return {}

    return (
        patch("sova.dashboard.routers.dependencies._fetch_pr_map", return_value=pr_map or {}),
        patch("sova.dashboard.routers.dependencies._fetch_agent_map", side_effect=_agent_map),
        patch("sova.dashboard.routers.dependencies._fetch_handoff_map", return_value={}),
        patch("sova.dashboard.routers.dependencies._fetch_last_run_map", side_effect=_last_run_map),
    )


@pytest.fixture
async def client(setup_db):
    from sova.dashboard.app import create_app

    app = create_app(multi_project=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestBuildGraphConfigError:
    @pytest.mark.asyncio
    async def test_config_load_failure_raises_config_error(self) -> None:
        from sova.dashboard.routers.dependencies import _build_graph, _ConfigError

        with (
            patch("sova.dashboard.project_context.get_project_dir", return_value="/tmp/fake"),
            patch("sova.config.loader.load_config", side_effect=FileNotFoundError("no sova.toml")),
        ):
            with pytest.raises(_ConfigError, match="no sova.toml"):
                await _build_graph()

    @pytest.mark.asyncio
    async def test_adapter_creation_failure_raises_config_error(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.routers.dependencies import _build_graph, _ConfigError

        with (
            patch("sova.dashboard.project_context.get_project_dir", return_value="/tmp/fake"),
            patch("sova.config.loader.load_config", return_value=MagicMock()),
            patch("sova.adapters.create_adapter", side_effect=ValueError("missing repo")),
        ):
            with pytest.raises(_ConfigError, match="missing repo"):
                await _build_graph()


class TestFetchPrMapUnknownState:
    @pytest.mark.asyncio
    async def test_unknown_computed_state_falls_back_to_awaiting_review(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_pr_map

        mock_prs = [
            {"number": 1, "url": "", "computed_state": "totally_unknown", "state_label": "", "linked_issues": [10]},
        ]
        with patch("sova.dashboard.services.pr_service.list_open_prs_with_state", return_value=mock_prs):
            result = await _fetch_pr_map()

        assert result[10]["pr_state"] == "awaiting_review"

    @pytest.mark.asyncio
    async def test_none_linked_issues_treated_as_empty(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_pr_map

        mock_prs = [
            {"number": 1, "url": "", "computed_state": "draft", "state_label": "", "linked_issues": None},
        ]
        with patch("sova.dashboard.services.pr_service.list_open_prs_with_state", return_value=mock_prs):
            result = await _fetch_pr_map()

        assert result == {}


class TestFetchPrMap:
    @pytest.mark.asyncio
    async def test_builds_issue_to_pr_mapping(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_pr_map

        mock_prs = [
            {
                "number": 42,
                "url": "https://github.com/test/pull/42",
                "computed_state": "approved_ci_green",
                "state_label": "Ready to Merge",
                "linked_issues": [10],
            },
            {
                "number": 43,
                "url": "https://github.com/test/pull/43",
                "computed_state": "ci_running",
                "state_label": "CI Running",
                "linked_issues": [20],
            },
        ]
        with patch("sova.dashboard.services.pr_service.list_open_prs_with_state", return_value=mock_prs):
            result = await _fetch_pr_map()

        assert 10 in result
        assert result[10]["pr_number"] == 42
        assert result[10]["pr_state"] == "approved_ci_green"
        assert 20 in result
        assert result[20]["pr_state"] == "ci_running"

    @pytest.mark.asyncio
    async def test_multiple_prs_per_issue_keeps_highest_number(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_pr_map

        mock_prs = [
            {"number": 10, "url": "", "computed_state": "ci_failed", "state_label": "", "linked_issues": [5]},
            {"number": 20, "url": "", "computed_state": "approved_ci_green", "state_label": "", "linked_issues": [5]},
        ]
        with patch("sova.dashboard.services.pr_service.list_open_prs_with_state", return_value=mock_prs):
            result = await _fetch_pr_map()

        assert result[5]["pr_number"] == 20

    @pytest.mark.asyncio
    async def test_pr_fetch_failure_returns_empty(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_pr_map

        with patch("sova.dashboard.services.pr_service.list_open_prs_with_state", side_effect=Exception("API error")):
            result = await _fetch_pr_map()

        assert result == {}

    @pytest.mark.asyncio
    async def test_no_linked_issues_skipped(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_pr_map

        mock_prs = [{"number": 42, "url": "", "computed_state": "draft", "state_label": "", "linked_issues": []}]
        with patch("sova.dashboard.services.pr_service.list_open_prs_with_state", return_value=mock_prs):
            result = await _fetch_pr_map()

        assert result == {}


class TestParseIssueInt:
    def test_plain_number(self) -> None:
        from sova.dashboard.routers.dependencies import _parse_issue_int

        assert _parse_issue_int("507") == 507

    def test_hash_prefix(self) -> None:
        from sova.dashboard.routers.dependencies import _parse_issue_int

        assert _parse_issue_int("#507") == 507

    def test_none(self) -> None:
        from sova.dashboard.routers.dependencies import _parse_issue_int

        assert _parse_issue_int(None) is None

    def test_empty_string(self) -> None:
        from sova.dashboard.routers.dependencies import _parse_issue_int

        assert _parse_issue_int("") is None

    def test_malformed(self) -> None:
        from sova.dashboard.routers.dependencies import _parse_issue_int

        assert _parse_issue_int("abc") is None

    def test_whitespace(self) -> None:
        from sova.dashboard.routers.dependencies import _parse_issue_int

        assert _parse_issue_int("  42 ") == 42


class TestFetchAgentMap:
    @pytest.mark.asyncio
    async def test_builds_agent_map(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_agent_map

        mock_data = {
            "agents": [
                {"issue": "10", "run_id": 5, "role": "developer", "status": "running", "elapsed_seconds": 90},
            ]
        }
        with patch("sova.dashboard.services.agent_lifecycle.get_unified_agents", return_value=mock_data):
            result = await _fetch_agent_map()
        assert result[10]["run_id"] == 5
        assert result[10]["role"] == "developer"
        assert result[10]["elapsed_seconds"] == 90

    @pytest.mark.asyncio
    async def test_multiple_agents_same_issue_keeps_highest_run_id(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_agent_map

        mock_data = {
            "agents": [
                {"issue": "10", "run_id": 3, "role": "developer", "status": "running"},
                {"issue": "10", "run_id": 7, "role": "developer", "status": "running"},
            ]
        }
        with patch("sova.dashboard.services.agent_lifecycle.get_unified_agents", return_value=mock_data):
            result = await _fetch_agent_map()
        assert result[10]["run_id"] == 7

    @pytest.mark.asyncio
    async def test_agent_fetch_failure_returns_empty(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_agent_map

        with patch("sova.dashboard.services.agent_lifecycle.get_unified_agents", side_effect=Exception("fail")):
            result = await _fetch_agent_map()
        assert result == {}

    @pytest.mark.asyncio
    async def test_skips_agents_without_issue(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_agent_map

        mock_data = {"agents": [{"issue": None, "run_id": 1, "role": "dev", "status": "running"}]}
        with patch("sova.dashboard.services.agent_lifecycle.get_unified_agents", return_value=mock_data):
            result = await _fetch_agent_map()
        assert result == {}

    @pytest.mark.asyncio
    async def test_handles_hash_prefix(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_agent_map

        mock_data = {"agents": [{"issue": "#42", "run_id": 1, "role": "dev", "status": "running"}]}
        with patch("sova.dashboard.services.agent_lifecycle.get_unified_agents", return_value=mock_data):
            result = await _fetch_agent_map()
        assert 42 in result


class TestFetchHandoffMap:
    def test_builds_handoff_map(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_handoff_map

        mock_handoffs = [
            {
                "issue": "10",
                "status": "awaiting_action",
                "next_actions": [{"label": "Address PR", "role": "address-pr"}],
            },
            {
                "issue": "20",
                "status": "awaiting_action",
                "next_actions": [{"label": "Integrate PR", "role": "integrate-pr"}],
            },
        ]
        with patch("sova.dashboard.services.handoff_service.get_all_handoffs", return_value=mock_handoffs):
            result = _fetch_handoff_map()
        assert result[10]["next_action"] == "Address PR"
        assert result[20]["next_action"] == "Integrate PR"

    def test_empty_next_actions(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_handoff_map

        mock_handoffs = [{"issue": "10", "status": "awaiting_action", "next_actions": []}]
        with patch("sova.dashboard.services.handoff_service.get_all_handoffs", return_value=mock_handoffs):
            result = _fetch_handoff_map()
        assert result[10]["next_action"] == ""

    def test_handoff_fetch_failure_returns_empty(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_handoff_map

        with patch("sova.dashboard.services.handoff_service.get_all_handoffs", side_effect=Exception("fail")):
            result = _fetch_handoff_map()
        assert result == {}

    def test_skips_handoffs_without_issue(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_handoff_map

        mock_handoffs = [{"issue": None, "status": "awaiting_action", "next_actions": [{"label": "test"}]}]
        with patch("sova.dashboard.services.handoff_service.get_all_handoffs", return_value=mock_handoffs):
            result = _fetch_handoff_map()
        assert result == {}

    def test_handles_hash_prefix(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_handoff_map

        mock_handoffs = [{"issue": "#42", "status": "awaiting_action", "next_actions": [{"label": "Review"}]}]
        with patch("sova.dashboard.services.handoff_service.get_all_handoffs", return_value=mock_handoffs):
            result = _fetch_handoff_map()
        assert 42 in result

    def test_filters_out_non_awaiting_action_status(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_handoff_map

        mock_handoffs = [
            {"issue": "10", "status": "awaiting_action", "next_actions": [{"label": "Review"}]},
            {"issue": "20", "status": "completed", "next_actions": [{"label": "Address PR"}]},
            {"issue": "30", "status": "failed", "next_actions": [{"label": "Retry"}]},
        ]
        with patch("sova.dashboard.services.handoff_service.get_all_handoffs", return_value=mock_handoffs):
            result = _fetch_handoff_map()
        assert 10 in result
        assert 20 not in result
        assert 30 not in result


class TestFetchLastRunMap:
    @pytest.mark.asyncio
    async def test_last_run_fetch_failure_returns_empty(self) -> None:
        from sova.dashboard.routers.dependencies import _fetch_last_run_map

        with patch("sova.dashboard.project_context.get_project_dir", side_effect=Exception("no project")):
            result = await _fetch_last_run_map()
        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_newest_terminal_run_per_issue(self, setup_db) -> None:
        from sova.dashboard.routers.dependencies import _fetch_last_run_map
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        project_dir = "/tmp/test-project"

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                session.add(TaskRun(id=1, issue_number="42", status="failed", role="developer"))
                session.add(TaskRun(id=2, issue_number="42", status="done", role="developer"))
                session.add(TaskRun(id=3, issue_number="42", status="running", role="developer"))
                session.add(TaskRun(id=4, issue_number="99", status="interrupted", role="researcher"))

        with patch("sova.dashboard.project_context.get_project_dir", return_value=project_dir):
            result = await _fetch_last_run_map()

        assert 42 in result
        assert result[42]["run_id"] == 2
        assert result[42]["status"] == "done"
        assert 99 in result
        assert result[99]["run_id"] == 4
        assert result[99]["status"] == "interrupted"


class TestDependencyAPI:
    @pytest.mark.asyncio
    async def test_graph_endpoint(self, client: AsyncClient) -> None:
        tasks = [
            _task(1, title="Base", state=TaskState.DONE),
            _task(2, title="Feature", body="## Dependencies\n- #1\n"),
        ]
        p1, p2, p3 = _mock_graph_context(tasks)
        f1, f2, f3, f4 = _mock_fetch_context()
        with p1, p2, p3, f1, f2, f3, f4:
            resp = await client.get("/api/dependencies/graph")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert "validation" in data

    @pytest.mark.asyncio
    async def test_graph_endpoint_with_pr_enrichment(self, client: AsyncClient) -> None:
        tasks = [_task(1, title="In Review", state=TaskState.IN_REVIEW)]
        pr_map = {
            1: {
                "pr_number": 99,
                "pr_url": "https://github.com/test/pull/99",
                "pr_state": "approved_ci_green",
                "pr_state_label": "Ready to Merge",
            }
        }
        p1, p2, p3 = _mock_graph_context(tasks)
        f1, f2, f3, f4 = _mock_fetch_context(pr_map=pr_map)
        with p1, p2, p3, f1, f2, f3, f4:
            resp = await client.get("/api/dependencies/graph")
        assert resp.status_code == 200
        node = resp.json()["nodes"][0]
        assert node["pr_number"] == 99
        assert node["pr_state"] == "approved_ci_green"
        # PR-state-aware action override: approved_ci_green -> integrate-pr only
        assert len(node["available_actions"]) == 1
        assert node["available_actions"][0]["role"] == "integrate-pr"

    @pytest.mark.asyncio
    async def test_graph_endpoint_pr_fetch_failure(self, client: AsyncClient) -> None:
        """PR fetch failure should not break the graph endpoint (fail-open)."""
        tasks = [_task(1, title="Feature")]
        p1, p2, p3 = _mock_graph_context(tasks)
        f1, f2, f3, f4 = _mock_fetch_context()
        with p1, p2, p3, f1, f2, f3, f4:
            resp = await client.get("/api/dependencies/graph")
        assert resp.status_code == 200
        node = resp.json()["nodes"][0]
        assert "pr_number" not in node

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

    @pytest.mark.asyncio
    async def test_graph_endpoint_error(self, client: AsyncClient) -> None:
        f1, f2, f3, f4 = _mock_fetch_context()
        with (
            patch(
                "sova.dashboard.routers.dependencies._build_graph",
                side_effect=RuntimeError("boom"),
            ),
            f1,
            f2,
            f3,
            f4,
        ):
            resp = await client.get("/api/dependencies/graph")
        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_graph_endpoint_config_error(self, client: AsyncClient) -> None:
        from sova.dashboard.routers.dependencies import _ConfigError

        f1, f2, f3, f4 = _mock_fetch_context()
        with (
            patch(
                "sova.dashboard.routers.dependencies._build_graph",
                side_effect=_ConfigError("bad config"),
            ),
            f1,
            f2,
            f3,
            f4,
        ):
            resp = await client.get("/api/dependencies/graph")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_ready_endpoint_error(self, client: AsyncClient) -> None:
        with patch(
            "sova.dashboard.routers.dependencies._build_graph",
            side_effect=RuntimeError("boom"),
        ):
            resp = await client.get("/api/dependencies/ready")
        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_ready_endpoint_config_error(self, client: AsyncClient) -> None:
        from sova.dashboard.routers.dependencies import _ConfigError

        with patch(
            "sova.dashboard.routers.dependencies._build_graph",
            side_effect=_ConfigError("bad config"),
        ):
            resp = await client.get("/api/dependencies/ready")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_chain_endpoint_error(self, client: AsyncClient) -> None:
        with patch(
            "sova.dashboard.routers.dependencies._build_graph",
            side_effect=RuntimeError("boom"),
        ):
            resp = await client.get("/api/dependencies/chain/1")
        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_chain_endpoint_config_error(self, client: AsyncClient) -> None:
        from sova.dashboard.routers.dependencies import _ConfigError

        with patch(
            "sova.dashboard.routers.dependencies._build_graph",
            side_effect=_ConfigError("bad config"),
        ):
            resp = await client.get("/api/dependencies/chain/1")
        assert resp.status_code == 503
