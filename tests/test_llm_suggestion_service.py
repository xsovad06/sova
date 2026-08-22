"""Tests for the LLM action suggestion service."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from cachetools import TTLCache

from sova.dashboard.services.llm_suggestion_service import (
    _PR_ACTION_LABELS,
    _make_cache_key,
    clear_cache,
    get_llm_suggestion,
)
from sova.llm.models import LLMResult


@pytest.fixture(autouse=True)
def reset_cache() -> None:
    clear_cache()
    yield  # type: ignore[misc]
    clear_cache()


def _mock_llm_result(action_id: str, reasoning: str = "test reason") -> LLMResult:
    """Build a mock LLMResult returning valid JSON."""
    content = json.dumps({"action_id": action_id, "reasoning": reasoning})
    return LLMResult(text=content, model="claude-haiku-4-5-20251001")


def _kwargs(**overrides: object) -> dict:
    defaults: dict = {
        "pr_number": 378,
        "deterministic_state": "pr_sova_pending",
        "deterministic_action_id": "review_pr",
        "pr_computed_state": "approved_ci_green",
        "has_sova_review": False,
        "sova_verdict": None,
        "mergeable": "MERGEABLE",
        "review_decision": "APPROVED",
        "ci_passed": True,
        "external_reviews_enabled": True,
    }
    defaults.update(overrides)
    return defaults


class TestGetLlmSuggestion:
    async def test_calls_provider_and_parses_response(self) -> None:
        mock_result = _mock_llm_result("integrate", "PR is approved and CI green")
        with patch(
            "sova.dashboard.services.llm_suggestion_service.invoke",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await get_llm_suggestion(**_kwargs())

        assert result is not None
        assert result["action_id"] == "integrate"
        assert result["action_label"] == "Integrate PR"
        assert result["reasoning"] == "PR is approved and CI green"
        assert result["disagrees"] is True

    async def test_disagrees_false_when_llm_matches_deterministic(self) -> None:
        mock_result = _mock_llm_result("review_pr")
        with patch(
            "sova.dashboard.services.llm_suggestion_service.invoke",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await get_llm_suggestion(**_kwargs(deterministic_action_id="review_pr"))

        assert result is not None
        assert result["disagrees"] is False

    async def test_returns_none_on_provider_error(self) -> None:
        with patch(
            "sova.dashboard.services.llm_suggestion_service.invoke",
            new_callable=AsyncMock,
            side_effect=RuntimeError("provider error"),
        ):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_returns_none_on_invalid_action_id(self) -> None:
        mock_result = LLMResult(
            text=json.dumps({"action_id": "not_a_valid_action", "reasoning": "test"}),
            model="claude-haiku-4-5-20251001",
        )
        with patch(
            "sova.dashboard.services.llm_suggestion_service.invoke",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_returns_none_on_malformed_json(self) -> None:
        mock_result = LLMResult(text="not json at all", model="claude-haiku-4-5-20251001")
        with patch(
            "sova.dashboard.services.llm_suggestion_service.invoke",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_caches_result_second_call_skips_provider(self) -> None:
        mock_result = _mock_llm_result("integrate")
        mock_invoke = AsyncMock(return_value=mock_result)
        with patch("sova.dashboard.services.llm_suggestion_service.invoke", mock_invoke):
            result1 = await get_llm_suggestion(**_kwargs())
            result2 = await get_llm_suggestion(**_kwargs())

        assert result1 == result2
        assert mock_invoke.call_count == 1

    async def test_different_pr_numbers_are_cached_separately(self) -> None:
        results = [_mock_llm_result("integrate"), _mock_llm_result("review_pr")]
        mock_invoke = AsyncMock(side_effect=results)
        with patch("sova.dashboard.services.llm_suggestion_service.invoke", mock_invoke):
            result_a = await get_llm_suggestion(**_kwargs(pr_number=100))
            result_b = await get_llm_suggestion(**_kwargs(pr_number=200))

        assert result_a["action_id"] == "integrate"
        assert result_b["action_id"] == "review_pr"
        assert mock_invoke.call_count == 2

    async def test_different_states_are_cached_separately(self) -> None:
        results = [_mock_llm_result("integrate"), _mock_llm_result("address_pr")]
        mock_invoke = AsyncMock(side_effect=results)
        with patch("sova.dashboard.services.llm_suggestion_service.invoke", mock_invoke):
            result_a = await get_llm_suggestion(**_kwargs(deterministic_state="pr_sova_pending"))
            result_b = await get_llm_suggestion(**_kwargs(deterministic_state="pr_awaiting_review"))

        assert result_a["action_id"] == "integrate"
        assert result_b["action_id"] == "address_pr"

    async def test_expired_cache_entry_is_deleted(self) -> None:
        import sova.dashboard.services.llm_suggestion_service as mod

        fake_time = [0.0]
        test_cache: TTLCache[str, dict] = TTLCache(maxsize=100, ttl=mod._CACHE_TTL, timer=lambda: fake_time[0])

        mock_result = _mock_llm_result("integrate")
        mock_invoke = AsyncMock(return_value=mock_result)
        with patch("sova.dashboard.services.llm_suggestion_service.invoke", mock_invoke):
            original_cache = mod._cache
            mod._cache = test_cache
            try:
                await get_llm_suggestion(**_kwargs())
                assert len(mod._cache) == 1

                fake_time[0] = mod._CACHE_TTL + 1

                await get_llm_suggestion(**_kwargs())
                assert mock_invoke.call_count == 2
            finally:
                mod._cache = original_cache

    async def test_returns_none_on_prompt_format_error(self) -> None:
        with patch(
            "sova.dashboard.services.llm_suggestion_service._PROMPT",
            "{missing_key_that_does_not_exist}",
        ):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_state_change_invalidates_cache(self) -> None:
        """When deterministic_state changes, cached result for old state is not reused."""
        results = [_mock_llm_result("review_pr"), _mock_llm_result("integrate")]
        mock_invoke = AsyncMock(side_effect=results)
        with patch("sova.dashboard.services.llm_suggestion_service.invoke", mock_invoke):
            result_a = await get_llm_suggestion(**_kwargs(deterministic_state="pr_sova_pending"))
            result_b = await get_llm_suggestion(**_kwargs(deterministic_state="pr_approved"))

        assert result_a["action_id"] == "review_pr"
        assert result_b["action_id"] == "integrate"
        assert mock_invoke.call_count == 2


class TestMakeCacheKey:
    def test_unique_by_pr_number(self) -> None:
        k1 = _make_cache_key(1, "pr_sova_pending", "approved_ci_green")
        k2 = _make_cache_key(2, "pr_sova_pending", "approved_ci_green")
        assert k1 != k2

    def test_unique_by_deterministic_state(self) -> None:
        k1 = _make_cache_key(1, "pr_sova_pending", "approved_ci_green")
        k2 = _make_cache_key(1, "pr_awaiting_review", "approved_ci_green")
        assert k1 != k2

    def test_unique_by_computed_state(self) -> None:
        k1 = _make_cache_key(1, "pr_sova_pending", "approved_ci_green")
        k2 = _make_cache_key(1, "pr_sova_pending", "approved")
        assert k1 != k2

    def test_same_args_same_key(self) -> None:
        k1 = _make_cache_key(42, "pr_approved", "approved_ci_green")
        k2 = _make_cache_key(42, "pr_approved", "approved_ci_green")
        assert k1 == k2


class TestPrActionLabels:
    def test_all_pr_actions_have_labels(self) -> None:
        expected = {"review_pr", "address_review", "address_pr", "integrate"}
        assert set(_PR_ACTION_LABELS.keys()) == expected

    def test_labels_are_non_empty_strings(self) -> None:
        for action_id, label in _PR_ACTION_LABELS.items():
            assert isinstance(label, str) and label, f"{action_id} has empty label"
