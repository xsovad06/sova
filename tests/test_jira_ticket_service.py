"""Tests for sova.dashboard.services.jira_ticket_service: the cached by-key Jira ticket lookup."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from sova.adapters.base import AdapterError, Task, TaskState
from sova.adapters.jira import MAX_BULK_FETCH_KEYS, JiraAdapter
from sova.config.models import ProjectConfig, TaskSourceConfig
from sova.dashboard.services.jira_ticket_service import lookup_jira_tickets


def _cfg(project_key: str = "ARVAC", base_url: str = "https://jira.example.test") -> ProjectConfig:
    return ProjectConfig(
        task_source=TaskSourceConfig(
            type="jira",
            jira_project_key=project_key,
            jira_base_url=base_url,
            jira_email="user@example.test",
            jira_api_token="token",
        )
    )


def _ticket(key: str) -> Task:
    return Task(id=key.split("-")[-1], title=key, state=TaskState.BACKLOG, metadata={"key": key})


@pytest.fixture
def adapter() -> Iterator[MagicMock]:
    fake = MagicMock(spec=JiraAdapter)
    fake.get_tasks_by_keys = AsyncMock(side_effect=lambda keys: [_ticket(k) for k in keys])
    fake.close = AsyncMock()
    with patch("sova.adapters.create_adapter", return_value=fake):
        yield fake


class TestLookupJiraTickets:
    async def test_nothing_to_look_up_makes_no_call(self, adapter: MagicMock) -> None:
        assert await lookup_jira_tickets(_cfg(), set()) == {}
        assert await lookup_jira_tickets(ProjectConfig(), {"ARVAC-1"}) == {}
        adapter.get_tasks_by_keys.assert_not_awaited()

    async def test_keys_are_normalized_into_one_request(self, adapter: MagicMock) -> None:
        found = await lookup_jira_tickets(_cfg(), [" arvac-1 ", "ARVAC-1", "ARVAC-2"])

        adapter.get_tasks_by_keys.assert_awaited_once_with(["ARVAC-1", "ARVAC-2"])
        assert set(found) == {"ARVAC-1", "ARVAC-2"}

    async def test_a_repeat_or_subset_is_answered_from_the_cache_per_project(self, adapter: MagicMock) -> None:
        await lookup_jira_tickets(_cfg(), {"ARVAC-1", "ARVAC-2"})
        subset = await lookup_jira_tickets(_cfg(), {"ARVAC-1"})
        await lookup_jira_tickets(_cfg(base_url="https://other.example.test"), {"ARVAC-1"})

        assert set(subset) == {"ARVAC-1"}
        assert adapter.get_tasks_by_keys.await_count == 2

    @pytest.mark.parametrize(
        "change",
        [
            {"jira_api_token": "other-token"},
            {"jira_email": "other@example.test"},
            {"jira_priority_mapping": {"P1 - Urgent": "critical"}},
            {"jira_status_mapping": {"Triage": "backlog"}},
        ],
        ids=["token", "email", "priority-mapping", "status-mapping"],
    )
    async def test_a_different_credential_or_mapping_misses_the_cache(self, adapter: MagicMock, change: dict) -> None:
        other = _cfg()
        other.task_source = other.task_source.model_copy(update=change)

        await lookup_jira_tickets(_cfg(), {"ARVAC-1"})
        await lookup_jira_tickets(other, {"ARVAC-1"})

        assert adapter.get_tasks_by_keys.await_count == 2

    async def test_a_failure_returns_nothing_is_not_cached_and_logs_once(self, adapter: MagicMock) -> None:
        adapter.get_tasks_by_keys.side_effect = AdapterError("HTTP 503")

        with capture_logs() as logs:
            first = await lookup_jira_tickets(_cfg(), {"ARVAC-1"})
            await lookup_jira_tickets(_cfg(), {"ARVAC-1"})

        assert first == {}
        assert adapter.get_tasks_by_keys.await_count == 2
        assert [e["event"] for e in logs].count("jira_tickets.lookup_failed") == 1

    @pytest.mark.parametrize(
        "error",
        [KeyError("fields"), TypeError("bad shape"), RuntimeError("unexpected")],
        ids=["key-error", "type-error", "runtime-error"],
    )
    async def test_an_unexpected_failure_still_falls_back(self, adapter: MagicMock, error: Exception) -> None:
        adapter.get_tasks_by_keys.side_effect = error

        with capture_logs() as logs:
            assert await lookup_jira_tickets(_cfg(), {"ARVAC-1"}) == {}

        assert any(e["event"] == "jira_tickets.lookup_error" for e in logs)
        adapter.close.assert_awaited()

    async def test_an_adapter_that_cannot_be_built_falls_back(self) -> None:
        with patch("sova.adapters.create_adapter", side_effect=RuntimeError("bad base url")):
            assert await lookup_jira_tickets(_cfg(), {"ARVAC-1"}) == {}

    async def test_keys_past_the_cap_are_skipped(self, adapter: MagicMock) -> None:
        keys = {f"ARVAC-{n}" for n in range(MAX_BULK_FETCH_KEYS + 5)}

        with capture_logs() as logs:
            found = await lookup_jira_tickets(_cfg(), keys)

        assert len(adapter.get_tasks_by_keys.await_args.args[0]) == MAX_BULK_FETCH_KEYS
        assert len(found) == MAX_BULK_FETCH_KEYS
        assert any(e["event"] == "jira_tickets.lookup_capped" for e in logs)
