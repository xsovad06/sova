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

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
from sova.llm import client
from sova.llm.backends import Backend, tier_candidates_for
from sova.llm.models import LLMResult, StreamEvent, resolve_model_alias

_ALIASES = {"smart": "ollama/llama3.1:70b"}


@contextmanager
def _routed_via(var_name: str, value: str = "1"):
    """Simulate a deployment with env-based Vertex/Bedrock routing for the Claude CLI.

    ``resolve_alias()`` detects the backend from the environment a spawned
    Claude CLI child would actually see, not this (test) process's own raw
    ``os.environ``: ``CLAUDE_CODE_USE_VERTEX``/``CLAUDE_CODE_USE_BEDROCK`` are
    stripped from a spawned child unless ``agent.env_passthrough`` opts them
    back in (``sova/utils/env.py``). A deployment that genuinely routes
    through Vertex/Bedrock via these vars must configure that passthrough, so
    tests simulate that configuration rather than only setting the raw var.
    """
    with (
        patch.dict("os.environ", {var_name: value}, clear=True),
        patch("sova.utils.env.configured_passthrough", return_value=(var_name,)),
    ):
        yield


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


def _llm_cfg(aliases: dict[str, str] | None = None, **kwargs) -> LLMConfig:
    """The whole ``llm`` section, on the permissive LiteLLM backend by default."""
    return LLMConfig(provider="litellm", model="claude-sonnet-4-6", model_aliases=aliases or {}, **kwargs)


class TestResolveAlias:
    """The backend-aware helper ``select_model`` and ``create_provider`` both build on."""

    def test_mapped_name_resolves(self) -> None:
        assert client.resolve_alias("smart", _llm_cfg(_ALIASES)) == "ollama/llama3.1:70b"

    def test_unmapped_name_passes_through(self) -> None:
        assert client.resolve_alias("opus", _llm_cfg(_ALIASES)) == "opus"

    def test_self_mapping_is_a_no_op(self) -> None:
        assert client.resolve_alias("opus", _llm_cfg({"opus": "opus"})) == "opus"

    def test_falsy_but_not_none_target_is_returned(self) -> None:
        """An alias resolving to '' is a legitimate resolution, not 'unmapped'."""
        assert client.resolve_alias("foo", _llm_cfg({"foo": ""})) == ""

    def test_scoped_key_wins_over_bare_key(self) -> None:
        cfg = _llm_cfg({"opus": "bare-target", "litellm:opus": "scoped-target"})
        assert client.resolve_alias("opus", cfg) == "scoped-target"

    def test_scoped_key_for_a_different_backend_is_ignored(self) -> None:
        cfg = _llm_cfg({"vertex:opus": "vertex-only-target"})
        assert client.resolve_alias("opus", cfg) == "opus"

    def test_colon_in_alias_name_still_resolves_via_bare_key(self) -> None:
        """A LiteLLM-style 'provider:model' name used as a key is not itself a scoped key."""
        cfg = _llm_cfg({"openai:gpt-4": "openai/gpt-4-turbo"})
        assert client.resolve_alias("openai:gpt-4", cfg) == "openai/gpt-4-turbo"


class TestResolveAliasBackendAware:
    """Tier-name fallthrough when the detected backend can't serve a bare tier name."""

    def test_bare_tier_name_resolves_on_firstparty(self) -> None:
        """SOVA resolves a bare tier name itself rather than leaving it to the CLI (issue #619)."""
        cfg = LLMConfig(provider="claude-code")
        with patch.dict("os.environ", {}, clear=True):
            resolved = client.resolve_alias("opus", cfg)
        assert resolved == resolve_model_alias("opus")
        assert resolved != "opus"

    def test_bare_tier_name_falls_through_to_a_vertex_candidate(self) -> None:
        cfg = LLMConfig(provider="claude-code")
        with _routed_via("CLAUDE_CODE_USE_VERTEX"):
            resolved = client.resolve_alias("opus", cfg)
        assert resolved == tier_candidates_for(Backend.VERTEX, "opus")[0]
        assert resolved != "opus"

    def test_bare_tier_name_falls_through_to_a_bedrock_candidate(self) -> None:
        cfg = LLMConfig(provider="claude-code")
        with _routed_via("CLAUDE_CODE_USE_BEDROCK"):
            resolved = client.resolve_alias("opus", cfg)
        assert resolved == tier_candidates_for(Backend.BEDROCK, "opus")[0]

    def test_explicit_scoped_alias_wins_over_the_builtin_candidate_table(self) -> None:
        cfg = LLMConfig(provider="claude-code", model_aliases={"vertex:opus": "my-custom-opus-id"})
        with _routed_via("CLAUDE_CODE_USE_VERTEX"):
            assert client.resolve_alias("opus", cfg) == "my-custom-opus-id"

    def test_non_tier_name_never_falls_through_even_when_unservable_looking(self) -> None:
        """Fallthrough is scoped to the six tier names; anything else passes through as-is."""
        cfg = LLMConfig(provider="claude-code", model_aliases={"my-custom-tier": "opus"})
        with _routed_via("CLAUDE_CODE_USE_VERTEX"):
            assert client.resolve_alias("my-custom-tier", cfg) == "opus"

    def test_resolve_tier_aliases_false_restores_passthrough(self) -> None:
        cfg = LLMConfig(provider="claude-code", resolve_tier_aliases=False)
        with _routed_via("CLAUDE_CODE_USE_VERTEX"):
            assert client.resolve_alias("opus", cfg) == "opus"

    def test_unresolvable_tier_candidates_pass_through_unchanged(self) -> None:
        """An override with no servable entries, on a tier with no builtin either, is not lost."""
        cfg = LLMConfig(
            provider="claude-code",
            model_aliases={"vertex:sonnet": "sonnet"},
            tier_candidates={"vertex:sonnet": "opus, smart"},
        )
        with _routed_via("CLAUDE_CODE_USE_VERTEX"):
            resolved = client.resolve_alias("sonnet", cfg)
        # All override entries (bare tier names) and no working fallback: the
        # scoped-alias value survives unchanged rather than resolving to None.
        assert resolved == tier_candidates_for(Backend.VERTEX, "sonnet")[0]


