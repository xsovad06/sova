"""Tests for get_priority_queue() TTL caching."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.dashboard.services import queue_service


@pytest.fixture(autouse=True)
def _clear_cache():
    queue_service._queue_cache.clear()
    queue_service._queue_cache_generation.clear()
    yield
    queue_service._queue_cache.clear()
    queue_service._queue_cache_generation.clear()


def _make_task(id_: str, state_val: str = "backlog") -> MagicMock:
    from sova.adapters.base import TaskState

    t = MagicMock()
    t.id = id_
    t.title = f"Task {id_}"
    t.state = TaskState(state_val)
    t.labels = []
    t.url = f"https://example.com/{id_}"
    t.milestone = None
    t.metadata = {}
    t.assignees = []
    t.issue_type = None
    t.story_points = None
    t.sprint = None
    t.components = []
    return t


def _github_cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.task_source.type = "github"
    cfg.github_repo = "owner/repo"
    return cfg


@pytest.mark.asyncio
async def test_cache_hit_within_ttl():
    task = _make_task("1")
    mock_adapter = AsyncMock()
    mock_adapter.list_tasks = AsyncMock(return_value=[task])
    cfg = _github_cfg()

    with (
        patch("sova.config.loader.load_config", return_value=cfg),
        patch("sova.adapters.create_adapter", return_value=mock_adapter),
        patch("sova.dashboard.services.queue_service._get_last_runs_by_issue", new_callable=AsyncMock, return_value={}),
    ):
        result1 = await queue_service.get_priority_queue(Path("/proj"))
        result2 = await queue_service.get_priority_queue(Path("/proj"))

    assert len(result1) == 1
    assert result1 == result2
    assert mock_adapter.list_tasks.call_count == 1


@pytest.mark.asyncio
async def test_cache_expires_after_ttl():
    task = _make_task("1")
    mock_adapter = AsyncMock()
    mock_adapter.list_tasks = AsyncMock(return_value=[task])
    cfg = _github_cfg()

    with (
        patch("sova.config.loader.load_config", return_value=cfg),
        patch("sova.adapters.create_adapter", return_value=mock_adapter),
        patch("sova.dashboard.services.queue_service._get_last_runs_by_issue", new_callable=AsyncMock, return_value={}),
    ):
        await queue_service.get_priority_queue(Path("/proj"))
        key = str(Path("/proj"))
        queue_service._queue_cache[key] = (
            time.monotonic() - queue_service._QUEUE_CACHE_TTL - 1,
            queue_service._queue_cache[key][1],
        )
        await queue_service.get_priority_queue(Path("/proj"))

    assert mock_adapter.list_tasks.call_count == 2


@pytest.mark.asyncio
async def test_multi_project_cache_isolation():
    task = _make_task("1")
    mock_adapter = AsyncMock()
    mock_adapter.list_tasks = AsyncMock(return_value=[task])
    cfg = _github_cfg()

    with (
        patch("sova.config.loader.load_config", return_value=cfg),
        patch("sova.adapters.create_adapter", return_value=mock_adapter),
        patch("sova.dashboard.services.queue_service._get_last_runs_by_issue", new_callable=AsyncMock, return_value={}),
    ):
        await queue_service.get_priority_queue(Path("/proj-a"))
        await queue_service.get_priority_queue(Path("/proj-b"))

    assert mock_adapter.list_tasks.call_count == 2
    assert str(Path("/proj-a")) in queue_service._queue_cache
    assert str(Path("/proj-b")) in queue_service._queue_cache


@pytest.mark.asyncio
async def test_adapter_error_not_cached():
    cfg = _github_cfg()
    mock_adapter = AsyncMock()
    mock_adapter.list_tasks = AsyncMock(side_effect=RuntimeError("API down"))

    with (
        patch("sova.config.loader.load_config", return_value=cfg),
        patch("sova.adapters.create_adapter", return_value=mock_adapter),
    ):
        result = await queue_service.get_priority_queue(Path("/proj"))

    assert result == []
    assert str(Path("/proj")) not in queue_service._queue_cache


@pytest.mark.asyncio
async def test_queue_passes_paginate_true():
    from sova.adapters.base import TaskFilters

    task = _make_task("1")
    mock_adapter = AsyncMock()
    mock_adapter.list_tasks = AsyncMock(return_value=[task])
    cfg = _github_cfg()

    with (
        patch("sova.config.loader.load_config", return_value=cfg),
        patch("sova.adapters.create_adapter", return_value=mock_adapter),
        patch("sova.dashboard.services.queue_service._get_last_runs_by_issue", new_callable=AsyncMock, return_value={}),
    ):
        await queue_service.get_priority_queue(Path("/proj"))

    mock_adapter.list_tasks.assert_called_once()
    filters_arg = mock_adapter.list_tasks.call_args[0][0]
    assert isinstance(filters_arg, TaskFilters)
    assert filters_arg.paginate is True


@pytest.mark.asyncio
async def test_last_run_refreshed_while_task_list_cached():
    """A run reaching awaiting_approval must show on the next call, not after the TTL (#1149)."""
    task = _make_task("1148", "triaged")
    mock_adapter = AsyncMock()
    mock_adapter.list_tasks = AsyncMock(return_value=[task])
    running = {"1148": {"id": 7, "status": "running", "role": "researcher"}}
    awaiting = {"1148": {"id": 7, "status": "awaiting_approval", "role": "researcher"}}

    with (
        patch("sova.config.loader.load_config", return_value=_github_cfg()),
        patch("sova.adapters.create_adapter", return_value=mock_adapter),
        patch(
            "sova.dashboard.services.queue_service._get_last_runs_by_issue",
            new_callable=AsyncMock,
            side_effect=[running, awaiting],
        ),
    ):
        first = await queue_service.get_priority_queue(Path("/proj"))
        second = await queue_service.get_priority_queue(Path("/proj"))

    assert mock_adapter.list_tasks.call_count == 1
    assert first[0]["last_run"]["status"] == "running"
    assert second[0]["last_run"]["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_spec_status_reenriched_on_every_call():
    task = _make_task("1", "researched")
    mock_adapter = AsyncMock()
    mock_adapter.list_tasks = AsyncMock(return_value=[task])

    with (
        patch("sova.config.loader.load_config", return_value=_github_cfg()),
        patch("sova.adapters.create_adapter", return_value=mock_adapter),
        patch("sova.dashboard.services.queue_service._get_last_runs_by_issue", new_callable=AsyncMock, return_value={}),
        patch("sova.dashboard.services.queue_service._enrich_spec_status", new_callable=AsyncMock) as enrich,
    ):
        await queue_service.get_priority_queue(Path("/proj"))
        await queue_service.get_priority_queue(Path("/proj"))

    assert mock_adapter.list_tasks.call_count == 1
    assert enrich.await_count == 2


@pytest.mark.asyncio
async def test_calls_do_not_share_mutable_queue_dicts():
    """Each call builds fresh dicts, so a caller mutating one cannot leak into the next."""
    task = _make_task("1")
    mock_adapter = AsyncMock()
    mock_adapter.list_tasks = AsyncMock(return_value=[task])

    with (
        patch("sova.config.loader.load_config", return_value=_github_cfg()),
        patch("sova.adapters.create_adapter", return_value=mock_adapter),
        patch("sova.dashboard.services.queue_service._get_last_runs_by_issue", new_callable=AsyncMock, return_value={}),
    ):
        first = await queue_service.get_priority_queue(Path("/proj"))
        first[0]["state"] = "mutated"
        second = await queue_service.get_priority_queue(Path("/proj"))

    assert second[0]["state"] == "backlog"


@pytest.mark.asyncio
async def test_mutating_returned_lists_does_not_corrupt_cached_task():
    """labels/assignees/components must be copies, not aliases of the cached Task's lists."""
    task = _make_task("1")
    task.labels = ["bug"]
    task.assignees = ["alice"]
    task.components = ["core"]
    mock_adapter = AsyncMock()
    mock_adapter.list_tasks = AsyncMock(return_value=[task])

    with (
        patch("sova.config.loader.load_config", return_value=_github_cfg()),
        patch("sova.adapters.create_adapter", return_value=mock_adapter),
        patch("sova.dashboard.services.queue_service._get_last_runs_by_issue", new_callable=AsyncMock, return_value={}),
    ):
        first = await queue_service.get_priority_queue(Path("/proj"))
        first[0]["labels"].append("mutated")
        first[0]["assignees"].append("mutated")
        first[0]["components"].append("mutated")
        second = await queue_service.get_priority_queue(Path("/proj"))

    assert second[0]["labels"] == ["bug"]
    assert second[0]["assignees"] == ["alice"]
    assert second[0]["components"] == ["core"]
    assert task.labels == ["bug"]


class TestInvalidateQueueCache:
    def test_drops_entry_matching_resolved_path(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        unresolved = project / ".." / "proj"
        queue_service._queue_cache[str(unresolved)] = (time.monotonic(), [])

        queue_service.invalidate_queue_cache(project.resolve())

        assert queue_service._queue_cache == {}

    def test_drops_single_project_entry(self, tmp_path: Path) -> None:
        queue_service._queue_cache[""] = (time.monotonic(), [])

        queue_service.invalidate_queue_cache(tmp_path)

        assert "" not in queue_service._queue_cache

    def test_keeps_other_projects(self, tmp_path: Path) -> None:
        other = str(tmp_path / "other")
        queue_service._queue_cache[other] = (time.monotonic(), [])

        queue_service.invalidate_queue_cache(tmp_path / "proj")

        assert other in queue_service._queue_cache

    @pytest.mark.asyncio
    async def test_next_call_refetches_after_invalidation(self) -> None:
        task = _make_task("1")
        mock_adapter = AsyncMock()
        mock_adapter.list_tasks = AsyncMock(return_value=[task])

        with (
            patch("sova.config.loader.load_config", return_value=_github_cfg()),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
            patch(
                "sova.dashboard.services.queue_service._get_last_runs_by_issue",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            await queue_service.get_priority_queue(None)
            queue_service.invalidate_queue_cache(Path("/anything"))
            await queue_service.get_priority_queue(None)

        assert mock_adapter.list_tasks.call_count == 2

    @pytest.mark.asyncio
    async def test_in_flight_fetch_does_not_resurrect_stale_entry_after_invalidation(self) -> None:
        """A fetch started before invalidation must not repopulate the cache once it lands."""
        task = _make_task("1")

        async def _slow_list_tasks(*_args: object, **_kwargs: object) -> list:
            # Invalidate mid-fetch, simulating an agent finishing while this
            # project's queue is already being refetched for another request.
            queue_service.invalidate_queue_cache(Path("/proj"))
            return [task]

        mock_adapter = AsyncMock()
        mock_adapter.list_tasks = AsyncMock(side_effect=_slow_list_tasks)

        with (
            patch("sova.config.loader.load_config", return_value=_github_cfg()),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
            patch(
                "sova.dashboard.services.queue_service._get_last_runs_by_issue",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            await queue_service.get_priority_queue(Path("/proj"))
            # The invalidation fired after the fetch had already started, so
            # the result must not have been cached.
            await queue_service.get_priority_queue(Path("/proj"))

        assert mock_adapter.list_tasks.call_count == 2
