"""Tests for model fallback layer 2: the client-owned fallback loop.

Layer 1 (passing ``--fallback-model`` to the Claude CLI) is covered by
tests/test_model_fallback_cli.py. This file covers the SOVA-owned chain walk in
sova/llm/client.py: chain construction, the shared deadline, the process-local
availability cache, and the wiring into invoke()/invoke_command().
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
from sova.llm import client
from sova.llm.errors import (
    BillingError,
    LLMInvocationError,
    ModelUnavailableError,
    ProviderUnavailableError,
    RateLimitError,
)
from sova.llm.models import LLMResult


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset the provider and the process-local availability cache per test."""
    client.reset_provider()
    client.reset_availability_cache()
    yield
    client.reset_provider()
    client.reset_availability_cache()


def _cfg(*fallbacks: str) -> ProjectConfig:
    return ProjectConfig(agent=AgentConfig(model="opus", fallback_models=list(fallbacks)))


def _ok(model: str = "opus") -> LLMResult:
    return LLMResult(text="ok", model=model, cost_usd=Decimal("0.01"))


# _cfg() always uses the default LLMConfig (provider="claude-code", api_base=""),
# so this is the availability cache identity every test in this module observes.
_IDENTITY = "claude-code:"


class _Recorder:
    """Attempt callable that records (model, next_hop, timeout, budget) per call."""

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str | None, str | None, float, Decimal | None]] = []

    async def __call__(
        self, model: str | None, next_hop: str | None, timeout: float, budget: Decimal | None
    ) -> LLMResult:
        self.calls.append((model, next_hop, timeout, budget))
        outcome = self._outcomes[len(self.calls) - 1] if len(self.calls) <= len(self._outcomes) else _ok()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# ModelAvailabilityCache
# ---------------------------------------------------------------------------


class TestModelAvailabilityCache:
    def test_mark_then_query_reports_unavailable(self) -> None:
        cache = client.ModelAvailabilityCache()
        cache.mark_unavailable(_IDENTITY, "opus")
        assert cache.is_unavailable(_IDENTITY, "opus")

    def test_unmarked_model_is_available(self) -> None:
        assert not client.ModelAvailabilityCache().is_unavailable(_IDENTITY, "sonnet")

    def test_none_is_never_marked_or_reported(self) -> None:
        """A None model means 'provider default' and has no cache identity."""
        cache = client.ModelAvailabilityCache()
        cache.mark_unavailable(_IDENTITY, None)
        assert not cache.is_unavailable(_IDENTITY, None)

    def test_entry_expires_after_ttl(self) -> None:
        cache = client.ModelAvailabilityCache(ttl_seconds=0.0)
        cache.mark_unavailable(_IDENTITY, "opus")
        assert not cache.is_unavailable(_IDENTITY, "opus")

    def test_reset_clears_entries(self) -> None:
        cache = client.ModelAvailabilityCache()
        cache.mark_unavailable(_IDENTITY, "opus")
        cache.reset()
        assert not cache.is_unavailable(_IDENTITY, "opus")

    def test_module_singleton_reset_hook(self) -> None:
        client.get_availability_cache().mark_unavailable(_IDENTITY, "opus")
        client.reset_availability_cache()
        assert not client.get_availability_cache().is_unavailable(_IDENTITY, "opus")

    def test_different_identity_is_isolated(self) -> None:
        """A model unavailable on one provider/deployment must not poison another's lookup."""
        cache = client.ModelAvailabilityCache()
        cache.mark_unavailable("vertex:project-a", "opus")
        assert cache.is_unavailable("vertex:project-a", "opus")
        assert not cache.is_unavailable("vertex:project-b", "opus")


# ---------------------------------------------------------------------------
# _build_candidate_chain()
# ---------------------------------------------------------------------------


