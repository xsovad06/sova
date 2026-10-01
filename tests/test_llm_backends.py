"""Tests for sova/llm/backends.py: backend detection and tier-alias serviceability.

Covers ``detect_backend`` (llm.provider + the two CLAUDE_CODE_USE_* env vars),
``backend_can_serve`` (fail-open predicate), and ``tier_candidates_for``
(override parsing with built-in fallback).
"""

from __future__ import annotations

import os
from unittest.mock import patch

from sova.config.models import LLMConfig
from sova.llm.backends import (
    TIER_NAMES,
    Backend,
    backend_can_serve,
    detect_backend,
    routing_env_vars_present,
    tier_candidates_for,
    tier_for_known_candidate,
)


class TestDetectBackend:
    def test_claude_code_default_is_firstparty(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert detect_backend(LLMConfig(provider="claude-code")) == Backend.FIRSTPARTY

    def test_anthropic_provider_is_firstparty(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert detect_backend(LLMConfig(provider="anthropic", model="x")) == Backend.FIRSTPARTY

    def test_vertex_provider_is_vertex(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert detect_backend(LLMConfig(provider="vertex", model="x")) == Backend.VERTEX

    def test_litellm_hybrid_openai_ollama_are_permissive(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            for provider in ("litellm", "hybrid", "openai", "ollama"):
                assert detect_backend(LLMConfig(provider=provider, model="x")) == Backend.LITELLM

    def test_claude_code_use_vertex_env_overrides_claude_code(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_USE_VERTEX": "1"}, clear=True):
            assert detect_backend(LLMConfig(provider="claude-code")) == Backend.VERTEX

    def test_claude_code_use_bedrock_env_overrides_claude_code(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_USE_BEDROCK": "1"}, clear=True):
            assert detect_backend(LLMConfig(provider="claude-code")) == Backend.BEDROCK

    def test_falsy_env_values_do_not_trigger_vertex(self) -> None:
        for falsy in ("", "0", "false", "False"):
            with patch.dict(os.environ, {"CLAUDE_CODE_USE_VERTEX": falsy}, clear=True):
                assert detect_backend(LLMConfig(provider="claude-code")) == Backend.FIRSTPARTY

    def test_vertex_env_wins_over_bedrock_env_when_both_set(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_BEDROCK": "1"}, clear=True):
            assert detect_backend(LLMConfig(provider="claude-code")) == Backend.VERTEX

    def test_vertex_env_is_irrelevant_for_non_claude_code_providers(self) -> None:
        """The env vars are CLI-specific; a direct API/LiteLLM provider ignores them."""
        with patch.dict(os.environ, {"CLAUDE_CODE_USE_VERTEX": "1"}, clear=True):
            assert detect_backend(LLMConfig(provider="anthropic", model="x")) == Backend.FIRSTPARTY

    def test_agent_scrubbed_environment_reports_firstparty(self) -> None:
        """A spawned agent whose env was scrubbed of CLAUDE_CODE_USE_VERTEX sees firstParty."""
        from sova.utils.env import scrub_agent_env

        scrubbed = scrub_agent_env({"CLAUDE_CODE_USE_VERTEX": "1", "PATH": "/bin"})
        with patch.dict(os.environ, scrubbed, clear=True):
            assert detect_backend(LLMConfig(provider="claude-code")) == Backend.FIRSTPARTY

    def test_agent_with_passthrough_still_sees_vertex(self) -> None:
        """A project that opts CLAUDE_CODE_USE_VERTEX back in via env_passthrough keeps seeing it."""
        from sova.utils.env import scrub_agent_env

        scrubbed = scrub_agent_env(
            {"CLAUDE_CODE_USE_VERTEX": "1", "PATH": "/bin"}, passthrough=["CLAUDE_CODE_USE_VERTEX"]
        )
        with patch.dict(os.environ, scrubbed, clear=True):
            assert detect_backend(LLMConfig(provider="claude-code")) == Backend.VERTEX

    def test_never_raises_on_a_malformed_cfg(self) -> None:
        class _NotAConfig:
            pass

        assert detect_backend(_NotAConfig()) == Backend.LITELLM  # type: ignore[arg-type]

    def test_explicit_env_param_wins_over_os_environ(self) -> None:
        """A caller resolving a spawned child's model passes that child's own env."""
        with patch.dict(os.environ, {"CLAUDE_CODE_USE_VERTEX": "1"}, clear=True):
            # The raw process env says Vertex, but the explicit (scrubbed)
            # child env says otherwise: the explicit env wins.
            assert detect_backend(LLMConfig(provider="claude-code"), env={}) == Backend.FIRSTPARTY

    def test_explicit_env_param_can_report_vertex_even_when_os_environ_is_clear(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            backend = detect_backend(LLMConfig(provider="claude-code"), env={"CLAUDE_CODE_USE_VERTEX": "1"})
        assert backend == Backend.VERTEX

    def test_empty_env_mapping_is_distinct_from_none(self) -> None:
        """An explicit empty mapping is a real 'nothing set' answer, not 'use os.environ'."""
        with patch.dict(os.environ, {"CLAUDE_CODE_USE_BEDROCK": "1"}, clear=True):
            assert detect_backend(LLMConfig(provider="claude-code"), env={}) == Backend.FIRSTPARTY


class TestRoutingEnvVarsPresent:
    def test_false_when_neither_var_is_set(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert routing_env_vars_present() is False

    def test_true_when_vertex_var_is_truthy(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_USE_VERTEX": "1"}, clear=True):
            assert routing_env_vars_present() is True

    def test_true_when_bedrock_var_is_truthy(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_USE_BEDROCK": "1"}, clear=True):
            assert routing_env_vars_present() is True

    def test_false_for_falsy_values(self) -> None:
        for falsy in ("", "0", "false", "False"):
            env = {"CLAUDE_CODE_USE_VERTEX": falsy, "CLAUDE_CODE_USE_BEDROCK": falsy}
            with patch.dict(os.environ, env, clear=True):
                assert routing_env_vars_present() is False


class TestBackendCanServe:
    def test_bare_tier_name_rejected_on_vertex(self) -> None:
        for tier in TIER_NAMES:
            assert backend_can_serve(Backend.VERTEX, tier) is False

    def test_bare_tier_name_rejected_on_bedrock(self) -> None:
        for tier in TIER_NAMES:
            assert backend_can_serve(Backend.BEDROCK, tier) is False

    def test_bare_tier_name_accepted_on_firstparty(self) -> None:
        for tier in TIER_NAMES:
            assert backend_can_serve(Backend.FIRSTPARTY, tier) is True

    def test_bare_tier_name_accepted_on_litellm(self) -> None:
        for tier in TIER_NAMES:
            assert backend_can_serve(Backend.LITELLM, tier) is True

    def test_unrecognized_id_is_accepted_everywhere_fail_open(self) -> None:
        """A correctly-pinned ID this table has never seen must never be rejected."""
        for backend in Backend:
            assert backend_can_serve(backend, "claude-opus-9-nonexistent") is True

    def test_qualified_vertex_id_is_accepted_on_vertex(self) -> None:
        assert backend_can_serve(Backend.VERTEX, "claude-sonnet-4-5@20250929") is True

    def test_at_pinned_vertex_id_rejected_on_firstparty(self) -> None:
        """Issue #1029: an @-pinned snapshot ID is Vertex's dialect, not Anthropic's direct API."""
        assert backend_can_serve(Backend.FIRSTPARTY, "claude-sonnet-4-5@20250929") is False

    def test_at_pinned_vertex_id_accepted_on_litellm(self) -> None:
        """LiteLLM is the permissive catch-all; it owns its own ID resolution per provider."""
        assert backend_can_serve(Backend.LITELLM, "claude-sonnet-4-5@20250929") is True

    def test_bedrock_style_id_rejected_off_bedrock(self) -> None:
        for backend in (Backend.FIRSTPARTY, Backend.VERTEX, Backend.LITELLM):
            assert backend_can_serve(backend, "us.anthropic.claude-sonnet-4-5-20250929-v1:0") is False
            assert backend_can_serve(backend, "anthropic.claude-sonnet-4-5-20250929-v1:0") is False

    def test_bedrock_style_id_accepted_on_bedrock(self) -> None:
        assert backend_can_serve(Backend.BEDROCK, "us.anthropic.claude-sonnet-4-5-20250929-v1:0") is True


def _reverse_alias(tier: str) -> str:
    """opus/smart and sonnet/fast and haiku/cheap share identical candidate lists."""
    pairs = {"opus": "smart", "smart": "opus", "sonnet": "fast", "fast": "sonnet", "haiku": "cheap", "cheap": "haiku"}
    return pairs[tier]


class TestTierForKnownCandidate:
    def test_known_vertex_candidate_resolves_to_its_tier(self) -> None:
        for tier in TIER_NAMES:
            for candidate in tier_candidates_for(Backend.VERTEX, tier):
                assert tier_for_known_candidate(candidate) in (tier, _reverse_alias(tier))

    def test_known_bedrock_candidate_resolves_to_its_tier(self) -> None:
        for tier in TIER_NAMES:
            for candidate in tier_candidates_for(Backend.BEDROCK, tier):
                assert tier_for_known_candidate(candidate) in (tier, _reverse_alias(tier))

    def test_unknown_id_returns_none(self) -> None:
        assert tier_for_known_candidate("claude-opus-9-nonexistent") is None
        assert tier_for_known_candidate("opus") is None


class TestTierCandidatesFor:
    def test_builtin_vertex_candidates_are_servable(self) -> None:
        for tier in TIER_NAMES:
            candidates = tier_candidates_for(Backend.VERTEX, tier)
            assert candidates
            assert all(backend_can_serve(Backend.VERTEX, c) for c in candidates)

    def test_builtin_bedrock_candidates_are_servable(self) -> None:
        for tier in TIER_NAMES:
            candidates = tier_candidates_for(Backend.BEDROCK, tier)
            assert candidates
            assert all(backend_can_serve(Backend.BEDROCK, c) for c in candidates)

    def test_firstparty_and_litellm_have_no_builtin_candidates(self) -> None:
        """Never consulted in practice since backend_can_serve never rejects a tier name there."""
        for tier in TIER_NAMES:
            assert tier_candidates_for(Backend.FIRSTPARTY, tier) == []
            assert tier_candidates_for(Backend.LITELLM, tier) == []

    def test_override_wins_over_builtin(self) -> None:
        overrides = {"vertex:opus": "custom-opus-id"}
        assert tier_candidates_for(Backend.VERTEX, "opus", overrides) == ["custom-opus-id"]

    def test_override_parses_comma_separated_list(self) -> None:
        overrides = {"vertex:opus": "id-one, id-two , id-three"}
        assert tier_candidates_for(Backend.VERTEX, "opus", overrides) == ["id-one", "id-two", "id-three"]

    def test_empty_override_falls_back_to_builtin(self) -> None:
        overrides = {"vertex:opus": ""}
        assert tier_candidates_for(Backend.VERTEX, "opus", overrides) == tier_candidates_for(Backend.VERTEX, "opus")

    def test_override_with_only_rejected_entries_falls_back_to_builtin(self) -> None:
        """An override of bare tier names (all rejected by backend_can_serve) is not usable."""
        overrides = {"vertex:opus": "opus, smart"}
        assert tier_candidates_for(Backend.VERTEX, "opus", overrides) == tier_candidates_for(Backend.VERTEX, "opus")

    def test_override_for_a_different_backend_is_ignored(self) -> None:
        overrides = {"bedrock:opus": "custom-bedrock-opus"}
        assert tier_candidates_for(Backend.VERTEX, "opus", overrides) == tier_candidates_for(Backend.VERTEX, "opus")

    def test_unknown_tier_and_backend_returns_empty(self) -> None:
        assert tier_candidates_for(Backend.FIRSTPARTY, "opus") == []
