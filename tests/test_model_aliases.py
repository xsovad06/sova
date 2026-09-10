"""Tests for the client-side model alias map (``llm.model_aliases``).

Alias resolution is deliberately client-side: ``normalize_model_name`` is
provider-owned and a no-op on the default claude-code path, so it cannot serve
as the alias layer (docs/model-selection-architecture.md, Q3). These tests cover
``select_model`` itself, its wiring into every client entry point and into the
fallback chain, and the parity guarantee that an empty map changes nothing.

``create_provider(LLMConfig)`` is covered here too: taking the whole config
section is what keeps a newly added field like ``model_aliases`` from silently
no-opping at a call site that forwards a hand-picked kwarg subset
(docs/model-selection-risk-assessment.md, R12).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
from sova.llm import client
from sova.llm.models import LLMResult, StreamEvent

_ALIASES = {"smart": "ollama/llama3.1:70b"}


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset the provider and the process-local availability cache per test."""
    client.reset_provider()
    client.reset_availability_cache()
    yield
    client.reset_provider()
    client.reset_availability_cache()


def _cfg(aliases: dict[str, str] | None = None, *fallbacks: str) -> ProjectConfig:
    """Project config on the LiteLLM provider with *aliases* and a fallback chain."""
    return ProjectConfig(
        llm=LLMConfig(provider="litellm", model="claude-sonnet-4-6", model_aliases=aliases or {}),
        agent=AgentConfig(model="opus", fallback_models=list(fallbacks)),
    )


def _passthrough_provider(*results: LLMResult | Exception) -> MagicMock:
    """Provider mock that records the model it is handed and never aliases it."""
    provider = MagicMock()
    provider.normalize_model_name = lambda m: m
    provider.invoke = AsyncMock(side_effect=list(results))
    provider.invoke_command = AsyncMock(side_effect=list(results))
    return provider


# ---------------------------------------------------------------------------
# select_model()
# ---------------------------------------------------------------------------


class TestSelectModel:
    def test_mapped_name_resolves_to_native_id(self) -> None:
        assert client.select_model("smart", _cfg(_ALIASES)) == "ollama/llama3.1:70b"

    def test_unmapped_name_passes_through(self) -> None:
        assert client.select_model("opus", _cfg(_ALIASES)) == "opus"

    def test_empty_map_is_identity(self) -> None:
        """The default empty map must reproduce today's resolution exactly."""
        for name in ("opus", "sonnet", "haiku", "claude-opus-4-6", "ollama/llama3.1:70b"):
            assert client.select_model(name, _cfg()) == name

    def test_none_model_is_never_aliased(self) -> None:
        """None means 'provider default' and has no name to look up."""
        assert client.select_model(None, _cfg({"": "opus"})) is None

    def test_missing_config_passes_through(self) -> None:
        assert client.select_model("smart", None) == "smart"

    def test_self_mapping_is_a_no_op(self) -> None:
        assert client.select_model("opus", _cfg({"opus": "opus"})) == "opus"

    def test_resolution_is_a_single_hop(self) -> None:
        """An alias whose target is itself a key is not chased, so a cyclic map cannot loop."""
        cfg = _cfg({"a": "b", "b": "a"})
        assert client.select_model("a", cfg) == "b"
        assert client.select_model("b", cfg) == "a"


class TestResolveAlias:
    """The raw-dict helper ``select_model`` and ``create_provider`` both build on."""

    def test_mapped_name_resolves(self) -> None:
        assert client.resolve_alias("smart", _ALIASES) == "ollama/llama3.1:70b"

    def test_unmapped_name_passes_through(self) -> None:
        assert client.resolve_alias("opus", _ALIASES) == "opus"

    def test_self_mapping_is_a_no_op(self) -> None:
        assert client.resolve_alias("opus", {"opus": "opus"}) == "opus"

    def test_falsy_but_not_none_target_is_returned(self) -> None:
        """An alias resolving to '' is a legitimate resolution, not 'unmapped'."""
        assert client.resolve_alias("foo", {"foo": ""}) == ""


# ---------------------------------------------------------------------------
# Wiring into invoke() / invoke_command() / invoke_streaming()
# ---------------------------------------------------------------------------