class TestBuildCandidateChain:
    def test_empty_fallback_models_yields_single_candidate(self) -> None:
        assert client._build_candidate_chain("opus", _cfg()) == ["opus"]

    def test_missing_config_yields_single_candidate(self) -> None:
        assert client._build_candidate_chain("opus", None) == ["opus"]

    def test_primary_then_fallbacks_in_order(self) -> None:
        assert client._build_candidate_chain("opus", _cfg("sonnet", "haiku")) == ["opus", "sonnet", "haiku"]

    def test_primary_repeated_in_fallbacks_is_dropped(self) -> None:
        assert client._build_candidate_chain("opus", _cfg("opus", "sonnet")) == ["opus", "sonnet"]

    def test_duplicate_fallbacks_are_dropped(self) -> None:
        assert client._build_candidate_chain("opus", _cfg("sonnet", "sonnet")) == ["opus", "sonnet"]

    def test_aliases_deduplicate_against_resolved_ids(self) -> None:
        """claude-code maps smart->opus, so 'smart' must not repeat the primary."""
        assert client._build_candidate_chain("opus", _cfg("smart", "sonnet")) == ["opus", "sonnet"]

    def test_none_primary_is_kept_and_not_deduplicated(self) -> None:
        """None means 'provider default' and never collides with a named model."""
        assert client._build_candidate_chain(None, _cfg("sonnet")) == [None, "sonnet"]

    def test_empty_string_fallbacks_are_skipped(self) -> None:
        assert client._build_candidate_chain("opus", _cfg("", "sonnet")) == ["opus", "sonnet"]


# ---------------------------------------------------------------------------
# _invoke_with_fallback()
# ---------------------------------------------------------------------------


