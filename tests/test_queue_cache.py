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
    yield
    queue_service._queue_cache.clear()


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


@pytest.mark.asyncio
async def test_cache_hit_within_ttl():
    task = _make_task("1")
    mock_adapter = AsyncMock()
    mock_adapter.list_tasks = AsyncMock(return_value=[task])
    cfg = MagicMock()
    cfg.task_source.type = "github"
    cfg.github_repo = "owner/repo"

    with (
        patch("sova.config.loader.load_config", return_value=cfg),
        patch("sova.adapters.create_adapter", return_value=mock_adapter),
        patch("sova.dashboard.services.queue_service._get_last_runs_by_issue", new_callable=AsyncMock, return_value={}),
    ):
        result1 = await queue_service.get_priority_queue(Path("/proj"))
        result2 = await queue_service.get_priority_queue(Path("/proj"))

    assert len(result1) == 1
    assert result1 is result2
    assert mock_adapter.list_tasks.call_count == 1


@pytest.mark.asyncio
async def test_cache_expires_after_ttl():
    task = _make_task("1")
    mock_adapter = AsyncMock()
    mock_adapter.list_tasks = AsyncMock(return_value=[task])
    cfg = MagicMock()
    cfg.task_source.type = "github"
    cfg.github_repo = "owner/repo"

    with (
        patch("sova.config.loader.load_config", return_value=cfg),
        patch("sova.adapters.create_adapter", return_value=mock_adapter),
        patch("sova.dashboard.services.queue_service._get_last_runs_by_issue", new_callable=AsyncMock, return_value={}),
    ):
        result1 = await queue_service.get_priority_queue(Path("/proj"))
        queue_service._queue_cache[str(Path("/proj"))] = (
            time.monotonic() - queue_service._QUEUE_CACHE_TTL - 1,
            result1,
        )
        result2 = await queue_service.get_priority_queue(Path("/proj"))

    assert mock_adapter.list_tasks.call_count == 2
    assert result1 is not result2


@pytest.mark.asyncio
async def test_multi_project_cache_isolation():
    task = _make_task("1")
    mock_adapter = AsyncMock()
    mock_adapter.list_tasks = AsyncMock(return_value=[task])
    cfg = MagicMock()
    cfg.task_source.type = "github"
    cfg.github_repo = "owner/repo"

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
    cfg = MagicMock()
    cfg.task_source.type = "github"
    cfg.github_repo = "owner/repo"
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
    cfg = MagicMock()
    cfg.task_source.type = "github"
    cfg.github_repo = "owner/repo"

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
