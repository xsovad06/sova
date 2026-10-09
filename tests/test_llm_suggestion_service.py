"""Tests for the LLM action suggestion service."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cachetools import TTLCache

from sova.config.models import ProjectConfig
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


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    """Reset module-level state between tests."""
    import sova.dashboard.services.llm_suggestion_service as mod

    mod._warned_not_anthropic_capable = False
    yield  # type: ignore[misc]
    mod._warned_not_anthropic_capable = False


def _cfg(provider: str = "claude-code", model: str = "", llm_suggestions: bool = True) -> ProjectConfig:
    cfg = ProjectConfig()
    cfg.llm.provider = provider  # type: ignore[assignment]
    if model:
        cfg.llm.model = model
    cfg.dashboard.llm_suggestions = llm_suggestions
    return cfg


def _raw_result(text: str) -> LLMResult:
    return LLMResult(text=text, model="claude-haiku-4-5-20251001")


def _llm_result(action_id: str, reasoning: str = "test reason") -> LLMResult:
    return _raw_result(json.dumps({"action_id": action_id, "reasoning": reasoning}))


def _mock_invoke(result: LLMResult | None = None, *, side_effect: object = None) -> AsyncMock:
    """Build a stub for ``sova.llm.client.invoke``. *side_effect* takes an exception
    or a list of per-call results."""
    return AsyncMock(return_value=result, side_effect=side_effect)


@contextmanager
def _patch_service(cfg: ProjectConfig, invoke_mock: AsyncMock) -> Iterator[None]:
    """Patch ``load_config`` (fresh per call, via the real ``asyncio.to_thread``) and
    ``sova.llm.client.invoke`` for a typical test.

    Patching ``load_config`` directly rather than ``asyncio.to_thread`` itself keeps
    the real ``to_thread`` seam live, so a test can never accidentally swallow an
    unrelated ``asyncio.to_thread`` call elsewhere in the same request path.
    """
    with (
        patch("sova.dashboard.services.llm_suggestion_service.load_config", new=MagicMock(return_value=cfg)),
        patch("sova.dashboard.services.llm_suggestion_service.llm_invoke", new=invoke_mock),
    ):
        yield


def _kwargs(**overrides: object) -> dict:
    defaults: dict = {
        "pr_number": 378,
        "deterministic_state": "pr_sova_pending",
        "deterministic_action_id": "review_pr",
        "pr_computed_state": "approved_ci_green",
        "has_sova_review": False,
        "sova_verdict": None,
        "mergeable": "MERGEABLE",
        "merge_state": "CLEAN",
        "review_decision": "APPROVED",
        "ci_passed": True,
        "external_reviews_enabled": True,
    }
    defaults.update(overrides)
    return defaults


class TestGetLlmSuggestion:
    async def test_calls_provider_and_parses_response(self) -> None:
        invoke_mock = _mock_invoke(_llm_result("integrate", "PR is approved and CI green"))
        cfg = _cfg(provider="claude-code")

        with _patch_service(cfg, invoke_mock):
            result = await get_llm_suggestion(**_kwargs())

        assert result is not None
        assert result["action_id"] == "integrate"
        assert result["action_label"] == "Integrate PR"
        assert result["reasoning"] == "PR is approved and CI green"
        assert result["disagrees"] is True

        invoke_mock.assert_called_once()
        call_kwargs = invoke_mock.call_args.kwargs
        assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
        assert call_kwargs["max_tokens"] == 200
        assert call_kwargs["isolated"] is True
        assert call_kwargs["task_type"] == "pr_suggestion"

    async def test_vertex_provider_uses_litellm_prefixed_vertex_model(self) -> None:
        """llm.provider="vertex" routes through LiteLLMProvider, which forwards the ID
        to litellm verbatim: without the "vertex_ai/" prefix litellm reads a bare
        "claude-..." ID as Anthropic-direct and never reaches the operator's Vertex
        project at all (#924)."""
        invoke_mock = _mock_invoke(_llm_result("integrate"))
        cfg = _cfg(provider="vertex", model="vertex_ai/claude-haiku-4-5")

        with _patch_service(cfg, invoke_mock):
            await get_llm_suggestion(**_kwargs())

        assert invoke_mock.call_args.kwargs["model"] == "vertex_ai/claude-haiku-4-5@20251001"

    async def test_vertex_routed_cli_gets_pinned_vertex_id_not_firstparty(self) -> None:
        """CLAUDE_CODE_USE_VERTEX redirects the CLI to a deployment that rejects the
        firstParty "claude-haiku-4-5-20251001" form and needs an @-pinned ID, and
        unlike llm.provider="vertex" it expects that ID without a litellm prefix."""
        import os

        invoke_mock = _mock_invoke(_llm_result("integrate"))
        cfg = _cfg(provider="claude-code")

        with (
            _patch_service(cfg, invoke_mock),
            patch.dict(os.environ, {"CLAUDE_CODE_USE_VERTEX": "1"}),
            patch(
                "sova.utils.env.configured_passthrough",
                return_value=("CLAUDE_CODE_USE_VERTEX",),
            ),
        ):
            await get_llm_suggestion(**_kwargs())

        assert invoke_mock.call_args.kwargs["model"] == "claude-haiku-4-5@20251001"

    async def test_vertex_provider_with_non_anthropic_model_is_disabled(self) -> None:
        """llm.provider="vertex" is generic LiteLLM Vertex routing, not Claude-only:
        its documented example model is Gemini, so a Gemini-configured project must
        not be sent a hardcoded Claude request (#924)."""
        invoke_mock = _mock_invoke(_llm_result("integrate"))
        cfg = _cfg(provider="vertex", model="vertex_ai/gemini-2.5-pro")

        with _patch_service(cfg, invoke_mock):
            result = await get_llm_suggestion(**_kwargs())

        assert result is None
        invoke_mock.assert_not_called()

    async def test_litellm_with_anthropic_model_but_no_prefix_falls_back_to_configured_model(self) -> None:
        """A bare (unprefixed) llm.model gives _model_for_provider() nothing to infer a
        vendor prefix from, so it returns None and invoke() falls back to cfg.model
        unchanged rather than guessing a prefix (#924)."""
        invoke_mock = _mock_invoke(_llm_result("integrate"))
        cfg = _cfg(provider="litellm", model="claude-sonnet-4-6")

        with _patch_service(cfg, invoke_mock):
            await get_llm_suggestion(**_kwargs())

        assert invoke_mock.call_args.kwargs["model"] is None

    async def test_litellm_with_prefixed_anthropic_model_resolves_cheap_tier(self) -> None:
        """The whole point of the cheap tier is that this advisory widget never runs on
        the operator's primary (possibly expensive) pinned model; a vendor-prefixed
        llm.model must still get the cheap tier, with the same prefix reapplied."""
        invoke_mock = _mock_invoke(_llm_result("integrate"))
        cfg = _cfg(provider="litellm", model="anthropic/claude-opus-4-1")

        with _patch_service(cfg, invoke_mock):
            await get_llm_suggestion(**_kwargs())

        assert invoke_mock.call_args.kwargs["model"] == "anthropic/claude-haiku-4-5-20251001"

    async def test_litellm_with_bedrock_prefixed_model_falls_back_to_configured_model(self) -> None:
        """Only the "anthropic/" prefix can be reapplied to the resolved cheap-tier ID
        as-is. "bedrock/" requires the "anthropic."/"us.anthropic." ID form, so
        reapplying it verbatim would produce "bedrock/claude-haiku-4-5-20251001", an ID
        Bedrock rejects; _model_for_provider() must return None instead (#924)."""
        invoke_mock = _mock_invoke(_llm_result("integrate"))
        cfg = _cfg(provider="litellm", model="bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0")

        with _patch_service(cfg, invoke_mock):
            await get_llm_suggestion(**_kwargs())

        assert invoke_mock.call_args.kwargs["model"] is None

    async def test_litellm_with_openrouter_prefixed_model_falls_back_to_configured_model(self) -> None:
        """A two-segment prefix like "openrouter/anthropic/" must not be collapsed to
        just "openrouter/" by reapplying only the first path segment (#924)."""
        invoke_mock = _mock_invoke(_llm_result("integrate"))
        cfg = _cfg(provider="litellm", model="openrouter/anthropic/claude-3-opus")

        with _patch_service(cfg, invoke_mock):
            await get_llm_suggestion(**_kwargs())

        assert invoke_mock.call_args.kwargs["model"] is None

    async def test_litellm_with_non_anthropic_model_is_disabled(self) -> None:
        invoke_mock = _mock_invoke(_llm_result("integrate"))
        cfg = _cfg(provider="litellm", model="gpt-5")

        with _patch_service(cfg, invoke_mock):
            result = await get_llm_suggestion(**_kwargs())

        assert result is None
        invoke_mock.assert_not_called()

    async def test_openai_provider_is_disabled_regardless_of_model(self) -> None:
        invoke_mock = _mock_invoke(_llm_result("integrate"))
        cfg = _cfg(provider="openai", model="claude-sonnet-4-6")

        with _patch_service(cfg, invoke_mock):
            result = await get_llm_suggestion(**_kwargs())

        assert result is None
        invoke_mock.assert_not_called()

    async def test_vertex_resolves_tier_even_when_resolve_tier_aliases_disabled(self) -> None:
        """llm.resolve_tier_aliases=False is a global, explicit "byte-identical
        passthrough" opt-out; this module's own bare "_TIER" literal must still resolve
        to a concrete ID regardless, or every call on such a deployment would fail (#924)."""
        invoke_mock = _mock_invoke(_llm_result("integrate"))
        cfg = _cfg(provider="vertex", model="vertex_ai/claude-sonnet-4-5")
        cfg.llm.resolve_tier_aliases = False

        with _patch_service(cfg, invoke_mock):
            await get_llm_suggestion(**_kwargs())

        model = invoke_mock.call_args.kwargs["model"]
        assert model is not None
        assert model not in ("haiku", "")

    async def test_warns_once_when_not_anthropic_capable(self) -> None:
        invoke_mock = _mock_invoke(_llm_result("integrate"))
        cfg = _cfg(provider="openai", model="gpt-5")

        with (
            _patch_service(cfg, invoke_mock),
            patch("sova.dashboard.services.llm_suggestion_service.log") as mock_log,
        ):
            await get_llm_suggestion(**_kwargs())
            await get_llm_suggestion(**_kwargs(pr_number=999))

        assert mock_log.info.call_count == 1

    async def test_prompt_includes_merge_state(self) -> None:
        """merge_state is _github_will_merge()'s primary mergeability signal (#1109);
        the LLM shadow-evaluation prompt must see it too, not just the stale `mergeable`."""
        invoke_mock = _mock_invoke(_llm_result("integrate", "branch is behind but will auto-merge"))
        cfg = _cfg()

        with _patch_service(cfg, invoke_mock):
            await get_llm_suggestion(**_kwargs(merge_state="BEHIND"))

        prompt_text = invoke_mock.call_args.args[0]
        assert "merge_state: BEHIND" in prompt_text

    async def test_disagrees_false_when_llm_matches_deterministic(self) -> None:
        invoke_mock = _mock_invoke(_llm_result("review_pr"))
        cfg = _cfg()

        with _patch_service(cfg, invoke_mock):
            result = await get_llm_suggestion(**_kwargs(deterministic_action_id="review_pr"))

        assert result is not None
        assert result["disagrees"] is False

    async def test_returns_none_when_config_disabled(self) -> None:
        cfg = _cfg(llm_suggestions=False)
        invoke_mock = _mock_invoke(_llm_result("integrate"))

        with _patch_service(cfg, invoke_mock):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None
        invoke_mock.assert_not_called()

    async def test_returns_none_on_config_load_failure(self) -> None:
        """Fail closed: an unloadable config must not fall through to a call anyway (#924)."""
        invoke_mock = _mock_invoke(_llm_result("integrate"))
        with (
            patch(
                "sova.dashboard.services.llm_suggestion_service.load_config",
                new=MagicMock(side_effect=RuntimeError("config error")),
            ),
            patch("sova.dashboard.services.llm_suggestion_service.llm_invoke", new=invoke_mock),
        ):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None
        invoke_mock.assert_not_called()

    async def test_returns_none_on_provider_error(self) -> None:
        invoke_mock = _mock_invoke(side_effect=RuntimeError("boom"))
        cfg = _cfg()

        with _patch_service(cfg, invoke_mock):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_returns_none_on_invalid_action_id(self) -> None:
        invoke_mock = _mock_invoke(_llm_result("not_a_valid_action"))
        cfg = _cfg()

        with _patch_service(cfg, invoke_mock):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_returns_none_on_malformed_json(self) -> None:
        invoke_mock = _mock_invoke(_raw_result("not json at all"))
        cfg = _cfg()

        with _patch_service(cfg, invoke_mock):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_strips_markdown_code_fences_from_response(self) -> None:
        invoke_mock = _mock_invoke(_raw_result('```json\n{"action_id": "integrate", "reasoning": "ready"}\n```'))
        cfg = _cfg()

        with _patch_service(cfg, invoke_mock):
            result = await get_llm_suggestion(**_kwargs())
        assert result is not None
        assert result["action_id"] == "integrate"

    async def test_caches_result_second_call_skips_provider(self) -> None:
        invoke_mock = _mock_invoke(_llm_result("integrate"))
        cfg = _cfg()

        with _patch_service(cfg, invoke_mock):
            result1 = await get_llm_suggestion(**_kwargs())
            result2 = await get_llm_suggestion(**_kwargs())

        assert result1 == result2
        assert invoke_mock.call_count == 1

    async def test_different_pr_numbers_are_cached_separately(self) -> None:
        invoke_mock = _mock_invoke(side_effect=[_llm_result("integrate"), _llm_result("review_pr")])
        cfg = _cfg()

        with _patch_service(cfg, invoke_mock):
            result_a = await get_llm_suggestion(**_kwargs(pr_number=100))
            result_b = await get_llm_suggestion(**_kwargs(pr_number=200))

        assert result_a["action_id"] == "integrate"
        assert result_b["action_id"] == "review_pr"
        assert invoke_mock.call_count == 2

    async def test_merge_state_change_triggers_new_call(self) -> None:
        """An unreviewed PR can shift from CLEAN to BLOCKED between polls while the
        deterministic/computed state strings stay unchanged; the suggestion must be
        re-evaluated rather than served stale from the cache (CodeRabbit, PR #1114)."""
        invoke_mock = _mock_invoke(side_effect=[_llm_result("integrate"), _llm_result("address_pr")])
        cfg = _cfg()

        with _patch_service(cfg, invoke_mock):
            result_a = await get_llm_suggestion(**_kwargs(merge_state="CLEAN"))
            result_b = await get_llm_suggestion(**_kwargs(merge_state="BLOCKED"))

        assert result_a["action_id"] == "integrate"
        assert result_b["action_id"] == "address_pr"
        assert invoke_mock.call_count == 2

    async def test_mergeable_fallback_change_triggers_new_call(self) -> None:
        """While merge_state stays UNKNOWN (GitHub still recomputing mergeStateStatus),
        mergeable can independently flip from UNKNOWN to MERGEABLE, and the resolver reads
        that fallback value; the suggestion must be re-evaluated, not served stale from the
        cache (CodeRabbit, PR #1114)."""
        invoke_mock = _mock_invoke(side_effect=[_llm_result("integrate"), _llm_result("address_pr")])
        cfg = _cfg()

        with _patch_service(cfg, invoke_mock):
            result_a = await get_llm_suggestion(**_kwargs(merge_state="UNKNOWN", mergeable="UNKNOWN"))
            result_b = await get_llm_suggestion(**_kwargs(merge_state="UNKNOWN", mergeable="MERGEABLE"))

        assert result_a["action_id"] == "integrate"
        assert result_b["action_id"] == "address_pr"
        assert invoke_mock.call_count == 2

    async def test_expired_cache_entry_triggers_new_call(self) -> None:
        import sova.dashboard.services.llm_suggestion_service as mod

        fake_time = [0.0]
        test_cache: TTLCache[str, dict] = TTLCache(maxsize=100, ttl=mod._CACHE_TTL, timer=lambda: fake_time[0])

        invoke_mock = _mock_invoke(side_effect=[_llm_result("integrate"), _llm_result("integrate")])
        cfg = _cfg()

        with _patch_service(cfg, invoke_mock):
            original_cache = mod._cache
            mod._cache = test_cache
            try:
                await get_llm_suggestion(**_kwargs())
                assert len(mod._cache) == 1

                fake_time[0] = mod._CACHE_TTL + 1

                await get_llm_suggestion(**_kwargs())
                assert invoke_mock.call_count == 2
            finally:
                mod._cache = original_cache

    async def test_returns_none_on_prompt_format_error(self) -> None:
        invoke_mock = _mock_invoke(_llm_result("integrate"))
        cfg = _cfg()

        with (
            _patch_service(cfg, invoke_mock),
            patch(
                "sova.dashboard.services.llm_suggestion_service._PROMPT",
                "{missing_key_that_does_not_exist}",
            ),
        ):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None
        invoke_mock.assert_not_called()

    async def test_state_change_invalidates_cache(self) -> None:
        invoke_mock = _mock_invoke(side_effect=[_llm_result("review_pr"), _llm_result("integrate")])
        cfg = _cfg()

        with _patch_service(cfg, invoke_mock):
            result_a = await get_llm_suggestion(**_kwargs(deterministic_state="pr_sova_pending"))
            result_b = await get_llm_suggestion(**_kwargs(deterministic_state="pr_approved"))

        assert result_a["action_id"] == "review_pr"
        assert result_b["action_id"] == "integrate"
        assert invoke_mock.call_count == 2


class TestMakeCacheKey:
    def test_unique_by_pr_number(self) -> None:
        k1 = _make_cache_key(1, "pr_sova_pending", "approved_ci_green", "CLEAN", "MERGEABLE")
        k2 = _make_cache_key(2, "pr_sova_pending", "approved_ci_green", "CLEAN", "MERGEABLE")
        assert k1 != k2

    def test_unique_by_deterministic_state(self) -> None:
        k1 = _make_cache_key(1, "pr_sova_pending", "approved_ci_green", "CLEAN", "MERGEABLE")
        k2 = _make_cache_key(1, "pr_awaiting_review", "approved_ci_green", "CLEAN", "MERGEABLE")
        assert k1 != k2

    def test_unique_by_computed_state(self) -> None:
        k1 = _make_cache_key(1, "pr_sova_pending", "approved_ci_green", "CLEAN", "MERGEABLE")
        k2 = _make_cache_key(1, "pr_sova_pending", "approved", "CLEAN", "MERGEABLE")
        assert k1 != k2

    def test_unique_by_merge_state(self) -> None:
        """An unreviewed PR can shift from CLEAN to BLOCKED while both state strings stay
        unchanged; the cache key must still distinguish the two reads (CodeRabbit, PR #1114)."""
        k1 = _make_cache_key(1, "pr_sova_pending", "approved_ci_green", "CLEAN", "MERGEABLE")
        k2 = _make_cache_key(1, "pr_sova_pending", "approved_ci_green", "BLOCKED", "MERGEABLE")
        assert k1 != k2

    def test_unique_by_mergeable_fallback(self) -> None:
        """While merge_state stays UNKNOWN (GitHub still recomputing mergeStateStatus),
        mergeable can independently flip from UNKNOWN to MERGEABLE, and the resolver reads
        that fallback value; the cache key must distinguish the two reads too (CodeRabbit,
        PR #1114)."""
        k1 = _make_cache_key(1, "pr_sova_pending", "approved_ci_green", "UNKNOWN", "UNKNOWN")
        k2 = _make_cache_key(1, "pr_sova_pending", "approved_ci_green", "UNKNOWN", "MERGEABLE")
        assert k1 != k2

    def test_same_args_same_key(self) -> None:
        k1 = _make_cache_key(42, "pr_approved", "approved_ci_green", "CLEAN", "MERGEABLE")
        k2 = _make_cache_key(42, "pr_approved", "approved_ci_green", "CLEAN", "MERGEABLE")
        assert k1 == k2


class TestPrActionLabels:
    def test_all_pr_actions_have_labels(self) -> None:
        expected = {"review_pr", "address_review", "address_pr", "integrate"}
        assert set(_PR_ACTION_LABELS.keys()) == expected

    def test_labels_are_non_empty_strings(self) -> None:
        for action_id, label in _PR_ACTION_LABELS.items():
            assert isinstance(label, str) and label, f"{action_id} has empty label"


class TestSuggestionPayloadIncludesMergeState:
    """Source-level check: there is no JS test harness in this repo, so the dashboard's
    own suggestion-request payload (built in agents.html, posted to POST /prs/{pr}/suggestion)
    is asserted against its literal source text, matching the pattern used elsewhere for
    JS-only gates (e.g. tests/test_settings_template.py).
    """

    def test_agents_html_sends_merge_state_in_suggestion_payload(self) -> None:
        from pathlib import Path

        path = Path(__file__).parent.parent / "sova" / "dashboard" / "templates" / "agents.html"
        content = path.read_text(encoding="utf-8")
        idx = content.find("deterministic_state: item.state,")
        assert idx != -1, "Suggestion request payload construction not found in agents.html"
        block = content[idx : idx + 600]
        assert "merge_state: prDetails.merge_state" in block, (
            "Suggestion payload must include merge_state so the LLM shadow-evaluation "
            "sees the same primary mergeability signal the deterministic resolver uses (#1109)"
        )