class TestInvokeWithFallbackLoop:
    async def test_single_candidate_passes_timeout_through_unchanged(self) -> None:
        """The default (no fallbacks) path must be indistinguishable from today."""
        attempt = _Recorder(_ok())
        result = await client._invoke_with_fallback(
            attempt, primary="opus", cfg=_cfg(), caller_fallback=None, timeout=900.0
        )
        assert result.text == "ok"
        assert attempt.calls == [("opus", None, 900.0, None)]

    async def test_single_candidate_forwards_caller_fallback(self) -> None:
        """With no chain hop to derive, the legacy shim value passes through."""
        attempt = _Recorder(_ok())
        await client._invoke_with_fallback(attempt, primary="opus", cfg=_cfg(), caller_fallback="sonnet", timeout=900.0)
        assert attempt.calls[0][1] == "sonnet"

    async def test_eligible_error_advances_to_next_candidate(self) -> None:
        attempt = _Recorder(RateLimitError("rate_limit"), _ok("sonnet"))
        result = await client._invoke_with_fallback(
            attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=900.0
        )
        assert result.model == "sonnet"
        assert [c[0] for c in attempt.calls] == ["opus", "sonnet"]

    async def test_chain_hop_overrides_caller_fallback(self) -> None:
        """SOVA is the source of truth: the CLI gets the chain's next hop."""
        attempt = _Recorder(_ok())
        await client._invoke_with_fallback(
            attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback="haiku", timeout=900.0
        )
        assert attempt.calls[0][1] == "sonnet"

    async def test_last_candidate_gets_no_next_hop(self) -> None:
        attempt = _Recorder(RateLimitError("rate_limit"), _ok("sonnet"))
        await client._invoke_with_fallback(
            attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=900.0
        )
        assert attempt.calls[1][1] is None

    @pytest.mark.parametrize("exc", [BillingError("budget_exhausted"), LLMInvocationError("boom"), ValueError("x")])
    async def test_non_eligible_error_reraises_without_advancing(self, exc: Exception) -> None:
        attempt = _Recorder(exc, _ok("sonnet"))
        with pytest.raises(type(exc)):
            await client._invoke_with_fallback(
                attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=900.0
            )
        assert len(attempt.calls) == 1

    async def test_non_eligible_error_writes_no_cache_entry(self) -> None:
        attempt = _Recorder(BillingError("budget_exhausted"))
        with pytest.raises(BillingError):
            await client._invoke_with_fallback(
                attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=900.0
            )
        assert not client.get_availability_cache().is_unavailable(_IDENTITY, "opus")

    async def test_exhausted_chain_raises_last_error(self) -> None:
        attempt = _Recorder(RateLimitError("first"), ProviderUnavailableError("last"))
        with pytest.raises(ProviderUnavailableError, match="last"):
            await client._invoke_with_fallback(
                attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=900.0
            )
        assert len(attempt.calls) == 2

    async def test_model_unavailable_is_recorded_in_cache(self) -> None:
        attempt = _Recorder(ModelUnavailableError("opus is not available"), _ok("sonnet"))
        await client._invoke_with_fallback(
            attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=900.0
        )
        assert client.get_availability_cache().is_unavailable(_IDENTITY, "opus")

    async def test_cached_unavailable_candidate_is_skipped(self) -> None:
        client.get_availability_cache().mark_unavailable(_IDENTITY, "opus")
        attempt = _Recorder(_ok("sonnet"))
        await client._invoke_with_fallback(
            attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=900.0
        )
        assert [c[0] for c in attempt.calls] == ["sonnet"]

    async def test_all_candidates_cached_unavailable_keeps_last(self) -> None:
        """The cache is an optimization, never a hard gate."""
        for model in ("opus", "sonnet"):
            client.get_availability_cache().mark_unavailable(_IDENTITY, model)
        attempt = _Recorder(_ok("sonnet"))
        await client._invoke_with_fallback(
            attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=900.0
        )
        assert [c[0] for c in attempt.calls] == ["sonnet"]

    async def test_attempts_share_one_deadline_below_caller_timeout(self) -> None:
        """Two candidates split the budget so attempt #2 is never guillotined."""
        attempt = _Recorder(RateLimitError("rate_limit"), _ok("sonnet"))
        await client._invoke_with_fallback(
            attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=1000.0
        )
        first, second = attempt.calls[0][2], attempt.calls[1][2]
        # Attempt #1 is capped at an equal share so it cannot eat the budget.
        assert first == pytest.approx(475.0, abs=5.0)  # 1000 * 0.95 / 2
        # It failed fast, so the last candidate inherits everything still left.
        assert second == pytest.approx(950.0, abs=5.0)
        # Either way the walk ends inside the caller's timeout.
        assert second < 1000.0

    async def test_hanging_first_attempt_cannot_consume_whole_budget(self) -> None:
        """The equal split is what stops attempt #1 from guillotining attempt #2."""
        attempt = _Recorder(RateLimitError("rate_limit"), _ok("haiku"))
        await client._invoke_with_fallback(
            attempt, primary="opus", cfg=_cfg("sonnet", "haiku"), caller_fallback=None, timeout=900.0
        )
        assert attempt.calls[0][2] == pytest.approx(285.0, abs=5.0)  # 900 * 0.95 / 3

    async def test_attempt_slice_floored_at_minimum(self) -> None:
        """An equal split below the floor is raised to the minimum viable slice."""
        attempt = _Recorder(RateLimitError("rate_limit"), _ok("sonnet"))
        await client._invoke_with_fallback(
            attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=100.0
        )
        assert attempt.calls[0][2] == pytest.approx(client._MIN_ATTEMPT_SECONDS, abs=1.0)

    async def test_budget_below_minimum_stops_walk_and_raises_last_error(self) -> None:
        attempt = _Recorder(RateLimitError("rate_limit"), _ok("sonnet"))
        with pytest.raises(RateLimitError):
            await client._invoke_with_fallback(
                attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=1.0
            )
        assert len(attempt.calls) == 1

    async def test_single_candidate_forwards_budget_unchanged(self) -> None:
        """No fallbacks configured: max_budget_usd passes through untouched."""
        attempt = _Recorder(_ok())
        await client._invoke_with_fallback(
            attempt,
            primary="opus",
            cfg=_cfg(),
            caller_fallback=None,
            timeout=900.0,
            max_budget_usd=Decimal("5"),
        )
        assert attempt.calls[0][3] == Decimal("5")

    async def test_budget_is_divided_across_the_chain_not_repeated(self) -> None:
        """Each candidate gets a share of the ceiling, not a fresh copy of it."""
        attempt = _Recorder(RateLimitError("rate_limit"), _ok("sonnet"))
        await client._invoke_with_fallback(
            attempt,
            primary="opus",
            cfg=_cfg("sonnet"),
            caller_fallback=None,
            timeout=900.0,
            max_budget_usd=Decimal("10"),
        )
        first_budget, second_budget = attempt.calls[0][3], attempt.calls[1][3]
        assert first_budget == Decimal("5")
        assert second_budget == Decimal("5")
        assert first_budget + second_budget == Decimal("10")

    async def test_three_way_chain_divides_budget_equally(self) -> None:
        attempt = _Recorder(RateLimitError("first"), RateLimitError("second"), _ok("haiku"))
        await client._invoke_with_fallback(
            attempt,
            primary="opus",
            cfg=_cfg("sonnet", "haiku"),
            caller_fallback=None,
            timeout=900.0,
            max_budget_usd=Decimal("9"),
        )
        budgets = [c[3] for c in attempt.calls]
        assert budgets == [Decimal("3"), Decimal("3"), Decimal("3")]

    async def test_no_budget_cap_means_no_budget_cap_on_any_attempt(self) -> None:
        attempt = _Recorder(RateLimitError("rate_limit"), _ok("sonnet"))
        await client._invoke_with_fallback(
            attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=900.0
        )
        assert [c[3] for c in attempt.calls] == [None, None]