class TestAliasWiring:
    async def test_invoke_sends_the_aliased_model_to_the_provider(self) -> None:
        provider = _passthrough_provider(LLMResult(text="ok", model="x"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=_cfg(_ALIASES)),
        ):
            await client.invoke("prompt", model="smart", timeout=900.0)
        assert provider.invoke.call_args.kwargs["model"] == "ollama/llama3.1:70b"

    async def test_invoke_with_empty_map_sends_the_original_model(self) -> None:
        provider = _passthrough_provider(LLMResult(text="ok", model="x"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=_cfg()),
        ):
            await client.invoke("prompt", model="smart", timeout=900.0)
        assert provider.invoke.call_args.kwargs["model"] == "smart"

    async def test_invoke_aliases_the_task_type_routed_model(self) -> None:
        """Routing picks the name; the alias map then maps it to a native ID."""
        cfg = _cfg({"fast": "ollama/qwen3-coder"})
        cfg.llm.routing = {"triage": "fast"}
        provider = _passthrough_provider(LLMResult(text="ok", model="x"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=cfg),
        ):
            await client.invoke("prompt", task_type="triage", timeout=900.0)
        assert provider.invoke.call_args.kwargs["model"] == "ollama/qwen3-coder"

    async def test_invoke_command_sends_the_aliased_model(self) -> None:
        provider = _passthrough_provider(LLMResult(text="ok", model="x"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=_cfg(_ALIASES)),
        ):
            await client.invoke_command("/review", "42", model="smart", timeout=900.0)
        assert provider.invoke_command.call_args.kwargs["model"] == "ollama/llama3.1:70b"

    async def test_invoke_streaming_sends_the_aliased_model(self) -> None:
        seen: dict[str, str | None] = {}

        async def _stream(prompt, *, model=None, **kwargs):
            seen["model"] = model
            yield StreamEvent(type="content", text="hi")

        provider = MagicMock()
        provider.normalize_model_name = lambda m: m
        provider.invoke_streaming = _stream
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=_cfg(_ALIASES)),
        ):
            events = [e async for e in client.invoke_streaming("prompt", model="smart")]
        assert seen["model"] == "ollama/llama3.1:70b"
        assert len(events) == 1


class TestAliasedBatch:
    """``invoke_batch`` is the fourth entry point and must alias like the other three."""

    async def _run(self, cfg, requests):
        from sova.llm.models import BatchRequest as _BR

        provider = MagicMock()
        provider.invoke_batch = AsyncMock(return_value=[])
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=cfg),
            patch("sova.llm.providers.anthropic_batch.create_batch_provider", return_value=None),
        ):
            await client.invoke_batch([_BR(custom_id=c, prompt=p, model=m) for c, p, m in requests])
        return provider.invoke_batch.call_args.args[0]

    async def test_request_models_are_aliased(self) -> None:
        """resolve_model() hands callers a tier name; the batch APIs need the native ID."""
        sent = await self._run(_cfg(_ALIASES), [("1", "prompt", "smart")])
        assert [r.model for r in sent] == ["ollama/llama3.1:70b"]

    async def test_empty_model_is_left_alone(self) -> None:
        """An empty model is BatchRequest's provider-default sentinel, not a lookup key."""
        sent = await self._run(_cfg({"": "opus"}), [("1", "prompt", "")])
        assert [r.model for r in sent] == [""]

    async def test_falsy_alias_target_is_not_discarded(self) -> None:
        """A legitimate empty-string alias target must survive, not fall back to req.model.

        Regression test for the ``model or req.model`` falsy-default bug: an alias
        resolving to ``""`` is falsy but not None, so it must still win over the
        original (unaliased) request model.
        """
        sent = await self._run(_cfg({"foo": ""}), [("1", "prompt", "foo")])
        assert [r.model for r in sent] == [""]

    async def test_empty_map_leaves_requests_unchanged(self) -> None:
        """An empty custom map is a no-op; the built-in tier table still expands separately."""
        sent = await self._run(_cfg(), [("1", "prompt", "ollama/qwen3-coder"), ("2", "prompt", "")])
        assert [r.model for r in sent] == ["ollama/qwen3-coder", ""]

    async def test_caller_requests_are_not_mutated(self) -> None:
        from sova.llm.models import BatchRequest as _BR

        original = _BR(custom_id="1", prompt="prompt", model="smart")
        provider = MagicMock()
        provider.invoke_batch = AsyncMock(return_value=[])
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=_cfg(_ALIASES)),
            patch("sova.llm.providers.anthropic_batch.create_batch_provider", return_value=None),
        ):
            await client.invoke_batch([original])
        assert original.model == "smart"

    async def test_config_is_loaded_once_for_the_whole_batch(self) -> None:
        """Loading per request would re-read sova.toml and reopen the DB N times."""
        from sova.llm.models import BatchRequest as _BR

        provider = MagicMock()
        provider.invoke_batch = AsyncMock(return_value=[])
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=_cfg(_ALIASES)) as mock_load,
            patch("sova.llm.providers.anthropic_batch.create_batch_provider", return_value=None),
        ):
            await client.invoke_batch([_BR(custom_id=str(i), prompt="prompt", model="smart") for i in range(5)])
        assert mock_load.call_count == 1


