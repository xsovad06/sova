"""Tests for sova.utils.env: spawn-boundary environment sanitization."""

from __future__ import annotations

from unittest.mock import patch

from sova.utils.env import (
    PARENT_SESSION_VARS,
    PROVIDER_ROUTING_VARS,
    SCRUBBED_VARS,
    scrub_agent_env,
)


class TestScrubAgentEnv:
    def test_removes_vertex_routing_vars(self) -> None:
        """The reported failure mode: inherited Vertex vars override the provider."""
        env = {
            "PATH": "/usr/bin",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_VERTEX_PROJECT_ID": "itpc-ca-1e0b0000a3",
            "CLOUD_ML_REGION": "global",
        }
        result = scrub_agent_env(env)
        assert result == {"PATH": "/usr/bin"}

    def test_removes_bedrock_routing_vars(self) -> None:
        env = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_BEARER_TOKEN_BEDROCK": "tok", "HOME": "/home/x"}
        assert scrub_agent_env(env) == {"HOME": "/home/x"}

    def test_removes_gateway_and_auth_skip_vars(self) -> None:
        """These can redirect Claude Code to an arbitrary gateway or skip provider auth."""
        env = {
            "ANTHROPIC_BASE_URL": "https://evil.example.com",
            "ANTHROPIC_VERTEX_BASE_URL": "https://evil.example.com",
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
            "CLAUDE_CODE_SKIP_VERTEX_AUTH": "1",
            "HOME": "/home/x",
        }
        assert scrub_agent_env(env) == {"HOME": "/home/x"}

    def test_removes_model_pinning_vars(self) -> None:
        """SOVA owns model selection through llm.routing; inherited pins override it."""
        env = {"ANTHROPIC_MODEL": "claude-3-opus", "ANTHROPIC_SMALL_FAST_MODEL": "claude-3-haiku"}
        assert scrub_agent_env(env) == {}

    def test_removes_parent_session_vars(self) -> None:
        """Starting the server inside a Claude Code session must not leak that session."""
        env = {
            "CLAUDECODE": "1",
            "CLAUDE_CODE_SESSION_ID": "abc-123",
            "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
            "LANG": "en_US.UTF-8",
        }
        assert scrub_agent_env(env) == {"LANG": "en_US.UTF-8"}

    def test_preserves_anthropic_api_key(self) -> None:
        """The anthropic provider reads this from the environment; scrubbing breaks it."""
        env = {"ANTHROPIC_API_KEY": "sk-ant-xxx", "CLAUDE_CODE_USE_VERTEX": "1"}
        assert scrub_agent_env(env) == {"ANTHROPIC_API_KEY": "sk-ant-xxx"}

    def test_passthrough_preserves_named_vars(self) -> None:
        """Deployments deliberately on Vertex opt back in via agent.env_passthrough."""
        env = {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "proj"}
        result = scrub_agent_env(env, passthrough=["CLAUDE_CODE_USE_VERTEX"])
        assert result == {"CLAUDE_CODE_USE_VERTEX": "1"}

    def test_passthrough_ignores_blank_entries(self) -> None:
        env = {"CLAUDE_CODE_USE_VERTEX": "1", "PATH": "/bin"}
        assert scrub_agent_env(env, passthrough=["", "  ", None]) == {"PATH": "/bin"}  # type: ignore[list-item]

    def test_passthrough_tolerates_surrounding_whitespace(self) -> None:
        env = {"CLOUD_ML_REGION": "global"}
        assert scrub_agent_env(env, passthrough=["  CLOUD_ML_REGION  "]) == {"CLOUD_ML_REGION": "global"}

    def test_defaults_to_process_environment(self) -> None:
        with patch.dict("os.environ", {"CLAUDE_CODE_USE_VERTEX": "1"}, clear=False):
            assert "CLAUDE_CODE_USE_VERTEX" not in scrub_agent_env()

    def test_returns_a_copy(self) -> None:
        env = {"PATH": "/bin"}
        result = scrub_agent_env(env)
        result["INJECTED"] = "1"
        assert "INJECTED" not in env

    def test_empty_env_is_handled(self) -> None:
        assert scrub_agent_env({}) == {}

    def test_never_logs_values(self) -> None:
        """Scrubbed values may be credentials; only names are safe to log."""
        env = {"AWS_BEARER_TOKEN_BEDROCK": "super-secret-token"}
        with patch("sova.utils.env.log") as mock_log:
            scrub_agent_env(env)
            _, kwargs = mock_log.info.call_args
        assert "super-secret-token" not in repr(kwargs)
        assert kwargs["removed"] == ["AWS_BEARER_TOKEN_BEDROCK"]

    def test_no_log_when_nothing_scrubbed(self) -> None:
        with patch("sova.utils.env.log") as mock_log:
            scrub_agent_env({"PATH": "/bin"})
        mock_log.info.assert_not_called()


class TestScrubbedVarSets:
    def test_scrubbed_is_the_union(self) -> None:
        assert SCRUBBED_VARS == PROVIDER_ROUTING_VARS | PARENT_SESSION_VARS

    def test_var_sets_are_disjoint(self) -> None:
        assert not (PROVIDER_ROUTING_VARS & PARENT_SESSION_VARS)

    def test_credential_vars_are_not_scrubbed(self) -> None:
        """Guards the deliberate carve-out documented in sova/utils/env.py."""
        assert not ({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"} & SCRUBBED_VARS)