class TestUntypedProviderErrors:
    """Every provider still raises bare RuntimeError (sova/llm/providers/*).

    If the loop only advanced on typed errors it would never advance in
    production, while llm.engine_owned_fallback=False disables the engine walk
    too: fallback would be dead end to end.
    """

    @pytest.mark.parametrize(
        "message",
        [
            "Claude CLI failed (exit 1): claude-opus-5 is not available on your vertex deployment",
            "Claude CLI failed (exit 1): HTTP 429 rate_limit_error",
            "Claude CLI failed (exit 1): connection refused",
        ],
    )
    async def test_untyped_recoverable_error_advances_chain(self, message: str) -> None:
        attempt = _Recorder(RuntimeError(message), _ok("sonnet"))
        result = await client._invoke_with_fallback(
            attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=900.0
        )
        assert result.model == "sonnet"
        assert [c[0] for c in attempt.calls] == ["opus", "sonnet"]

    async def test_untyped_billing_error_does_not_advance(self) -> None:
        attempt = _Recorder(RuntimeError("Claude CLI failed (exit 1): budget_exhausted"), _ok("sonnet"))
        with pytest.raises(RuntimeError, match="budget_exhausted"):
            await client._invoke_with_fallback(
                attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=900.0
            )
        assert len(attempt.calls) == 1

    async def test_untyped_empty_output_error_does_not_advance(self) -> None:
        """An empty-output failure is uncategorized: a retry on another model gains nothing."""
        attempt = _Recorder(RuntimeError("Claude CLI succeeded but produced no output"), _ok("sonnet"))
        with pytest.raises(RuntimeError, match="no output"):
            await client._invoke_with_fallback(
                attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=900.0
            )
        assert len(attempt.calls) == 1

    async def test_untyped_unavailable_error_populates_cache(self) -> None:
        attempt = _Recorder(RuntimeError("claude-opus-5 is not available"), _ok("sonnet"))
        await client._invoke_with_fallback(
            attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=900.0
        )
        assert client.get_availability_cache().is_unavailable(_IDENTITY, "opus")

    async def test_untyped_rate_limit_error_leaves_cache_clean(self) -> None:
        """Only an unavailable model is cached: a throttled one is still enabled."""
        attempt = _Recorder(RuntimeError("HTTP 429 rate_limit"), _ok("sonnet"))
        await client._invoke_with_fallback(
            attempt, primary="opus", cfg=_cfg("sonnet"), caller_fallback=None, timeout=900.0
        )
        assert not client.get_availability_cache().is_unavailable(_IDENTITY, "opus")


# ---------------------------------------------------------------------------
# Wiring into invoke() / invoke_command()
# ---------------------------------------------------------------------------


