"""Tests for sova.supervisor.ci_budget: CI minutes budget tracking."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.supervisor.ci_budget import (
    _UNLIMITED_SENTINEL,
    CIBudget,
    CIBudgetTracker,
    _parse_billing_response,
    _zero_budget,
    get_ci_budget_tracker,
)


class TestCIBudget:
    def test_dataclass_creation(self) -> None:
        budget = CIBudget(total=2000, used=1423, remaining=577, pct_used=71.2)
        assert budget.total == 2000
        assert budget.used == 1423
        assert budget.remaining == 577
        assert budget.pct_used == 71.2

    def test_frozen(self) -> None:
        budget = CIBudget(total=2000, used=1423, remaining=577, pct_used=71.2)
        with pytest.raises(AttributeError):
            budget.total = 3000  # type: ignore[misc]

    def test_zero_budget(self) -> None:
        budget = _zero_budget()
        assert budget.total == 0
        assert budget.used == 0
        assert budget.remaining == 0
        assert budget.pct_used == 0.0


class TestParseBillingResponse:
    def test_normal_response(self) -> None:
        data = {"total_minutes_used": 1423, "included_minutes": 2000}
        budget = _parse_billing_response(data)
        assert budget.total == 2000
        assert budget.used == 1423
        assert budget.remaining == 577
        assert budget.pct_used == 71.2

    def test_zero_usage(self) -> None:
        data = {"total_minutes_used": 0, "included_minutes": 2000}
        budget = _parse_billing_response(data)
        assert budget.remaining == 2000
        assert budget.pct_used == 0.0

    def test_full_usage(self) -> None:
        data = {"total_minutes_used": 2000, "included_minutes": 2000}
        budget = _parse_billing_response(data)
        assert budget.remaining == 0
        assert budget.pct_used == 100.0

    def test_overage(self) -> None:
        data = {"total_minutes_used": 2500, "included_minutes": 2000}
        budget = _parse_billing_response(data)
        assert budget.remaining == 0
        assert budget.pct_used == 125.0

    def test_unlimited_plan(self) -> None:
        data = {"total_minutes_used": 500, "included_minutes": 0}
        budget = _parse_billing_response(data)
        assert budget.remaining == _UNLIMITED_SENTINEL
        assert budget.pct_used == 0.0

    def test_missing_fields(self) -> None:
        data = {}
        budget = _parse_billing_response(data)
        assert budget.total == 0
        assert budget.used == 0
        assert budget.remaining == _UNLIMITED_SENTINEL

    def test_non_numeric_fields(self) -> None:
        data = {"total_minutes_used": "abc", "included_minutes": "xyz"}
        budget = _parse_billing_response(data)
        assert budget == _zero_budget()


class TestCIBudgetTracker:
    @pytest.mark.asyncio
    async def test_first_call_fetches(self) -> None:
        tracker = CIBudgetTracker(ttl_seconds=600.0)
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = '{"total_minutes_used": 100, "included_minutes": 2000}'

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=mock_result):
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                budget = await tracker.get_budget("owner/repo", "user")

        assert budget.total == 2000
        assert budget.used == 100
        assert budget.remaining == 1900

    @pytest.mark.asyncio
    async def test_cached_within_ttl(self) -> None:
        tracker = CIBudgetTracker(ttl_seconds=600.0)
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = '{"total_minutes_used": 100, "included_minutes": 2000}'

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=mock_result) as mock_run:
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                await tracker.get_budget("owner/repo", "user")
                budget2 = await tracker.get_budget("owner/repo", "user")

        assert mock_run.call_count == 1
        assert budget2.used == 100

    @pytest.mark.asyncio
    async def test_refetch_after_ttl(self) -> None:
        tracker = CIBudgetTracker(ttl_seconds=0.01)
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = '{"total_minutes_used": 100, "included_minutes": 2000}'

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=mock_result) as mock_run:
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                await tracker.get_budget("owner/repo", "user")
                await asyncio.sleep(0.02)
                await tracker.get_budget("owner/repo", "user")

        assert mock_run.call_count == 2

    @pytest.mark.asyncio
    async def test_api_failure_returns_zero(self) -> None:
        tracker = CIBudgetTracker()
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.stderr = "403 Forbidden"

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=mock_result):
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                budget = await tracker.get_budget("owner/repo", "user")

        assert budget == _zero_budget()

    @pytest.mark.asyncio
    async def test_bad_json_returns_zero(self) -> None:
        tracker = CIBudgetTracker()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = "not json"

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=mock_result):
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                budget = await tracker.get_budget("owner/repo", "user")

        assert budget == _zero_budget()

    @pytest.mark.asyncio
    async def test_per_repo_cache_isolation(self) -> None:
        tracker = CIBudgetTracker(ttl_seconds=600.0)
        result_a = MagicMock()
        result_a.success = True
        result_a.stdout = '{"total_minutes_used": 100, "included_minutes": 2000}'
        result_b = MagicMock()
        result_b.success = True
        result_b.stdout = '{"total_minutes_used": 500, "included_minutes": 3000}'

        with patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=[result_a, result_b]) as mock_run:
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                budget_a = await tracker.get_budget("owner/repo-a", "user")
                budget_b = await tracker.get_budget("owner/repo-b", "user")

        assert mock_run.call_count == 2
        assert budget_a.used == 100
        assert budget_b.used == 500

    @pytest.mark.asyncio
    async def test_array_json_returns_zero(self) -> None:
        tracker = CIBudgetTracker()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = "[]"

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=mock_result):
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                budget = await tracker.get_budget("owner/repo", "user")

        assert budget == _zero_budget()

    @pytest.mark.asyncio
    async def test_empty_repo_returns_zero(self) -> None:
        tracker = CIBudgetTracker()
        budget = await tracker.get_budget("", "user")
        assert budget == _zero_budget()

    @pytest.mark.asyncio
    async def test_run_exception_returns_zero(self) -> None:
        tracker = CIBudgetTracker()

        with patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=OSError("fail")):
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                budget = await tracker.get_budget("owner/repo", "user")

        assert budget == _zero_budget()

    @pytest.mark.asyncio
    async def test_user_404_fallback_to_org(self) -> None:
        tracker = CIBudgetTracker()
        fail_result = MagicMock()
        fail_result.success = False
        fail_result.stderr = "HTTP 404: Not Found"
        ok_result = MagicMock()
        ok_result.success = True
        ok_result.stdout = '{"total_minutes_used": 100, "included_minutes": 2000}'

        with patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=[fail_result, ok_result]) as mock_run:
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                budget = await tracker.get_budget("owner/repo", "user")

        assert budget.total == 2000
        assert budget.used == 100
        assert mock_run.call_count == 2

    @pytest.mark.asyncio
    async def test_both_endpoints_404_returns_zero(self) -> None:
        tracker = CIBudgetTracker()
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.stderr = "HTTP 404: Not Found"

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=mock_result) as mock_run:
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                budget = await tracker.get_budget("owner/repo", "user")

        assert budget == _zero_budget()
        assert mock_run.call_count == 2


class TestCIBudgetTrackerFactory:
    def test_same_identity_returns_same_instance(self) -> None:
        with patch.dict("sova.supervisor.ci_budget._trackers", {}, clear=True):
            t1 = get_ci_budget_tracker("user1")
            t2 = get_ci_budget_tracker("user1")
            assert t1 is t2

    def test_different_identity_returns_different_instance(self) -> None:
        with patch.dict("sova.supervisor.ci_budget._trackers", {}, clear=True):
            t1 = get_ci_budget_tracker("user1")
            t2 = get_ci_budget_tracker("user2")
            assert t1 is not t2

    def test_empty_identity_uses_default(self) -> None:
        with patch.dict("sova.supervisor.ci_budget._trackers", {}, clear=True):
            t1 = get_ci_budget_tracker("")
            t2 = get_ci_budget_tracker("")
            assert t1 is t2
