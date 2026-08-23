"""Tests for sova.supervisor.queue_maintenance."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import SupervisorConfig
from sova.supervisor.queue_maintenance import (
    _discover_ready,
    _prune_done,
    apply_planner_queue_changes,
    maintain_queue,
)


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
        adapter.list_tasks.side_effect = Exception("rate limited")

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