class TestInvokeFallbackWiring:
    async def test_invoke_walks_configured_chain(self) -> None:
        provider = MagicMock()
        provider.normalize_model_name = lambda m: m
        provider.invoke = AsyncMock(side_effect=[RateLimitError("rate_limit"), _ok("sonnet")])
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=_cfg("sonnet")),
        ):
            result = await client.invoke("prompt", model="opus", timeout=900.0)
        assert result.model == "sonnet"
        assert [c.kwargs["model"] for c in provider.invoke.call_args_list] == ["opus", "sonnet"]

    async def test_invoke_records_compression_savings_once_on_winner(self) -> None:
        provider = MagicMock()
        provider.normalize_model_name = lambda m: m
        winner = LLMResult(text="ok", model="sonnet", input_tokens=1000)
        provider.invoke = AsyncMock(side_effect=[RateLimitError("rate_limit"), winner])
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=_cfg("sonnet")),
            patch.object(client, "maybe_compress", return_value="y" * 100),
        ):
            result = await client.invoke("x" * 400, model="opus", timeout=900.0)
        assert result is winner
        assert result.tokens_saved == 75
        assert result.pre_compression_input_tokens == 1075

    async def test_invoke_command_walks_configured_chain(self) -> None:
        provider = MagicMock()
        provider.normalize_model_name = lambda m: m
        provider.invoke_command = AsyncMock(side_effect=[RateLimitError("rate_limit"), _ok("sonnet")])
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=_cfg("sonnet")),
        ):
            result = await client.invoke_command("/develop", args="42", model="opus", timeout=900.0)
        assert result.model == "sonnet"
        assert [c.kwargs["model"] for c in provider.invoke_command.call_args_list] == ["opus", "sonnet"]

    async def test_config_load_failure_degrades_to_single_candidate(self) -> None:
        provider = MagicMock()
        provider.normalize_model_name = lambda m: m
        provider.invoke = AsyncMock(side_effect=RateLimitError("rate_limit"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=None),
        ):
            with pytest.raises(RateLimitError):
                await client.invoke("prompt", model="opus", timeout=900.0)
        assert provider.invoke.await_count == 1

    async def test_invoke_command_config_load_failure_degrades_to_single_candidate(self) -> None:
        """invoke_command() independently calls _try_load_config and has its own
        outer asyncio.timeout wrapper, so it needs its own regression test mirroring
        invoke()'s: a failed config load must not block the single-candidate call."""
        provider = MagicMock()
        provider.normalize_model_name = lambda m: m
        provider.invoke_command = AsyncMock(side_effect=RateLimitError("rate_limit"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=None),
        ):
            with pytest.raises(RateLimitError):
                await client.invoke_command("/develop", args="42", model="opus", timeout=900.0)
        assert provider.invoke_command.await_count == 1

    async def test_invoke_budget_is_divided_across_provider_calls(self) -> None:
        provider = MagicMock()
        provider.normalize_model_name = lambda m: m
        provider.invoke = AsyncMock(side_effect=[RateLimitError("rate_limit"), _ok("sonnet")])
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=_cfg("sonnet")),
        ):
            await client.invoke("prompt", model="opus", timeout=900.0, max_budget_usd=Decimal("10"))
        budgets = [c.kwargs["max_budget_usd"] for c in provider.invoke.call_args_list]
        assert budgets == [Decimal("5"), Decimal("5")]

    async def test_exit_one_with_valid_json_does_not_trigger_fallback(self) -> None:
        """The provider's partial-success guard returns, so the loop never sees an error."""
        import json

        from sova.utils.shell import ShellResult

        payload = json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": "partial ok",
                "duration_ms": 10,
                "session_id": "s",
                "total_cost_usd": 0.01,
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "modelUsage": {},
            }
        )
        with (
            patch("sova.llm.providers.claude_code.run", new_callable=AsyncMock) as mock_run,
            patch.object(client, "_try_load_config", return_value=_cfg("sonnet")),
        ):
            mock_run.return_value = ShellResult(returncode=1, stdout=payload, stderr="")
            result = await client.invoke("prompt", model="opus", timeout=900.0)
        assert result.text == "partial ok"
        assert mock_run.await_count == 1


# ---------------------------------------------------------------------------
# llm.engine_owned_fallback
# ---------------------------------------------------------------------------


class TestEngineOwnedFallbackFlag:
    def test_defaults_to_off(self) -> None:
        assert LLMConfig().engine_owned_fallback is False

    def test_loaded_from_toml(self, tmp_path) -> None:
        from sova.config.loader import load_config

        (tmp_path / "sova.toml").write_text("[llm]\nengine_owned_fallback = true\n")
        assert load_config(tmp_path).llm.engine_owned_fallback is True

    def test_registered_in_settings_metadata(self) -> None:
        from sova.dashboard.settings_meta import get_meta

        assert get_meta("llm.engine_owned_fallback") is not None
