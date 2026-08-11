"""Tests for sova.supervisor.ci_budget: CI minutes budget tracking."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.supervisor.ci_budget import (
    _UNLIMITED_SENTINEL,
    CIBudget,
    CIBudgetTracker,
    _parse_legacy_billing_response,
    _parse_usage_response,
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


class TestParseLegacyBillingResponse:
    def test_normal_response(self) -> None:
        data = {"total_minutes_used": 1423, "included_minutes": 2000}
        budget = _parse_legacy_billing_response(data)
        assert budget.total == 2000
        assert budget.used == 1423
        assert budget.remaining == 577
        assert budget.pct_used == 71.2

    def test_zero_usage(self) -> None:
        data = {"total_minutes_used": 0, "included_minutes": 2000}
        budget = _parse_legacy_billing_response(data)
        assert budget.remaining == 2000
        assert budget.pct_used == 0.0

    def test_full_usage(self) -> None:
        data = {"total_minutes_used": 2000, "included_minutes": 2000}
        budget = _parse_legacy_billing_response(data)
        assert budget.remaining == 0
        assert budget.pct_used == 100.0

    def test_overage(self) -> None:
        data = {"total_minutes_used": 2500, "included_minutes": 2000}
        budget = _parse_legacy_billing_response(data)
        assert budget.remaining == 0
        assert budget.pct_used == 125.0

    def test_unlimited_plan(self) -> None:
        data = {"total_minutes_used": 500, "included_minutes": 0}
        budget = _parse_legacy_billing_response(data)
        assert budget.remaining == _UNLIMITED_SENTINEL
        assert budget.pct_used == 0.0

    def test_missing_fields(self) -> None:
        data = {}
        budget = _parse_legacy_billing_response(data)
        assert budget.total == 0
        assert budget.used == 0
        assert budget.remaining == _UNLIMITED_SENTINEL

    def test_non_numeric_fields(self) -> None:
        data = {"total_minutes_used": "abc", "included_minutes": "xyz"}
        budget = _parse_legacy_billing_response(data)
        assert budget == _zero_budget()


class TestParseUsageResponse:
    def test_filters_public_repos(self) -> None:
        data = {
            "usageItems": [
                {"product": "actions", "unitType": "minutes", "quantity": 2949.0, "repositoryName": "sova"},
                {"product": "actions", "unitType": "minutes", "quantity": 267.0, "repositoryName": "gwym"},
                {"product": "actions", "unitType": "minutes", "quantity": 51.0, "repositoryName": "pipeline"},
            ]
        }
        budget = _parse_usage_response(data, 2000, public_repos={"sova"})
        assert budget.used == 318
        assert budget.total == 2000
        assert budget.remaining == 1682

    def test_no_public_repos_sums_all(self) -> None:
        data = {
            "usageItems": [
                {"product": "actions", "unitType": "minutes", "quantity": 100.0, "repositoryName": "repo-a"},
                {"product": "actions", "unitType": "minutes", "quantity": 200.0, "repositoryName": "repo-b"},
            ]
        }
        budget = _parse_usage_response(data, 2000, public_repos=set())
        assert budget.used == 300

    def test_all_public_repos_zero_used(self) -> None:
        data = {
            "usageItems": [
                {"product": "actions", "unitType": "minutes", "quantity": 500.0, "repositoryName": "public-a"},
                {"product": "actions", "unitType": "minutes", "quantity": 300.0, "repositoryName": "public-b"},
            ]
        }
        budget = _parse_usage_response(data, 2000, public_repos={"public-a", "public-b"})
        assert budget.used == 0
        assert budget.remaining == 2000

    def test_filters_non_actions_products(self) -> None:
        data = {
            "usageItems": [
                {"product": "actions", "unitType": "minutes", "quantity": 100.0, "repositoryName": "gwym"},
                {"product": "packages", "quantity": 50.0, "repositoryName": "gwym"},
            ]
        }
        budget = _parse_usage_response(data, 2000)
        assert budget.used == 100

    def test_empty_usage_items(self) -> None:
        budget = _parse_usage_response({"usageItems": []}, 2000)
        assert budget.used == 0
        assert budget.remaining == 2000

    def test_unlimited_plan(self) -> None:
        data = {
            "usageItems": [
                {"product": "actions", "unitType": "minutes", "quantity": 500.0, "repositoryName": "gwym"},
            ]
        }
        budget = _parse_usage_response(data, 0)
        assert budget.remaining == _UNLIMITED_SENTINEL
        assert budget.pct_used == 0.0

    def test_skips_non_dict_items(self) -> None:
        data = {
            "usageItems": [
                "not a dict",
                {"product": "actions", "unitType": "minutes", "quantity": 100.0, "repositoryName": "gwym"},
            ]
        }
        budget = _parse_usage_response(data, 2000)
        assert budget.used == 100

    def test_none_public_repos_treated_as_empty(self) -> None:
        data = {
            "usageItems": [
                {"product": "actions", "unitType": "minutes", "quantity": 100.0, "repositoryName": "gwym"},
            ]
        }
        budget = _parse_usage_response(data, 2000, public_repos=None)
        assert budget.used == 100

    def test_fractional_quantities_accumulate(self) -> None:
        data = {
            "usageItems": [
                {"product": "actions", "unitType": "minutes", "quantity": 0.4, "repositoryName": "a"},
                {"product": "actions", "unitType": "minutes", "quantity": 0.4, "repositoryName": "b"},
                {"product": "actions", "unitType": "minutes", "quantity": 0.4, "repositoryName": "c"},
            ]
        }
        budget = _parse_usage_response(data, 2000)
        assert budget.used == 1

    def test_skips_non_numeric_quantity(self) -> None:
        data = {
            "usageItems": [
                {"product": "actions", "unitType": "minutes", "quantity": "abc", "repositoryName": "a"},
                {"product": "actions", "unitType": "minutes", "quantity": 100.0, "repositoryName": "b"},
            ]
        }
        budget = _parse_usage_response(data, 2000)
        assert budget.used == 100

    def test_skips_storage_unit_type(self) -> None:
        data = {
            "usageItems": [
                {"product": "actions", "unitType": "GiB", "quantity": 50.0, "repositoryName": "gwym"},
                {"product": "actions", "unitType": "minutes", "quantity": 100.0, "repositoryName": "gwym"},
            ]
        }
        budget = _parse_usage_response(data, 2000)
        assert budget.used == 100


class TestGetPublicRepos:
    @pytest.mark.asyncio
    async def test_identifies_public_repos(self) -> None:
        async def mock_run(*args, env=None):
            result = MagicMock()
            repo_path = args[2]
            if "public-repo" in repo_path:
                result.success = True
                result.stdout = "false"
            else:
                result.success = True
                result.stdout = "true"
            return result

        with patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=mock_run):
            public = await CIBudgetTracker._get_public_repos("owner", {"public-repo", "private-repo"}, None)

        assert public == {"public-repo"}

    @pytest.mark.asyncio
    async def test_api_failure_excludes_repo(self) -> None:
        with patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=OSError("fail")):
            public = await CIBudgetTracker._get_public_repos("owner", {"repo"}, None)

        assert public == set()

    @pytest.mark.asyncio
    async def test_empty_names_skipped(self) -> None:
        repo_result = MagicMock()
        repo_result.success = True
        repo_result.stdout = "true"

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=repo_result) as mock_run:
            await CIBudgetTracker._get_public_repos("owner", {"", "repo"}, None)

        assert len(mock_run.call_args_list) == 1


class TestCIBudgetTracker:
    @pytest.mark.asyncio
    async def test_new_api_success_filters_public(self) -> None:
        tracker = CIBudgetTracker(ttl_seconds=600.0)

        usage_result = MagicMock()
        usage_result.success = True
        usage_result.stdout = (
            '{"usageItems": ['
            '{"product": "actions", "unitType": "minutes", "quantity": 2949.0, "repositoryName": "sova"},'
            '{"product": "actions", "unitType": "minutes", "quantity": 267.0, "repositoryName": "gwym"}'
            "]}"
        )

        user_result = MagicMock()
        user_result.success = True
        user_result.stdout = '{"plan": {"name": "free"}}'

        repo_sova = MagicMock()
        repo_sova.success = True
        repo_sova.stdout = "false"

        repo_gwym = MagicMock()
        repo_gwym.success = True
        repo_gwym.stdout = "true"

        async def mock_run(*args, env=None):
            endpoint = args[2] if len(args) > 2 else ""
            if "billing/usage" in endpoint:
                return usage_result
            if endpoint == "user":
                return user_result
            if "sova" in endpoint:
                return repo_sova
            return repo_gwym

        with patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=mock_run):
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                budget = await tracker.get_budget("owner/repo", "user")

        assert budget.total == 2000
        assert budget.used == 267
        assert budget.remaining == 1733

    @pytest.mark.asyncio
    async def test_new_api_failure_falls_back_to_legacy(self) -> None:
        tracker = CIBudgetTracker(ttl_seconds=600.0)

        fail_result = MagicMock()
        fail_result.success = False
        fail_result.stderr = "HTTP 403"

        legacy_result = MagicMock()
        legacy_result.success = True
        legacy_result.stdout = '{"total_minutes_used": 100, "included_minutes": 2000}'

        call_count = 0

        async def mock_run(*args, env=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return fail_result
            return legacy_result

        with patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=mock_run):
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                budget = await tracker.get_budget("owner/repo", "user")

        assert budget.total == 2000
        assert budget.used == 100

    @pytest.mark.asyncio
    async def test_cached_within_ttl(self) -> None:
        tracker = CIBudgetTracker(ttl_seconds=600.0)

        usage_result = MagicMock()
        usage_result.success = True
        usage_result.stdout = (
            '{"usageItems": [{"product": "actions", "unitType": "minutes",'
            ' "quantity": 100.0, "repositoryName": "gwym"}]}'
        )

        user_result = MagicMock()
        user_result.success = True
        user_result.stdout = '{"plan": {"name": "free"}}'

        repo_result = MagicMock()
        repo_result.success = True
        repo_result.stdout = "true"

        async def mock_run(*args, env=None):
            endpoint = args[2] if len(args) > 2 else ""
            if "billing/usage" in endpoint:
                return usage_result
            if endpoint == "user":
                return user_result
            return repo_result

        with patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=mock_run) as mock_run_fn:
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                await tracker.get_budget("owner/repo", "user")
                calls_after_first = mock_run_fn.call_count
                budget2 = await tracker.get_budget("owner/repo", "user")

        assert budget2.used == 100
        assert mock_run_fn.call_count == calls_after_first

    @pytest.mark.asyncio
    async def test_refetch_after_ttl(self) -> None:
        tracker = CIBudgetTracker(ttl_seconds=0.01)

        usage_result = MagicMock()
        usage_result.success = True
        usage_result.stdout = (
            '{"usageItems": [{"product": "actions", "unitType": "minutes",'
            ' "quantity": 100.0, "repositoryName": "gwym"}]}'
        )

        user_result = MagicMock()
        user_result.success = True
        user_result.stdout = '{"plan": {"name": "free"}}'

        repo_result = MagicMock()
        repo_result.success = True
        repo_result.stdout = "true"

        async def mock_run(*args, env=None):
            endpoint = args[2] if len(args) > 2 else ""
            if "billing/usage" in endpoint:
                return usage_result
            if endpoint == "user":
                return user_result
            return repo_result

        with patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=mock_run) as mock_run_fn:
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                await tracker.get_budget("owner/repo", "user")
                first_fetch_calls = mock_run_fn.call_count
                await asyncio.sleep(0.02)
                await tracker.get_budget("owner/repo", "user")

        assert mock_run_fn.call_count > first_fetch_calls

    @pytest.mark.asyncio
    async def test_all_apis_fail_returns_zero(self) -> None:
        tracker = CIBudgetTracker()
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.stderr = "HTTP 404: Not Found"

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=mock_result):
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                budget = await tracker.get_budget("owner/repo", "user")

        assert budget == _zero_budget()

    @pytest.mark.asyncio
    async def test_bad_json_returns_zero(self) -> None:
        tracker = CIBudgetTracker()

        fail_result = MagicMock()
        fail_result.success = True
        fail_result.stdout = "not json"

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=fail_result):
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
    async def test_legacy_user_404_fallback_to_org(self) -> None:
        """When new API fails and legacy user endpoint 404s, try org endpoint."""
        tracker = CIBudgetTracker()

        new_api_fail = MagicMock()
        new_api_fail.success = False
        new_api_fail.stderr = "HTTP 403"

        legacy_user_fail = MagicMock()
        legacy_user_fail.success = False
        legacy_user_fail.stderr = "HTTP 404: Not Found"

        legacy_org_ok = MagicMock()
        legacy_org_ok.success = True
        legacy_org_ok.stdout = '{"total_minutes_used": 100, "included_minutes": 2000}'

        call_count = 0

        async def mock_run(*args, env=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return new_api_fail
            if call_count == 2:
                return legacy_user_fail
            return legacy_org_ok

        with patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=mock_run):
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                budget = await tracker.get_budget("owner/repo", "user")

        assert budget.total == 2000
        assert budget.used == 100

    @pytest.mark.asyncio
    async def test_legacy_410_treated_as_not_found(self) -> None:
        """HTTP 410 (endpoint moved) is treated like 404, not a hard error."""
        tracker = CIBudgetTracker()

        new_api_fail = MagicMock()
        new_api_fail.success = False
        new_api_fail.stderr = "HTTP 403"

        legacy_410 = MagicMock()
        legacy_410.success = False
        legacy_410.stderr = "HTTP 410: This endpoint has been moved"

        async def mock_run(*args, env=None):
            endpoint = args[2] if len(args) > 2 else ""
            if "usage" in endpoint:
                return new_api_fail
            return legacy_410

        with patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=mock_run):
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                budget = await tracker.get_budget("owner/repo", "user")

        assert budget == _zero_budget()

    @pytest.mark.asyncio
    async def test_pro_plan_gets_3000_minutes(self) -> None:
        tracker = CIBudgetTracker(ttl_seconds=600.0)

        usage_result = MagicMock()
        usage_result.success = True
        usage_result.stdout = (
            '{"usageItems": [{"product": "actions", "unitType": "minutes",'
            ' "quantity": 100.0, "repositoryName": "gwym"}]}'
        )

        user_result = MagicMock()
        user_result.success = True
        user_result.stdout = '{"plan": {"name": "pro"}}'

        repo_result = MagicMock()
        repo_result.success = True
        repo_result.stdout = "true"

        async def mock_run(*args, env=None):
            endpoint = args[2] if len(args) > 2 else ""
            if "billing/usage" in endpoint:
                return usage_result
            if endpoint == "user":
                return user_result
            return repo_result

        with patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=mock_run):
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                budget = await tracker.get_budget("owner/repo", "user")

        assert budget.total == 3000
        assert budget.remaining == 2900


class TestGetPlanIncludedMinutes:
    @pytest.mark.asyncio
    async def test_free_plan(self) -> None:
        user_result = MagicMock()
        user_result.success = True
        user_result.stdout = '{"plan": {"name": "free"}}'

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=user_result):
            minutes = await CIBudgetTracker._get_plan_included_minutes(None)

        assert minutes == 2000

    @pytest.mark.asyncio
    async def test_unknown_plan_defaults_to_free(self) -> None:
        user_result = MagicMock()
        user_result.success = True
        user_result.stdout = '{"plan": {"name": "custom_plan"}}'

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=user_result):
            minutes = await CIBudgetTracker._get_plan_included_minutes(None)

        assert minutes == 2000

    @pytest.mark.asyncio
    async def test_api_failure_defaults_to_free(self) -> None:
        with patch("sova.utils.shell.run", new_callable=AsyncMock, side_effect=OSError("fail")):
            minutes = await CIBudgetTracker._get_plan_included_minutes(None)

        assert minutes == 2000

    @pytest.mark.asyncio
    async def test_unsuccessful_result_defaults_to_free(self) -> None:
        result = MagicMock()
        result.success = False
        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=result):
            minutes = await CIBudgetTracker._get_plan_included_minutes(None)

        assert minutes == 2000

    @pytest.mark.asyncio
    async def test_malformed_json_defaults_to_free(self) -> None:
        result = MagicMock()
        result.success = True
        result.stdout = "not valid json"
        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=result):
            minutes = await CIBudgetTracker._get_plan_included_minutes(None)

        assert minutes == 2000

    @pytest.mark.asyncio
    async def test_no_plan_object_defaults_to_free(self) -> None:
        result = MagicMock()
        result.success = True
        result.stdout = '{"login": "user"}'
        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=result):
            minutes = await CIBudgetTracker._get_plan_included_minutes(None)

        assert minutes == 2000


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
