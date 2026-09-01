"""Tests for merge conflict gate in TaskProgressionEngine."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import SupervisorConfig
from sova.dashboard.services.pr_service import get_pr_mergeability_map
from sova.git.pr import PRInfo
from sova.supervisor.gates.merge_conflict import check_merge_conflict_gate
from sova.supervisor.progression import (
    ProgressionAction,
    TaskProgressionEngine,
)


def _task(
    issue_id: int,
    title: str = "",
    body: str = "",
    state: TaskState = TaskState.BACKLOG,
) -> Task:
    return Task(
        id=str(issue_id),
        title=title or f"Issue #{issue_id}",
        body=body,
        state=state,
    )


def _make_engine(
    config: SupervisorConfig | None = None,
    adapter: Any | None = None,
) -> TaskProgressionEngine:
    cfg = config or SupervisorConfig()
    mock_adapter = adapter or AsyncMock()
    mock_session_factory = MagicMock()
    return TaskProgressionEngine(
        config=cfg,
        adapter=mock_adapter,
        project_dir=Path(tempfile.mkdtemp()),
        session_factory=mock_session_factory,
    )


class TestMergeConflictGate:
    def test_conflicting_blocks(self) -> None:
        mergeability = {42: "CONFLICTING"}
        result = check_merge_conflict_gate(42, mergeability)
        assert result is not None
        assert result.gate == "conflict"
        assert "#42" in result.detail

    def test_mergeable_passes(self) -> None:
        mergeability = {42: "MERGEABLE"}
        result = check_merge_conflict_gate(42, mergeability)
        assert result is None

    def test_unknown_passes(self) -> None:
        mergeability = {42: "UNKNOWN"}
        result = check_merge_conflict_gate(42, mergeability)
        assert result is None

    def test_missing_issue_passes(self) -> None:
        mergeability = {99: "CONFLICTING"}
        result = check_merge_conflict_gate(42, mergeability)
        assert result is None

    def test_empty_status_passes(self) -> None:
        mergeability = {42: ""}
        result = check_merge_conflict_gate(42, mergeability)
        assert result is None

    def test_empty_map_passes(self) -> None:
        result = check_merge_conflict_gate(42, {})
        assert result is None


_P = "sova.supervisor.progression"


class TestConflictGateIntegration:
    @pytest.mark.asyncio
    async def test_conflict_blocks_integrate(self) -> None:
        adapter = AsyncMock()
        adapter.get_state = AsyncMock(return_value=TaskState.IN_REVIEW)
        adapter.list_tasks = AsyncMock(return_value=[_task(1, state=TaskState.IN_REVIEW)])
        engine = _make_engine(
            config=SupervisorConfig(auto_integrate=True),
            adapter=adapter,
        )
        with (
            patch.object(
                engine,
                "_refine_in_review_action",
                new_callable=AsyncMock,
                return_value=(ProgressionAction.SPAWN_INTEGRATE, PRInfo(number=55, url="")),
            ),
            patch(f"{_P}.check_already_running", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_dependency_gate", return_value=None),
            patch(f"{_P}.check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_slot_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_budget_gate", new_callable=AsyncMock, return_value=None),
            patch(
                f"{_P}.check_ownership_gate",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
        ):
            decision = await engine._evaluate_single(
                1,
                TaskState.IN_REVIEW,
                MagicMock(),
                precomputed_memory=None,
                precomputed_quota=None,
                precomputed_slots=None,
                precomputed_conflicts={1: "CONFLICTING"},
            )
        assert decision.action == ProgressionAction.BLOCKED
        assert any(b.gate == "conflict" for b in decision.blocked_by)

    @pytest.mark.asyncio
    async def test_conflict_does_not_block_researcher(self) -> None:
        adapter = AsyncMock()
        adapter.get_state = AsyncMock(return_value=TaskState.TRIAGED)
        adapter.list_tasks = AsyncMock(return_value=[_task(1, state=TaskState.TRIAGED)])
        engine = _make_engine(config=SupervisorConfig(auto_research=True), adapter=adapter)
        with (
            patch(f"{_P}.check_already_running", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_dependency_gate", return_value=None),
            patch(f"{_P}.check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_slot_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_budget_gate", new_callable=AsyncMock, return_value=None),
            patch(
                f"{_P}.check_ownership_gate",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
        ):
            decision = await engine._evaluate_single(
                1,
                TaskState.TRIAGED,
                MagicMock(),
                precomputed_memory=None,
                precomputed_quota=None,
                precomputed_slots=None,
                precomputed_conflicts={1: "CONFLICTING"},
            )
        assert decision.action == ProgressionAction.SPAWN_RESEARCHER

    @pytest.mark.asyncio
    async def test_conflict_does_not_block_developer(self) -> None:
        adapter = AsyncMock()
        adapter.get_state = AsyncMock(return_value=TaskState.RESEARCHED)
        adapter.list_tasks = AsyncMock(return_value=[_task(1, state=TaskState.RESEARCHED)])
        engine = _make_engine(config=SupervisorConfig(auto_develop=True), adapter=adapter)
        with (
            patch(f"{_P}.check_already_running", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_dependency_gate", return_value=None),
            patch(f"{_P}.check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_slot_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_budget_gate", new_callable=AsyncMock, return_value=None),
            patch(
                f"{_P}.check_ownership_gate",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
        ):
            decision = await engine._evaluate_single(
                1,
                TaskState.RESEARCHED,
                MagicMock(),
                precomputed_memory=None,
                precomputed_quota=None,
                precomputed_slots=None,
                precomputed_conflicts={1: "CONFLICTING"},
            )
        assert decision.action == ProgressionAction.SPAWN_DEVELOPER

    @pytest.mark.asyncio
    async def test_no_conflicts_map_passes(self) -> None:
        adapter = AsyncMock()
        adapter.get_state = AsyncMock(return_value=TaskState.IN_REVIEW)
        adapter.list_tasks = AsyncMock(return_value=[_task(1, state=TaskState.IN_REVIEW)])
        engine = _make_engine(config=SupervisorConfig(auto_integrate=True), adapter=adapter)
        with (
            patch.object(
                engine,
                "_refine_in_review_action",
                new_callable=AsyncMock,
                return_value=(ProgressionAction.SPAWN_INTEGRATE, PRInfo(number=55, url="")),
            ),
            patch(f"{_P}.check_already_running", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_dependency_gate", return_value=None),
            patch(f"{_P}.check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_slot_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_budget_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_review_completed_gate", new_callable=AsyncMock, return_value=None),
            patch(
                f"{_P}.check_ownership_gate",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
        ):
            decision = await engine._evaluate_single(
                1,
                TaskState.IN_REVIEW,
                MagicMock(),
                precomputed_memory=None,
                precomputed_quota=None,
                precomputed_slots=None,
                precomputed_conflicts=None,
            )
        assert decision.action == ProgressionAction.SPAWN_INTEGRATE

    @pytest.mark.asyncio
    @patch("sova.supervisor.progression.load_config")
    async def test_evaluate_all_fetches_mergeability(self, mock_cfg: MagicMock) -> None:
        mock_cfg.return_value.max_parallel_agents = 5
        tasks = [_task(1, state=TaskState.IN_REVIEW)]
        adapter = AsyncMock()
        adapter.list_tasks = AsyncMock(return_value=tasks)
        engine = _make_engine(config=SupervisorConfig(auto_integrate=True, task_queue=[1]), adapter=adapter)
        with (
            patch.object(
                engine,
                "_refine_in_review_action",
                new_callable=AsyncMock,
                return_value=(ProgressionAction.SPAWN_INTEGRATE, PRInfo(number=55, url="")),
            ),
            patch(f"{_P}.check_already_running", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_dependency_gate", return_value=None),
            patch(f"{_P}.check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_budget_gate", new_callable=AsyncMock, return_value=None),
            patch(
                f"{_P}.check_ownership_gate",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch(f"{_P}.get_alive_count", new_callable=AsyncMock, return_value=0),
            patch(f"{_P}.check_memory_pressure_gate", return_value=None),
            patch(
                "sova.dashboard.services.pr_service.get_pr_mergeability_map",
                new_callable=AsyncMock,
                return_value={1: "CONFLICTING"},
            ) as mock_merge,
        ):
            decisions = await engine.evaluate_all()
        mock_merge.assert_called_once()
        assert len(decisions) == 1
        assert decisions[0].action == ProgressionAction.BLOCKED
        assert any(b.gate == "conflict" for b in decisions[0].blocked_by)

    @pytest.mark.asyncio
    @patch("sova.supervisor.progression.load_config")
    async def test_evaluate_all_mergeability_api_failure_fails_open(self, mock_cfg: MagicMock) -> None:
        mock_cfg.return_value.max_parallel_agents = 5
        tasks = [_task(1, state=TaskState.IN_REVIEW)]
        adapter = AsyncMock()
        adapter.list_tasks = AsyncMock(return_value=tasks)
        engine = _make_engine(config=SupervisorConfig(auto_integrate=True, task_queue=[1]), adapter=adapter)
        with (
            patch.object(
                engine,
                "_refine_in_review_action",
                new_callable=AsyncMock,
                return_value=(ProgressionAction.SPAWN_INTEGRATE, PRInfo(number=55, url="")),
            ),
            patch(f"{_P}.check_already_running", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_dependency_gate", return_value=None),
            patch(f"{_P}.check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_budget_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_review_completed_gate", new_callable=AsyncMock, return_value=None),
            patch(
                f"{_P}.check_ownership_gate",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch(f"{_P}.get_alive_count", new_callable=AsyncMock, return_value=0),
            patch(f"{_P}.check_memory_pressure_gate", return_value=None),
            patch(
                "sova.dashboard.services.pr_service.get_pr_mergeability_map",
                new_callable=AsyncMock,
                side_effect=RuntimeError("API error"),
            ),
        ):
            decisions = await engine.evaluate_all()
        assert len(decisions) == 1
        assert decisions[0].action == ProgressionAction.SPAWN_INTEGRATE

    @pytest.mark.asyncio
    async def test_evaluate_task_fetches_mergeability(self) -> None:
        """evaluate_task() must also check merge conflicts (not just evaluate_all)."""
        adapter = AsyncMock()
        adapter.get_state = AsyncMock(return_value=TaskState.IN_REVIEW)
        engine = _make_engine(config=SupervisorConfig(auto_integrate=True), adapter=adapter)
        with (
            patch.object(
                engine,
                "_refine_in_review_action",
                new_callable=AsyncMock,
                return_value=(ProgressionAction.SPAWN_INTEGRATE, PRInfo(number=55, url="")),
            ),
            patch(f"{_P}.check_already_running", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_dependency_gate", return_value=None),
            patch(f"{_P}.check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_slot_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_budget_gate", new_callable=AsyncMock, return_value=None),
            patch(
                f"{_P}.check_ownership_gate",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch(
                "sova.dashboard.services.pr_service.get_pr_mergeability_map",
                new_callable=AsyncMock,
                return_value={42: "CONFLICTING"},
            ) as mock_merge,
        ):
            decision = await engine.evaluate_task(42)
        mock_merge.assert_called_once()
        assert decision.action == ProgressionAction.BLOCKED
        assert any(b.gate == "conflict" for b in decision.blocked_by)

    @pytest.mark.asyncio
    async def test_evaluate_task_mergeability_failure_fails_open(self) -> None:
        """evaluate_task() fails open when mergeability fetch errors."""
        adapter = AsyncMock()
        adapter.get_state = AsyncMock(return_value=TaskState.IN_REVIEW)
        engine = _make_engine(config=SupervisorConfig(auto_integrate=True), adapter=adapter)
        with (
            patch.object(
                engine,
                "_refine_in_review_action",
                new_callable=AsyncMock,
                return_value=(ProgressionAction.SPAWN_INTEGRATE, PRInfo(number=55, url="")),
            ),
            patch(f"{_P}.check_already_running", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_dependency_gate", return_value=None),
            patch(f"{_P}.check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_slot_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_budget_gate", new_callable=AsyncMock, return_value=None),
            patch(f"{_P}.check_review_completed_gate", new_callable=AsyncMock, return_value=None),
            patch(
                f"{_P}.check_ownership_gate",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch(
                "sova.dashboard.services.pr_service.get_pr_mergeability_map",
                new_callable=AsyncMock,
                side_effect=RuntimeError("API error"),
            ),
        ):
            decision = await engine.evaluate_task(42)
        assert decision.action == ProgressionAction.SPAWN_INTEGRATE


class TestMergeabilityMap:
    @pytest.mark.asyncio
    async def test_multi_issue_pr(self) -> None:
        """A PR closing multiple issues maps all of them."""
        prs = [{"number": 10, "mergeable": "CONFLICTING", "linked_issues": [1, 2, 3], "linked_issue": 1}]
        with patch(
            "sova.dashboard.services.pr_service.list_open_prs_with_state",
            new_callable=AsyncMock,
            return_value=prs,
        ):
            result = await get_pr_mergeability_map()
        assert result == {1: "CONFLICTING", 2: "CONFLICTING", 3: "CONFLICTING"}

    @pytest.mark.asyncio
    async def test_multi_pr_same_issue_conflicting_wins(self) -> None:
        """When multiple PRs reference the same issue, CONFLICTING takes precedence."""
        prs = [
            {"number": 10, "mergeable": "MERGEABLE", "linked_issues": [1], "linked_issue": 1},
            {"number": 11, "mergeable": "CONFLICTING", "linked_issues": [1], "linked_issue": 1},
        ]
        with patch(
            "sova.dashboard.services.pr_service.list_open_prs_with_state",
            new_callable=AsyncMock,
            return_value=prs,
        ):
            result = await get_pr_mergeability_map()
        assert result[1] == "CONFLICTING"

    @pytest.mark.asyncio
    async def test_multi_pr_same_issue_no_conflict(self) -> None:
        """When no PR conflicts, the last status wins (both MERGEABLE)."""
        prs = [
            {"number": 10, "mergeable": "MERGEABLE", "linked_issues": [1], "linked_issue": 1},
            {"number": 11, "mergeable": "MERGEABLE", "linked_issues": [1], "linked_issue": 1},
        ]
        with patch(
            "sova.dashboard.services.pr_service.list_open_prs_with_state",
            new_callable=AsyncMock,
            return_value=prs,
        ):
            result = await get_pr_mergeability_map()
        assert result[1] == "MERGEABLE"

    @pytest.mark.asyncio
    async def test_empty_linked_issues_skipped(self) -> None:
        """PRs with no linked issues are ignored."""
        prs = [{"number": 10, "mergeable": "CONFLICTING", "linked_issues": [], "linked_issue": None}]
        with patch(
            "sova.dashboard.services.pr_service.list_open_prs_with_state",
            new_callable=AsyncMock,
            return_value=prs,
        ):
            result = await get_pr_mergeability_map()
        assert result == {}

    @pytest.mark.asyncio
    async def test_api_failure_returns_empty(self) -> None:
        """API failures return empty dict (fail-open)."""
        with patch(
            "sova.dashboard.services.pr_service.list_open_prs_with_state",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API down"),
        ):
            result = await get_pr_mergeability_map()
        assert result == {}