# ---------------------------------------------------------------------------
# Wiring into the fallback chain
# ---------------------------------------------------------------------------


class TestAliasedFallbackChain:
    def test_fallback_candidates_are_aliased(self) -> None:
        """A map that resolved only the primary would leave every hop unmapped."""
        cfg = _cfg({"smart": "ollama/llama3.1:70b", "cheap": "ollama/qwen3-coder"}, "cheap")
        assert client._build_candidate_chain("ollama/llama3.1:70b", cfg) == [
            "ollama/llama3.1:70b",
            "ollama/qwen3-coder",
        ]

    def test_fallback_alias_of_the_primary_is_deduplicated(self) -> None:
        """'smart' and the ID it resolves to are one model, not two chain hops."""
        cfg = _cfg(_ALIASES, "smart", "gpt-4o")
        assert client._build_candidate_chain("ollama/llama3.1:70b", cfg) == ["ollama/llama3.1:70b", "gpt-4o"]

    def test_empty_map_leaves_the_chain_unchanged(self) -> None:
        assert client._build_candidate_chain("opus", _cfg(None, "sonnet", "haiku")) == ["opus", "sonnet", "haiku"]

    async def test_invoke_walks_an_aliased_chain(self) -> None:
        from sova.llm.errors import RateLimitError

        provider = _passthrough_provider(RateLimitError("rate_limit"), LLMResult(text="ok", model="x"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=_cfg(_ALIASES, "cheap")),
        ):
            await client.invoke("prompt", model="smart", timeout=900.0)
        assert [c.kwargs["model"] for c in provider.invoke.call_args_list] == ["ollama/llama3.1:70b", "cheap"]


# ---------------------------------------------------------------------------
# create_provider(LLMConfig) and config registration
# ---------------------------------------------------------------------------


class TestCreateProviderTakesWholeConfig:
    def test_reload_provider_forwards_the_whole_llm_section(self) -> None:
        """Forwarding the object itself is what stops a new field from being dropped."""
        cfg = _cfg(_ALIASES)
        with patch("sova.llm.client.create_provider") as mock_create:
            client.reload_provider(cfg)
        assert mock_create.call_args.args[0] is cfg.llm
        assert mock_create.call_args.args[0].model_aliases == _ALIASES


class TestModelAliasesConfig:
    def test_defaults_to_empty(self) -> None:
        assert LLMConfig().model_aliases == {}

    def test_loaded_from_toml(self, tmp_path) -> None:
        from sova.config.loader import load_config

        (tmp_path / "sova.toml").write_text('[llm]\nmodel_aliases = { smart = "ollama/llama3.1:70b" }\n')
        assert load_config(tmp_path).llm.model_aliases == _ALIASES

    def test_exported_from_the_package(self) -> None:
        """select_model sits alongside its sibling resolvers in the public surface."""
        import sova.llm as llm_pkg

        assert llm_pkg.select_model is client.select_model
        assert "select_model" in llm_pkg.__all__

    def test_registered_in_settings_metadata(self) -> None:
        from sova.dashboard.settings_meta import get_meta

        meta = get_meta("llm.model_aliases")
        assert meta is not None
        assert meta.value_type == "object"