class TestResolveAliasScrubbedEnvironment:
    """CodeRabbit (PR #1106): detection must match the spawned CLI child's env.

    ``CLAUDE_CODE_USE_VERTEX``/``CLAUDE_CODE_USE_BEDROCK`` are stripped from a
    spawned Claude CLI child's environment unless ``agent.env_passthrough``
    opts them back in (``sova/utils/env.py:scrub_agent_env``). A raw var set
    in the resolving process's own environment but never passed through must
    not be treated as routing: the child will run firstParty regardless, and
    resolving to an ``@``-pinned Vertex ID would hand that child a model it
    cannot serve.
    """

    def test_env_var_without_passthrough_is_not_treated_as_routed(self) -> None:
        cfg = LLMConfig(provider="claude-code")
        with (
            patch.dict("os.environ", {"CLAUDE_CODE_USE_VERTEX": "1"}, clear=True),
            patch("sova.utils.env.configured_passthrough", return_value=()),
        ):
            resolved = client.resolve_alias("opus", cfg)
        # Resolved as firstParty (SOVA's own tier table), never an @-pinned
        # Vertex candidate the scrubbed child could not serve.
        assert resolved == resolve_model_alias("opus")
        assert "@" not in resolved

    def test_env_var_with_passthrough_is_treated_as_routed(self) -> None:
        cfg = LLMConfig(provider="claude-code")
        with _routed_via("CLAUDE_CODE_USE_VERTEX"):
            resolved = client.resolve_alias("opus", cfg)
        assert resolved == tier_candidates_for(Backend.VERTEX, "opus")[0]

    def test_configured_passthrough_is_not_consulted_when_no_routing_var_is_set(self) -> None:
        """The config-load behind configured_passthrough() is skipped in the common case."""
        cfg = LLMConfig(provider="claude-code")
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("sova.utils.env.configured_passthrough") as mock_passthrough,
        ):
            client.resolve_alias("opus", cfg)
        mock_passthrough.assert_not_called()

    def test_non_claude_code_provider_ignores_the_raw_routing_var(self) -> None:
        """Only the claude-code provider spawns a CLI child whose env can be scrubbed."""
        cfg = LLMConfig(provider="anthropic", model="x")
        with (
            patch.dict("os.environ", {"CLAUDE_CODE_USE_VERTEX": "1"}, clear=True),
            patch("sova.utils.env.configured_passthrough") as mock_passthrough,
        ):
            resolved = client.resolve_alias("opus", cfg)
        mock_passthrough.assert_not_called()
        assert resolved == resolve_model_alias("opus")


class TestResolveAliasVertexLiteLLMPrefix:
    """CodeRabbit (PR #1106): the ``vertex`` provider routes through LiteLLM.

    ``LiteLLMProvider`` forwards the model ID to litellm unchanged, and
    litellm's Vertex AI dialect requires a ``vertex_ai/`` prefix (see
    ``_VENDOR_MODEL_EXAMPLES`` in ``sova/config/models.py``). The bare
    candidate table must be prefixed only for ``cfg.provider == "vertex"``:
    ``Backend.VERTEX`` also covers the claude-code CLI's own env-based Vertex
    routing, which expects a bare, unprefixed ID.
    """

    def test_vertex_provider_candidate_gets_vertex_ai_prefix(self) -> None:
        cfg = LLMConfig(provider="vertex", model="x")
        resolved = client.resolve_alias("opus", cfg)
        candidate = tier_candidates_for(Backend.VERTEX, "opus")[0]
        assert resolved == f"vertex_ai/{candidate}"

    def test_claude_code_env_routed_candidate_has_no_prefix(self) -> None:
        cfg = LLMConfig(provider="claude-code")
        with _routed_via("CLAUDE_CODE_USE_VERTEX"):
            resolved = client.resolve_alias("opus", cfg)
        assert resolved == tier_candidates_for(Backend.VERTEX, "opus")[0]
        assert not resolved.startswith("vertex_ai/")

    def test_already_prefixed_override_is_not_double_prefixed(self) -> None:
        cfg = LLMConfig(
            provider="vertex",
            model="x",
            tier_candidates={"vertex:opus": "vertex_ai/custom-opus"},
        )
        assert client.resolve_alias("opus", cfg) == "vertex_ai/custom-opus"


