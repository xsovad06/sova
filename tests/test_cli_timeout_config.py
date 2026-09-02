"""Tests for configurable CLI timeout feature (#687)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


class TestCLITimeoutConfig:
    """Test that CLI timeout is configurable via LLMConfig."""

    def test_llm_config_has_cli_timeout_field(self) -> None:
        from sova.config.models import LLMConfig

        cfg = LLMConfig()
        assert hasattr(cfg, "cli_timeout")
        assert cfg.cli_timeout == 900

    def test_llm_config_cli_timeout_can_be_overridden(self) -> None:
        from sova.config.models import LLMConfig

        cfg = LLMConfig(cli_timeout=1200)
        assert cfg.cli_timeout == 1200

    def test_llm_config_cli_timeout_rejects_zero(self) -> None:
        from pydantic import ValidationError

        from sova.config.models import LLMConfig

        with pytest.raises(ValidationError):
            LLMConfig(cli_timeout=0)

    def test_llm_config_cli_timeout_rejects_negative(self) -> None:
        from pydantic import ValidationError

        from sova.config.models import LLMConfig

        with pytest.raises(ValidationError):
            LLMConfig(cli_timeout=-1)

    def test_cli_timeout_loaded_from_toml(self, tmp_path: Path) -> None:
        # Intentionally exercises the TOML fallback loading path (issue #557).
        from sova.config.loader import load_config

        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[llm]\ncli_timeout = 1800\n")
        cfg = load_config(tmp_path)
        assert cfg.llm.cli_timeout == 1800

    @pytest.fixture
    def mock_run(self):
        with patch("sova.llm.providers.claude_code.run", new_callable=AsyncMock) as mock:
            yield mock

    async def test_invoke_uses_config_timeout_when_none(self, mock_run: AsyncMock, tmp_path: Path, seed_config) -> None:
        """When timeout=None, client.invoke() should resolve to config.cli_timeout."""
        import json

        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        # Setup config with custom timeout
        seed_config(tmp_path, llm={"cli_timeout": 1200})

        # Mock successful LLM response
        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "ok",
                    "total_cost_usd": 0.01,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "modelUsage": {},
                    "stop_reason": "end_turn",
                }
            ),
            stderr="",
        )

        # Call invoke with timeout=None (default)
        await invoke("test prompt", cwd=tmp_path)

        # Verify the timeout passed to the provider
        assert mock_run.call_args[1]["timeout"] == 1200

    async def test_invoke_preserves_explicit_timeout(self, mock_run: AsyncMock, tmp_path: Path, seed_config) -> None:
        """When timeout is explicitly set, it should be passed through unchanged."""
        import json

        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        seed_config(tmp_path, llm={"cli_timeout": 1200})

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "ok",
                    "total_cost_usd": 0.01,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "modelUsage": {},
                    "stop_reason": "end_turn",
                }
            ),
            stderr="",
        )

        # Call with explicit timeout
        await invoke("test prompt", cwd=tmp_path, timeout=120)

        # Verify explicit timeout is preserved
        assert mock_run.call_args[1]["timeout"] == 120

    async def test_invoke_command_uses_config_timeout(self, mock_run: AsyncMock, tmp_path: Path, seed_config) -> None:
        """invoke_command should also resolve timeout=None to config value."""
        import json

        from sova.llm.client import invoke_command
        from sova.utils.shell import ShellResult

        seed_config(tmp_path, llm={"cli_timeout": 1500})

        # Create the command file
        cmd_dir = tmp_path / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "test.md").write_text("# test command")

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "ok",
                    "total_cost_usd": 0.01,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "modelUsage": {},
                    "stop_reason": "end_turn",
                }
            ),
            stderr="",
        )

        await invoke_command("/test", cwd=tmp_path)

        assert mock_run.call_args[1]["timeout"] == 1500

    async def test_config_load_failure_uses_hardcoded_fallback(self, mock_run: AsyncMock) -> None:
        """When config loading fails, fallback to hardcoded 900s."""
        import json

        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "ok",
                    "total_cost_usd": 0.01,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "modelUsage": {},
                    "stop_reason": "end_turn",
                }
            ),
            stderr="",
        )

        # Call without cwd (will trigger config load failure in resolution)
        with patch("sova.config.loader.load_config", side_effect=FileNotFoundError):
            await invoke("test prompt", timeout=None)

        # Should fallback to 900s
        assert mock_run.call_args[1]["timeout"] == 900

    async def test_litellm_provider_receives_resolved_timeout(self, tmp_path: Path, seed_config) -> None:
        """LiteLLM provider should receive the resolved timeout value."""
        from unittest.mock import MagicMock

        from sova.llm.client import reset_provider, set_provider

        with patch.dict("sys.modules", {"litellm": MagicMock(__version__="1.0.0")}):
            import sova.llm.litellm_provider as llm_mod

            orig_has = llm_mod._HAS_LITELLM
            orig_litellm = getattr(llm_mod, "litellm", None)
            try:
                llm_mod._HAS_LITELLM = True
                mock_litellm = MagicMock()
                llm_mod.litellm = mock_litellm

                from sova.llm.client import invoke
                from sova.llm.litellm_provider import LiteLLMProvider

                seed_config(tmp_path, llm={"cli_timeout": 1000})

                # Mock response (async)
                from tests.test_llm import _MockResponse

                mock_litellm.acompletion = AsyncMock(return_value=_MockResponse())

                # Use LiteLLM provider
                provider = LiteLLMProvider(model="gpt-4o")
                set_provider(provider)

                await invoke("test", cwd=tmp_path)

                # Verify timeout was passed
                call_kwargs = mock_litellm.acompletion.call_args[1]
                assert call_kwargs["timeout"] == 1000
            finally:
                llm_mod._HAS_LITELLM = orig_has
                llm_mod.litellm = orig_litellm
                reset_provider()
