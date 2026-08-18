"""Tests for model fallback layer 1: passing --fallback-model to Claude CLI."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sova.llm.client import invoke, invoke_command
from sova.llm.models import LLMResult
from sova.llm.provider import LLMProvider
from sova.llm.providers.claude_code import ClaudeCodeProvider


class TestFallbackModelPassthrough:
    """Test that fallback_model parameter flows through the invoke chain."""

    async def test_invoke_passes_fallback_to_provider(self) -> None:
        """The client.invoke() should pass fallback_model to the provider."""
        provider = AsyncMock(spec=LLMProvider)
        provider.invoke.return_value = LLMResult(text="ok", model="opus")

        with patch("sova.llm.client.get_provider", return_value=provider):
            await invoke("test prompt", model="opus", fallback_model="sonnet")

        provider.invoke.assert_called_once()
        args, kwargs = provider.invoke.call_args
        assert kwargs.get("fallback_model") == "sonnet"

    async def test_invoke_command_passes_fallback_to_provider(self) -> None:
        """The client.invoke_command() should pass fallback_model to the provider."""
        provider = AsyncMock(spec=LLMProvider)
        provider.invoke_command.return_value = LLMResult(text="ok", model="opus")

        with patch("sova.llm.client.get_provider", return_value=provider):
            await invoke_command("/develop", args="42", model="opus", fallback_model="sonnet")

        provider.invoke_command.assert_called_once()
        args, kwargs = provider.invoke_command.call_args
        assert kwargs.get("fallback_model") == "sonnet"

    async def test_provider_invoke_command_passes_fallback_to_invoke(self) -> None:
        """The provider's invoke_command() should pass fallback_model to its invoke()."""
        provider = ClaudeCodeProvider()
        provider.invoke = AsyncMock(return_value=LLMResult(text="ok", model="opus"))

        await provider.invoke_command("/test", model="opus", fallback_model="sonnet")

        provider.invoke.assert_called_once()
        args, kwargs = provider.invoke.call_args
        assert kwargs.get("fallback_model") == "sonnet"

    async def test_claude_code_provider_builds_args_with_fallback(self) -> None:
        """ClaudeCodeProvider should include --fallback-model flag when fallback_model is provided."""
        from sova.llm.providers.claude_code import _build_args

        args = _build_args("test prompt", model="opus", fallback_model="sonnet")

        assert "--model" in args
        assert "opus" in args
        assert "--fallback-model" in args
        assert "sonnet" in args

    async def test_claude_code_provider_omits_fallback_when_none(self) -> None:
        """ClaudeCodeProvider should NOT include --fallback-model flag when fallback_model is None."""
        from sova.llm.providers.claude_code import _build_args

        args = _build_args("test prompt", model="opus", fallback_model=None)

        assert "--model" in args
        assert "opus" in args
        assert "--fallback-model" not in args


class TestStepInvokesFallback:
    """Test that steps compute and pass the appropriate fallback model."""

    async def test_step_passes_next_fallback_to_invoke_command(self, tmp_path: Path) -> None:
        """Steps should pass fallback_models[fallback_model_index] as fallback_model parameter."""
        from sova.config.models import AgentConfig, ProjectConfig
        from sova.core.context import ExecutionContext
        from sova.core.steps.develop import DevelopStep

        config = ProjectConfig(
            github_repo="test/repo",
            agent=AgentConfig(model="opus", fallback_models=["sonnet", "haiku"]),
        )
        from sova.adapters.github import GitHubAdapter

        adapter = GitHubAdapter("test/repo", "testuser", project_number=0)
        ctx = ExecutionContext(
            project_dir=tmp_path,
            config=config,
            adapter=adapter,
            issue_number="123",
            fallback_model_index=0,
        )
        ctx.resolved_model = "opus"

        step = DevelopStep()

        # Mock the invoke_command to capture what was passed
        mock_result = LLMResult(text="ok", model="opus", cost_usd=Decimal("1.0"), session_id="test-session")
        with patch("sova.core.steps.develop.invoke_command", return_value=mock_result) as mock_invoke:
            # Also mock _append_implementation_notes and the inner check loop
            with patch("sova.core.steps.develop._append_implementation_notes", return_value=None):
                with patch.object(step, "_run_inner_check_loop", return_value=(True, "")):
                    await step.execute(ctx)

        # Verify invoke_command was called with fallback_model="sonnet"
        mock_invoke.assert_called_once()
        args, kwargs = mock_invoke.call_args
        assert kwargs.get("model") == "opus"
        assert kwargs.get("fallback_model") == "sonnet"

    async def test_step_passes_correct_fallback_after_index_advance(self, tmp_path: Path) -> None:
        """After fallback_model_index advances, steps should pass the next fallback."""
        from sova.config.models import AgentConfig, ProjectConfig
        from sova.core.context import ExecutionContext
        from sova.core.steps.develop import DevelopStep

        config = ProjectConfig(
            github_repo="test/repo",
            agent=AgentConfig(model="opus", fallback_models=["sonnet", "haiku"]),
        )
        from sova.adapters.github import GitHubAdapter

        adapter = GitHubAdapter("test/repo", "testuser", project_number=0)
        ctx = ExecutionContext(
            project_dir=tmp_path,
            config=config,
            adapter=adapter,
            issue_number="123",
            fallback_model_index=1,  # Already advanced once
        )
        ctx.resolved_model = "sonnet"  # Fallback kicked in

        step = DevelopStep()

        mock_result = LLMResult(text="ok", model="sonnet", cost_usd=Decimal("1.0"), session_id="test-session")
        with patch("sova.core.steps.develop.invoke_command", return_value=mock_result) as mock_invoke:
            with patch("sova.core.steps.develop._append_implementation_notes", return_value=None):
                with patch.object(step, "_run_inner_check_loop", return_value=(True, "")):
                    await step.execute(ctx)

        # Verify invoke_command was called with fallback_model="haiku"
        mock_invoke.assert_called_once()
        args, kwargs = mock_invoke.call_args
        assert kwargs.get("model") == "sonnet"
        assert kwargs.get("fallback_model") == "haiku"

    async def test_step_passes_no_fallback_when_exhausted(self, tmp_path: Path) -> None:
        """When fallback_model_index >= len(fallback_models), no fallback should be passed."""
        from sova.config.models import AgentConfig, ProjectConfig
        from sova.core.context import ExecutionContext
        from sova.core.steps.develop import DevelopStep

        config = ProjectConfig(
            github_repo="test/repo",
            agent=AgentConfig(model="opus", fallback_models=["sonnet", "haiku"]),
        )
        from sova.adapters.github import GitHubAdapter

        adapter = GitHubAdapter("test/repo", "testuser", project_number=0)
        ctx = ExecutionContext(
            project_dir=tmp_path,
            config=config,
            adapter=adapter,
            issue_number="123",
            fallback_model_index=2,  # Exhausted both fallbacks
        )
        ctx.resolved_model = "haiku"  # Last fallback

        step = DevelopStep()

        mock_result = LLMResult(text="ok", model="haiku", cost_usd=Decimal("1.0"), session_id="test-session")
        with patch("sova.core.steps.develop.invoke_command", return_value=mock_result) as mock_invoke:
            with patch("sova.core.steps.develop._append_implementation_notes", return_value=None):
                with patch.object(step, "_run_inner_check_loop", return_value=(True, "")):
                    await step.execute(ctx)

        # Verify invoke_command was called with no fallback_model
        mock_invoke.assert_called_once()
        args, kwargs = mock_invoke.call_args
        assert kwargs.get("model") == "haiku"
        assert kwargs.get("fallback_model") is None