class TestResolveAliasPinnedIdDrift:
    """Issue #1029: a pinned ID reached directly (not via a tier alias) whose backend drifted."""

    def test_vertex_pinned_id_used_directly_corrects_on_firstparty(self) -> None:
        """agent.model pinned to a Vertex snapshot ID, deployment since moved off Vertex."""
        pinned = tier_candidates_for(Backend.VERTEX, "sonnet")[0]
        cfg = LLMConfig(provider="claude-code")
        with patch.dict("os.environ", {}, clear=True):
            resolved = client.resolve_alias(pinned, cfg)
        assert resolved == resolve_model_alias("sonnet")
        assert resolved != pinned

    def test_vertex_pinned_id_used_directly_is_unchanged_on_vertex(self) -> None:
        """The same pinned ID, still on Vertex, is already correct and must not be touched."""
        pinned = tier_candidates_for(Backend.VERTEX, "sonnet")[0]
        cfg = LLMConfig(provider="claude-code")
        with _routed_via("CLAUDE_CODE_USE_VERTEX"):
            assert client.resolve_alias(pinned, cfg) == pinned

    def test_vertex_ai_prefixed_pinned_id_corrects_on_firstparty(self) -> None:
        """A litellm-prefixed ID (pinned while cfg.provider == "vertex") also drifts off vertex."""
        pinned = f"vertex_ai/{tier_candidates_for(Backend.VERTEX, 'sonnet')[0]}"
        cfg = LLMConfig(provider="claude-code")
        with patch.dict("os.environ", {}, clear=True):
            resolved = client.resolve_alias(pinned, cfg)
        assert resolved == resolve_model_alias("sonnet")
        assert resolved != pinned

    def test_bedrock_pinned_id_used_directly_corrects_on_firstparty(self) -> None:
        pinned = tier_candidates_for(Backend.BEDROCK, "haiku")[0]
        cfg = LLMConfig(provider="claude-code")
        with patch.dict("os.environ", {}, clear=True):
            resolved = client.resolve_alias(pinned, cfg)
        assert resolved == resolve_model_alias("haiku")

    def test_bedrock_pinned_id_used_directly_corrects_on_vertex(self) -> None:
        """A Bedrock-dialect ID reached while actually running on Vertex is also corrected."""
        bedrock_pinned = tier_candidates_for(Backend.BEDROCK, "haiku")[0]
        cfg = LLMConfig(provider="claude-code")
        with _routed_via("CLAUDE_CODE_USE_VERTEX"):
            resolved = client.resolve_alias(bedrock_pinned, cfg)
        assert resolved == tier_candidates_for(Backend.VERTEX, "haiku")[0]


class TestResolveAliasReasonLogging:
    """The ``reason`` field on the ``llm.model_alias`` log line."""

    def test_explicit_bare_alias_logs_explicit(self) -> None:
        cfg = LLMConfig(provider="litellm", model_aliases={"smart": "ollama/llama3.1:70b"})
        with patch.object(client.log, "info") as mock_log:
            client.resolve_alias("smart", cfg)
        mock_log.assert_called_once()
        assert mock_log.call_args.kwargs["reason"] == "explicit"

    def test_backend_scoped_alias_logs_backend_scoped(self) -> None:
        cfg = LLMConfig(provider="claude-code", model_aliases={"vertex:opus": "my-custom-opus-id"})
        with patch.object(client.log, "info") as mock_log, _routed_via("CLAUDE_CODE_USE_VERTEX"):
            client.resolve_alias("opus", cfg)
        mock_log.assert_called_once()
        assert mock_log.call_args.kwargs["reason"] == "backend_scoped"

    def test_tier_fallthrough_logs_tier_candidate(self) -> None:
        cfg = LLMConfig(provider="claude-code")
        with (
            patch.object(client.log, "info") as mock_log,
            _routed_via("CLAUDE_CODE_USE_VERTEX"),
        ):
            client.resolve_alias("opus", cfg)
        mock_log.assert_called_once()
        assert mock_log.call_args.kwargs["reason"] == "tier_candidate"

    def test_unmapped_passthrough_is_never_logged(self) -> None:
        cfg = LLMConfig(provider="litellm")
        with patch.object(client.log, "info") as mock_log:
            client.resolve_alias("my-custom-model", cfg)
        mock_log.assert_not_called()


class TestTierCandidatesOverrideJsonArray:
    def test_json_array_override_is_parsed(self) -> None:
        cfg = LLMConfig(
            provider="claude-code",
            tier_candidates={"vertex:opus": '["custom-opus-a", "custom-opus-b"]'},
        )
        with _routed_via("CLAUDE_CODE_USE_VERTEX"):
            assert client.resolve_alias("opus", cfg) == "custom-opus-a"


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
