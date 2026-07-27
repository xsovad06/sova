"""Tests for the LLM action suggestion service."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.dashboard.services.llm_suggestion_service import (
    _PR_ACTION_LABELS,
    _make_cache_key,
    clear_cache,
    get_llm_suggestion,
)


@pytest.fixture(autouse=True)
def reset_cache() -> None:
    import sova.dashboard.services.llm_suggestion_service as _mod

    clear_cache()
    _mod._warned_no_key = False
    yield  # type: ignore[misc]
    clear_cache()
    _mod._warned_no_key = False


def _mock_response(action_id: str, reasoning: str = "test reason") -> MagicMock:
    """Build a mock httpx response returning valid JSON."""
    content = json.dumps({"action_id": action_id, "reasoning": reasoning})
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {"content": [{"text": content}]}
    return mock


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
    async def test_returns_none_without_api_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_calls_anthropic_api_and_parses_response(self) -> None:
        mock_resp = _mock_response("integrate", "PR is approved and CI green")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.post.return_value = mock_resp
                result = await get_llm_suggestion(**_kwargs())

        assert result is not None
        assert result["action_id"] == "integrate"
        assert result["action_label"] == "Integrate PR"
        assert result["reasoning"] == "PR is approved and CI green"
        assert result["disagrees"] is True  # differs from "review_pr"

    async def test_disagrees_false_when_llm_matches_deterministic(self) -> None:
        mock_resp = _mock_response("review_pr")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.post.return_value = mock_resp
                result = await get_llm_suggestion(**_kwargs(deterministic_action_id="review_pr"))

        assert result is not None
        assert result["disagrees"] is False

    async def test_returns_none_on_api_error(self) -> None:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.post.side_effect = Exception("network error")
                result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_returns_none_on_invalid_action_id(self) -> None:
        content = json.dumps({"action_id": "not_a_valid_action", "reasoning": "test"})
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"content": [{"text": content}]}
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.post.return_value = mock_resp
                result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_returns_none_on_malformed_json(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"content": [{"text": "not json at all"}]}
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.post.return_value = mock_resp
                result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_caches_result_second_call_skips_api(self) -> None:
        mock_resp = _mock_response("integrate")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.post.return_value = mock_resp

                result1 = await get_llm_suggestion(**_kwargs())
                result2 = await get_llm_suggestion(**_kwargs())

        assert result1 == result2
        assert mock_client.post.call_count == 1

    async def test_different_pr_numbers_are_cached_separately(self) -> None:
        mock_resp_integrate = _mock_response("integrate")
        mock_resp_review = _mock_response("review_pr")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.post.side_effect = [mock_resp_integrate, mock_resp_review]

                result_a = await get_llm_suggestion(**_kwargs(pr_number=100))
                result_b = await get_llm_suggestion(**_kwargs(pr_number=200))

        assert result_a["action_id"] == "integrate"
        assert result_b["action_id"] == "review_pr"
        assert mock_client.post.call_count == 2

    async def test_different_states_are_cached_separately(self) -> None:
        mock_resp_integrate = _mock_response("integrate")
        mock_resp_address = _mock_response("address_pr")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.post.side_effect = [mock_resp_integrate, mock_resp_address]

                result_a = await get_llm_suggestion(**_kwargs(deterministic_state="pr_sova_pending"))
                result_b = await get_llm_suggestion(**_kwargs(deterministic_state="pr_awaiting_review"))

        assert result_a["action_id"] == "integrate"
        assert result_b["action_id"] == "address_pr"

    async def test_warns_once_on_missing_api_key(self) -> None:
        import sova.dashboard.services.llm_suggestion_service as mod

        with patch.dict("os.environ", {}, clear=True):
            with patch.object(mod.log, "warning") as mock_warn:
                await get_llm_suggestion(**_kwargs())
                await get_llm_suggestion(**_kwargs())
                await get_llm_suggestion(**_kwargs())

        assert mock_warn.call_count == 1

    async def test_warned_flag_prevents_repeated_warnings(self) -> None:
        import sova.dashboard.services.llm_suggestion_service as mod

        assert mod._warned_no_key is False
        with patch.dict("os.environ", {}, clear=True):
            await get_llm_suggestion(**_kwargs())
        assert mod._warned_no_key is True

    async def test_expired_cache_entry_is_deleted(self) -> None:
        import time

        import sova.dashboard.services.llm_suggestion_service as mod

        mock_resp = _mock_response("integrate")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.post.return_value = mock_resp

                # First call populates cache
                await get_llm_suggestion(**_kwargs())
                assert len(mod._cache) == 1

                # Expire the cache entry
                key = list(mod._cache.keys())[0]
                mod._cache[key] = (time.monotonic() - mod._CACHE_TTL - 1, mod._cache[key][1])

                # Second call should delete expired entry and re-fetch
                await get_llm_suggestion(**_kwargs())
                assert mock_client.post.call_count == 2

    async def test_returns_none_on_prompt_format_error(self) -> None:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch(
                "sova.dashboard.services.llm_suggestion_service._PROMPT",
                "{missing_key_that_does_not_exist}",
            ):
                result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_state_change_invalidates_cache(self) -> None:
        """When deterministic_state changes, cached result for old state is not reused."""
        mock_resp_a = _mock_response("review_pr")
        mock_resp_b = _mock_response("integrate")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.post.side_effect = [mock_resp_a, mock_resp_b]

                result_a = await get_llm_suggestion(**_kwargs(deterministic_state="pr_sova_pending"))
                result_b = await get_llm_suggestion(**_kwargs(deterministic_state="pr_approved"))

        assert result_a["action_id"] == "review_pr"
        assert result_b["action_id"] == "integrate"
        assert mock_client.post.call_count == 2

    async def test_warned_flag_reset_allows_new_warning(self) -> None:
        """After resetting _warned_no_key, a new warning is emitted."""
        import sova.dashboard.services.llm_suggestion_service as mod

        with patch.dict("os.environ", {}, clear=True):
            with patch.object(mod.log, "warning") as mock_warn:
                await get_llm_suggestion(**_kwargs())
                assert mock_warn.call_count == 1

                mod._warned_no_key = False
                await get_llm_suggestion(**_kwargs())
                assert mock_warn.call_count == 2


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
