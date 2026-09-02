"""Tests for SOVA LLM interaction layer."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.llm import ComplexityTier, assess_complexity
from sova.llm.models import LLMResult, StreamEvent

# ---------------------------------------------------------------------------
# LLMResult dataclass
# ---------------------------------------------------------------------------


class TestLLMResult:
    def test_create_result(self) -> None:
        result = LLMResult(
            text="Hello world",
            model="claude-sonnet-4-5",
            cost_usd=Decimal("0.05"),
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            duration_ms=5000,
            session_id="abc-123",
            stop_reason="end_turn",
        )
        assert result.text == "Hello world"
        assert result.model == "claude-sonnet-4-5"
        assert result.cost_usd == Decimal("0.05")
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.total_tokens == 150

    def test_defaults(self) -> None:
        result = LLMResult(text="ok", model="opus")
        assert result.cost_usd == Decimal("0")
        assert result.input_tokens == 0
        assert result.output_tokens == 0
        assert result.cache_read_tokens == 0
        assert result.cache_creation_tokens == 0
        assert result.duration_ms == 0
        assert result.session_id == ""
        assert result.stop_reason == ""

    def test_is_error(self) -> None:
        ok = LLMResult(text="fine", model="opus", stop_reason="end_turn")
        assert not ok.is_error

        err = LLMResult(text="", model="opus", stop_reason="error")
        assert err.is_error


class TestStreamEvent:
    def test_content_event(self) -> None:
        event = StreamEvent(type="content", text="partial output")
        assert event.type == "content"
        assert event.text == "partial output"

    def test_result_event(self) -> None:
        event = StreamEvent(type="result", text="final", result=LLMResult(text="final", model="opus"))
        assert event.result is not None
        assert event.result.text == "final"


# ---------------------------------------------------------------------------
# Client: invoke()
# ---------------------------------------------------------------------------


def _make_cli_json(
    result_text: str = "Hello",
    cost: float = 0.05,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
    cache_creation: int = 200,
    duration_ms: int = 5000,
    model_id: str = "claude-sonnet-4-5@20250929",
) -> str:
    """Build a realistic Claude CLI JSON output."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": duration_ms,
            "result": result_text,
            "stop_reason": "end_turn",
            "session_id": "test-session-id",
            "total_cost_usd": cost,
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output_tokens,
            },
            "modelUsage": {
                model_id: {
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "cacheReadInputTokens": cache_read,
                    "cacheCreationInputTokens": cache_creation,
                    "costUSD": cost,
                }
            },
        }
    )


def _make_error_json() -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "duration_ms": 1000,
            "result": "Max turns reached",
            "stop_reason": "error",
            "session_id": "err-session",
            "total_cost_usd": 0.01,
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 5,
            },
            "modelUsage": {},
        }
    )


@pytest.fixture(autouse=True)
def _reset_provider():
    """Reset the global provider between tests to avoid state leakage."""
    from sova.llm.client import reset_provider

    reset_provider()
    yield
    reset_provider()


class TestInvoke:
    @pytest.fixture
    def mock_run(self):
        with patch("sova.llm.providers.claude_code.run", new_callable=AsyncMock) as mock:
            yield mock

    async def test_invoke_basic(self, mock_run: AsyncMock) -> None:
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=_make_cli_json(),
            stderr="",
        )

        result = await invoke("Say hello")

        assert result.text == "Hello"
        assert result.cost_usd == Decimal("0.05")
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.session_id == "test-session-id"

        # Verify CLI args
        call_args = mock_run.call_args[0]
        assert "claude" in call_args
        assert "-p" in call_args
        assert "Say hello" in call_args
        assert "--output-format" in call_args
        assert "json" in call_args

    async def test_invoke_with_model(self, mock_run: AsyncMock) -> None:
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=_make_cli_json(),
            stderr="",
        )

        await invoke("Hello", model="sonnet")

        call_args = mock_run.call_args[0]
        assert "--model" in call_args
        assert "sonnet" in call_args

    async def test_invoke_with_cwd(self, mock_run: AsyncMock, tmp_path: Path) -> None:
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=_make_cli_json(),
            stderr="",
        )

        await invoke("Hello", cwd=tmp_path)

        assert mock_run.call_args[1].get("cwd") == tmp_path

    async def test_invoke_with_max_budget(self, mock_run: AsyncMock) -> None:
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=_make_cli_json(),
            stderr="",
        )

        await invoke("Hello", max_budget_usd=Decimal("5.00"))

        call_args = mock_run.call_args[0]
        assert "--max-budget-usd" in call_args
        assert "5.00" in call_args

    async def test_invoke_cli_failure(self, mock_run: AsyncMock) -> None:
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=1,
            stdout="",
            stderr="claude: command not found",
        )

        with pytest.raises(RuntimeError, match="Claude CLI failed"):
            await invoke("Hello")

    async def test_invoke_error_result(self, mock_run: AsyncMock) -> None:
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=_make_error_json(),
            stderr="",
        )

        result = await invoke("Hello")
        assert result.is_error
        assert result.stop_reason == "error"

    async def test_invoke_cli_failure_extracts_stdout_json(self, mock_run: AsyncMock) -> None:
        """When stderr is empty, error detail should come from stdout JSON."""
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        error_json = json.dumps(
            {
                "is_error": True,
                "terminal_reason": "budget_exceeded",
                "result": "Max budget of $2.00 exceeded",
            }
        )
        mock_run.return_value = ShellResult(
            returncode=1,
            stdout=error_json,
            stderr="",
        )

        with pytest.raises(RuntimeError, match="budget_exceeded") as exc_info:
            await invoke("Hello")
        assert "is_error=true" in str(exc_info.value)

    async def test_invoke_cli_exit_1_with_valid_output(self, mock_run: AsyncMock) -> None:
        """Exit code 1 with valid JSON and empty stderr should return result."""
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=1,
            stdout=_make_cli_json(),
            stderr="",
        )
        result = await invoke("Hello")
        assert result.text == "Hello"
        assert not result.is_error

    async def test_invoke_cli_exit_1_with_invalid_json_falls_through(self, mock_run: AsyncMock) -> None:
        """Exit code 1 with unparseable stdout and empty stderr falls through to error."""
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=1,
            stdout="not valid json {{{",
            stderr="",
        )
        with pytest.raises(RuntimeError, match="Claude CLI failed"):
            await invoke("Hello")

    async def test_invoke_success_empty_output_raises(self, mock_run: AsyncMock) -> None:
        """Successful exit with empty stdout raises RuntimeError."""
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout="",
            stderr="",
        )
        with pytest.raises(RuntimeError, match="produced no output"):
            await invoke("Hello")

    async def test_invoke_cli_failure_prefers_stderr(self, mock_run: AsyncMock) -> None:
        """When stderr has content, it should be used over stdout."""
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=1,
            stdout='{"result": "ignored"}',
            stderr="actual error message",
        )

        with pytest.raises(RuntimeError, match="actual error message"):
            await invoke("Hello")


# ---------------------------------------------------------------------------
# _extract_failure_detail
# ---------------------------------------------------------------------------


class TestExtractFailureDetail:
    def test_prefers_stderr_when_present(self) -> None:
        from sova.llm.providers.claude_code import _extract_failure_detail
        from sova.utils.shell import ShellResult

        result = ShellResult(returncode=1, stdout="", stderr="real error")
        assert _extract_failure_detail(result) == "real error"

    def test_extracts_terminal_reason_from_stdout_json(self) -> None:
        from sova.llm.providers.claude_code import _extract_failure_detail
        from sova.utils.shell import ShellResult

        stdout = json.dumps(
            {
                "terminal_reason": "budget_exceeded",
                "is_error": True,
                "result": "Budget limit reached",
            }
        )
        result = ShellResult(returncode=1, stdout=stdout, stderr="")
        detail = _extract_failure_detail(result)
        assert "terminal_reason=budget_exceeded" in detail
        assert "is_error=true" in detail
        assert "Budget limit reached" in detail

    def test_handles_stdout_json_with_only_result(self) -> None:
        from sova.llm.providers.claude_code import _extract_failure_detail
        from sova.utils.shell import ShellResult

        stdout = json.dumps({"result": "Something went wrong"})
        result = ShellResult(returncode=1, stdout=stdout, stderr="")
        detail = _extract_failure_detail(result)
        assert "Something went wrong" in detail

    def test_handles_invalid_json_stdout(self) -> None:
        from sova.llm.providers.claude_code import _extract_failure_detail
        from sova.utils.shell import ShellResult

        result = ShellResult(returncode=1, stdout="not json {{{", stderr="")
        detail = _extract_failure_detail(result)
        assert "not json" in detail

    def test_handles_empty_stdout_and_stderr(self) -> None:
        from sova.llm.providers.claude_code import _extract_failure_detail
        from sova.utils.shell import ShellResult

        result = ShellResult(returncode=1, stdout="", stderr="")
        assert _extract_failure_detail(result) == "(no error detail captured)"

    def test_truncates_long_result(self) -> None:
        from sova.llm.providers.claude_code import _extract_failure_detail
        from sova.utils.shell import ShellResult

        long_text = "x" * 500
        stdout = json.dumps({"result": long_text})
        result = ShellResult(returncode=1, stdout=stdout, stderr="")
        detail = _extract_failure_detail(result)
        assert len(detail) <= 310

    def test_handles_non_dict_json_stdout(self) -> None:
        from sova.llm.providers.claude_code import _extract_failure_detail
        from sova.utils.shell import ShellResult

        result = ShellResult(returncode=1, stdout='["a", "b"]', stderr="")
        detail = _extract_failure_detail(result)
        assert detail == '["a", "b"]'


# ---------------------------------------------------------------------------
# Client: invoke_command()
# ---------------------------------------------------------------------------


class TestInvokeCommand:
    @pytest.fixture
    def mock_run(self):
        with patch("sova.llm.providers.claude_code.run", new_callable=AsyncMock) as mock:
            yield mock

    async def test_invoke_command(self, mock_run: AsyncMock) -> None:
        from sova.llm.client import invoke_command
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=_make_cli_json(result_text="Command output"),
            stderr="",
        )

        result = await invoke_command("/develop", args="42")

        assert result.text == "Command output"

        call_args = mock_run.call_args[0]
        assert "claude" in call_args
        assert "-p" in call_args
        # The prompt should contain the command
        prompt_idx = call_args.index("-p") + 1
        assert "/develop" in call_args[prompt_idx]
        assert "42" in call_args[prompt_idx]

    async def test_invoke_command_no_args(self, mock_run: AsyncMock) -> None:
        from sova.llm.client import invoke_command
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=_make_cli_json(),
            stderr="",
        )

        await invoke_command("/review")

        call_args = mock_run.call_args[0]
        prompt_idx = call_args.index("-p") + 1
        assert "/review" in call_args[prompt_idx]

    async def test_invoke_command_timeout(self, mock_run: AsyncMock) -> None:
        """Test that asyncio.timeout context manager enforces timeout."""
        import asyncio

        from sova.llm.client import invoke_command

        async def slow_operation(*_args: str, **_kwargs: object) -> None:
            await asyncio.sleep(10)

        mock_run.side_effect = slow_operation

        with pytest.raises(TimeoutError):
            await invoke_command("/develop", timeout=0.1)

    async def test_invoke_command_uses_resolved_timeout(self, mock_run: AsyncMock) -> None:
        """Test that _resolve_timeout is called and used."""
        from sova.llm.client import invoke_command
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=_make_cli_json(),
            stderr="",
        )

        # Explicit timeout should be used
        await invoke_command("/review", timeout=300.0)

        assert mock_run.call_args[1]["timeout"] == 300.0


# ---------------------------------------------------------------------------
# Client: _resolve_timeout()
# ---------------------------------------------------------------------------


class TestResolveTimeout:
    def test_explicit_timeout_returned(self) -> None:
        from sova.llm.client import _resolve_timeout

        assert _resolve_timeout(120.0) == 120.0

    def test_config_timeout_used_when_none(self, tmp_path: Path) -> None:
        from sova.llm.client import _resolve_timeout

        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[llm]\ncli_timeout = 600\n")

        result = _resolve_timeout(None, cwd=tmp_path)
        assert result == 600.0

    def test_fallback_when_config_load_fails(self) -> None:
        from sova.llm.client import _resolve_timeout

        # No config file, should use hardcoded fallback
        result = _resolve_timeout(None, cwd=Path("/nonexistent"))
        assert result == 900.0

    def test_fallback_when_config_invalid(self, tmp_path: Path) -> None:
        from sova.llm.client import _resolve_timeout

        # Invalid TOML should fall back to default
        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[llm]\ncli_timeout = invalid\n")

        result = _resolve_timeout(None, cwd=tmp_path)
        assert result == 900.0


# ---------------------------------------------------------------------------
# Client: invoke_batch()
# ---------------------------------------------------------------------------


class TestInvokeBatch:
    async def test_empty_requests_returns_empty(self) -> None:
        from sova.llm.client import invoke_batch

        result = await invoke_batch([])
        assert result == []

    async def test_invoke_batch_uses_batch_provider(self) -> None:
        from sova.llm.client import invoke_batch
        from sova.llm.models import BatchRequest, BatchResult, LLMResult

        req = BatchRequest(custom_id="req-1", prompt="hello")
        mock_batch_result = [
            BatchResult(
                request=req,
                result=LLMResult(text="response 1", model="opus"),
            )
        ]

        with patch("sova.llm.providers.anthropic_batch.create_batch_provider") as mock_create:
            mock_provider = AsyncMock()
            mock_provider.invoke_batch = AsyncMock(return_value=mock_batch_result)
            mock_create.return_value = mock_provider

            result = await invoke_batch([req], gcs_bucket="test-bucket")

            assert result == mock_batch_result
            mock_create.assert_called_once_with(gcs_bucket="test-bucket", gcs_prefix="sova-batch")
            mock_provider.invoke_batch.assert_awaited_once_with([req], poll_interval=60, timeout=86400)

    async def test_invoke_batch_falls_back_to_provider(self) -> None:
        from sova.llm.client import invoke_batch
        from sova.llm.models import BatchRequest, BatchResult, LLMResult

        req = BatchRequest(custom_id="req-1", prompt="hello")
        mock_result = [
            BatchResult(
                request=req,
                result=LLMResult(text="sequential response", model="sonnet"),
            )
        ]

        with (
            patch("sova.llm.providers.anthropic_batch.create_batch_provider", return_value=None),
            patch("sova.llm.client.get_provider") as mock_get_provider,
        ):
            mock_provider = AsyncMock()
            mock_provider.invoke_batch = AsyncMock(return_value=mock_result)
            mock_get_provider.return_value = mock_provider

            result = await invoke_batch([req])

            assert result == mock_result
            mock_get_provider.assert_called_once_with()
            mock_provider.invoke_batch.assert_awaited_once_with([req], poll_interval=60, timeout=86400)


# ---------------------------------------------------------------------------
# Client: resolve_model()
# ---------------------------------------------------------------------------


class TestResolveModel:
    def test_resolve_from_roles_config(self) -> None:
        from sova.config.models import RolesConfig
        from sova.llm.client import resolve_model

        roles = RolesConfig(researcher_model="opus", triage_model="haiku")
        assert resolve_model("researcher", roles) == ("opus", "role:researcher->opus")
        assert resolve_model("triage", roles) == ("haiku", "role:triage->haiku")

    def test_resolve_developer_uses_default(self) -> None:
        from sova.config.models import RolesConfig
        from sova.llm.client import resolve_model

        roles = RolesConfig(default="developer")
        # developer has no explicit model config, returns None
        assert resolve_model("developer", roles) is None

    def test_resolve_unknown_role(self) -> None:
        from sova.config.models import RolesConfig
        from sova.llm.client import resolve_model

        roles = RolesConfig()
        assert resolve_model("unknown_role", roles) is None

    def test_complexity_fallback_when_no_role_model(self) -> None:
        from sova.config.models import RolesConfig
        from sova.llm.client import resolve_model

        roles = RolesConfig()
        result_trivial = resolve_model("developer", roles, complexity=ComplexityTier.TRIVIAL)
        assert result_trivial == ("haiku", "complexity:trivial->haiku")
        result_complex = resolve_model("developer", roles, complexity=ComplexityTier.COMPLEX)
        assert result_complex == ("opus", "complexity:complex->opus")

    def test_mapped_role_falls_back_to_complexity_when_model_unset(self) -> None:
        """A mapped role (researcher) with no explicit model unset falls back to complexity routing."""
        from sova.config.models import RolesConfig
        from sova.llm.client import resolve_model

        roles = RolesConfig(researcher_model="")
        result = resolve_model("researcher", roles, complexity=ComplexityTier.TRIVIAL)
        assert result == ("haiku", "complexity:trivial->haiku")

    def test_role_model_takes_priority_over_complexity(self) -> None:
        from sova.config.models import RolesConfig
        from sova.llm.client import resolve_model

        roles = RolesConfig(researcher_model="sonnet")
        result = resolve_model("researcher", roles, complexity=ComplexityTier.EPIC)
        assert result == ("sonnet", "role:researcher->sonnet")

    def test_complexity_with_llm_config_override(self) -> None:
        from sova.config.models import LLMConfig, RolesConfig
        from sova.llm.client import resolve_model

        llm_cfg = LLMConfig(routing={"moderate": "opus"})
        roles = RolesConfig()
        result = resolve_model("developer", roles, complexity=ComplexityTier.MODERATE, llm_config=llm_cfg)
        assert result == ("opus", "config:override->opus")

    def test_no_complexity_returns_none(self) -> None:
        from sova.config.models import RolesConfig
        from sova.llm.client import resolve_model

        roles = RolesConfig()
        assert resolve_model("developer", roles) is None


# ---------------------------------------------------------------------------
# route_model()
# ---------------------------------------------------------------------------


class TestRouteModel:
    def test_default_routing_all_tiers(self) -> None:
        from sova.llm.routing import route_model

        assert route_model(ComplexityTier.TRIVIAL) == ("haiku", "complexity:trivial->haiku")
        assert route_model(ComplexityTier.SIMPLE) == ("sonnet", "complexity:simple->sonnet")
        assert route_model(ComplexityTier.MODERATE) == ("sonnet", "complexity:moderate->sonnet")
        assert route_model(ComplexityTier.COMPLEX) == ("opus", "complexity:complex->opus")
        assert route_model(ComplexityTier.EPIC) == ("opus", "complexity:epic->opus")

    def test_partial_config_override(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.routing import route_model

        llm_cfg = LLMConfig(routing={"moderate": "opus"})
        assert route_model(ComplexityTier.MODERATE, llm_config=llm_cfg) == ("opus", "config:override->opus")
        # Unspecified tiers use defaults
        assert route_model(ComplexityTier.TRIVIAL, llm_config=llm_cfg) == ("haiku", "complexity:trivial->haiku")
        assert route_model(ComplexityTier.SIMPLE, llm_config=llm_cfg) == ("sonnet", "complexity:simple->sonnet")

    def test_full_config_override(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.routing import route_model

        llm_cfg = LLMConfig(
            routing={
                "trivial": "sonnet",
                "simple": "sonnet",
                "moderate": "opus",
                "complex": "opus",
                "epic": "opus",
            }
        )
        assert route_model(ComplexityTier.TRIVIAL, llm_config=llm_cfg) == ("sonnet", "config:override->sonnet")
        assert route_model(ComplexityTier.MODERATE, llm_config=llm_cfg) == ("opus", "config:override->opus")

    def test_empty_config_uses_defaults(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.routing import route_model

        llm_cfg = LLMConfig(routing={})
        assert route_model(ComplexityTier.TRIVIAL, llm_config=llm_cfg) == ("haiku", "complexity:trivial->haiku")
        assert route_model(ComplexityTier.EPIC, llm_config=llm_cfg) == ("opus", "complexity:epic->opus")

    def test_invalid_keys_accepted_and_ignored(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.routing import route_model

        llm_cfg = LLMConfig(routing={"nonexistent": "haiku", "trivial": "opus"})
        # Unknown keys are accepted in config but ignored during lookup
        assert "nonexistent" in llm_cfg.routing
        assert route_model(ComplexityTier.TRIVIAL, llm_config=llm_cfg) == ("opus", "config:override->opus")
        # Unrecognized key has no effect on any tier
        assert route_model(ComplexityTier.SIMPLE, llm_config=llm_cfg) == ("sonnet", "complexity:simple->sonnet")

    def test_empty_string_override_falls_back(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.routing import route_model

        llm_cfg = LLMConfig(routing={"trivial": ""})
        # Empty string is a valid override value (not None), returned as-is
        assert route_model(ComplexityTier.TRIVIAL, llm_config=llm_cfg) == ("", "config:override->")

    def test_unknown_complexity_tier_falls_back_to_sonnet(self) -> None:
        from unittest.mock import MagicMock

        from sova.llm.routing import route_model

        # Simulate a future ComplexityTier member not in _DEFAULT_ROUTING
        fake_tier = MagicMock()
        fake_tier.value = "hypothetical"
        assert route_model(fake_tier) == ("sonnet", "complexity:hypothetical->sonnet")

    def test_none_llm_config_uses_defaults(self) -> None:
        from sova.llm.routing import route_model

        assert route_model(ComplexityTier.COMPLEX, llm_config=None) == ("opus", "complexity:complex->opus")

    def test_task_type_routing_takes_priority(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.routing import route_model

        llm_cfg = LLMConfig(routing={"triage": "ollama/qwen3:8b", "trivial": "haiku"})
        result = route_model(ComplexityTier.TRIVIAL, task_type="triage", llm_config=llm_cfg)
        assert result == ("ollama/qwen3:8b", "task_type:triage->ollama/qwen3:8b")

    def test_task_type_falls_through_to_complexity(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.routing import route_model

        llm_cfg = LLMConfig(routing={"trivial": "haiku"})
        result = route_model(ComplexityTier.TRIVIAL, task_type="extraction", llm_config=llm_cfg)
        assert result == ("haiku", "config:override->haiku")

    def test_task_type_no_config_uses_defaults(self) -> None:
        from sova.llm.routing import route_model

        result = route_model(ComplexityTier.MODERATE, task_type="triage")
        assert result == ("sonnet", "complexity:moderate->sonnet")

    def test_task_type_none_ignored(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.routing import route_model

        llm_cfg = LLMConfig(routing={"triage": "ollama/qwen3:8b"})
        result = route_model(ComplexityTier.MODERATE, task_type=None, llm_config=llm_cfg)
        assert result == ("sonnet", "complexity:moderate->sonnet")

    def test_task_type_empty_string_ignored(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.routing import route_model

        llm_cfg = LLMConfig(routing={"triage": "ollama/qwen3:8b"})
        result = route_model(ComplexityTier.MODERATE, task_type="", llm_config=llm_cfg)
        assert result == ("sonnet", "complexity:moderate->sonnet")

    def test_mixed_routing_keys(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.routing import route_model

        llm_cfg = LLMConfig(routing={"trivial": "haiku", "triage": "ollama/qwen3:8b"})
        # Task-type key should work
        assert route_model(ComplexityTier.TRIVIAL, task_type="triage", llm_config=llm_cfg) == (
            "ollama/qwen3:8b",
            "task_type:triage->ollama/qwen3:8b",
        )
        # Complexity key should work when no task_type match
        assert route_model(ComplexityTier.TRIVIAL, task_type="extraction", llm_config=llm_cfg) == (
            "haiku",
            "config:override->haiku",
        )


class TestTaskTypeKeys:
    def test_disjoint_from_complexity_tiers(self) -> None:
        from sova.llm.routing import TASK_TYPE_KEYS

        complexity_keys = {t.value for t in ComplexityTier}
        overlap = TASK_TYPE_KEYS & complexity_keys
        assert not overlap, f"Overlapping keys: {overlap}"

    def test_known_keys_present(self) -> None:
        from sova.llm.routing import TASK_TYPE_KEYS

        for key in ("triage", "extraction", "pr_body", "harden", "planner"):
            assert key in TASK_TYPE_KEYS


class TestResolveTaskTypeModel:
    def test_explicit_model_takes_priority(self) -> None:
        from sova.llm.client import _resolve_task_type_model

        assert _resolve_task_type_model("opus", "triage") == "opus"

    def test_no_task_type_returns_model(self) -> None:
        from sova.llm.client import _resolve_task_type_model

        assert _resolve_task_type_model(None, None) is None

    def test_task_type_resolves_from_config(self) -> None:
        from sova.llm.client import _resolve_task_type_model

        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value.llm.routing = {"triage": "ollama/qwen3:8b"}
            result = _resolve_task_type_model(None, "triage")
            assert result == "ollama/qwen3:8b"

    def test_task_type_not_in_config_returns_none(self) -> None:
        from sova.llm.client import _resolve_task_type_model

        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value.llm.routing = {"triage": "ollama/qwen3:8b"}
            result = _resolve_task_type_model(None, "extraction")
            assert result is None

    def test_config_load_failure_returns_model(self) -> None:
        from sova.llm.client import _resolve_task_type_model

        with patch("sova.config.loader.load_config", side_effect=FileNotFoundError):
            result = _resolve_task_type_model(None, "triage")
            assert result is None

    def test_empty_routing_returns_model(self) -> None:
        from sova.llm.client import _resolve_task_type_model

        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value.llm.routing = {}
            result = _resolve_task_type_model(None, "triage")
            assert result is None


# ---------------------------------------------------------------------------
# Provider: _parse_result()
# ---------------------------------------------------------------------------


class TestParseResult:
    def test_parse_success(self) -> None:
        from sova.llm.providers.claude_code import _parse_result

        raw = json.loads(
            _make_cli_json(
                result_text="Parsed output",
                cost=0.123,
                input_tokens=500,
                output_tokens=200,
                cache_read=100,
                cache_creation=300,
                duration_ms=8000,
            )
        )
        result = _parse_result(raw)

        assert result.text == "Parsed output"
        assert result.cost_usd == Decimal("0.123")
        assert result.input_tokens == 500
        assert result.output_tokens == 200
        assert result.cache_read_tokens == 100
        assert result.cache_creation_tokens == 300
        assert result.duration_ms == 8000
        assert result.session_id == "test-session-id"
        assert result.stop_reason == "end_turn"

    def test_parse_extracts_model_from_usage(self) -> None:
        from sova.llm.providers.claude_code import _parse_result

        raw = json.loads(_make_cli_json(model_id="claude-opus-4-6@20260401"))
        result = _parse_result(raw)
        assert result.model == "claude-opus-4-6@20260401"

    def test_parse_missing_fields_defaults(self) -> None:
        from sova.llm.providers.claude_code import _parse_result

        raw = {"result": "ok", "type": "result"}
        result = _parse_result(raw)
        assert result.text == "ok"
        assert result.cost_usd == Decimal("0")
        assert result.input_tokens == 0


# ---------------------------------------------------------------------------
# Cost tracking: record_cost()
# ---------------------------------------------------------------------------


class TestRecordCost:
    @pytest.fixture(autouse=True)
    async def setup_db(self):
        import os

        from sova.db.session import close_db, init_db

        os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
        await init_db(run_migrations=False)
        yield
        await close_db()
        os.environ.pop("SOVA_DATABASE_URL", None)

    async def test_record_cost_creates_entry(self) -> None:
        from sqlalchemy import select

        from sova.db.models import CostRecord
        from sova.db.session import get_session
        from sova.llm.cost import record_cost
        from sova.llm.models import LLMResult

        result = LLMResult(
            text="output",
            model="claude-opus-4-6",
            cost_usd=Decimal("1.50"),
            input_tokens=5000,
            output_tokens=2000,
            cache_read_tokens=100,
            cache_creation_tokens=500,
            duration_ms=15000,
        )

        record = await record_cost(
            result=result,
            phase="develop",
            issue="42",
            task_run_id=1,
        )

        assert record.model == "claude-opus-4-6"
        assert record.cost_usd == Decimal("1.50")
        assert record.input_tokens == 5000
        assert record.output_tokens == 2000
        assert record.cache_tokens == 600  # read + creation
        assert record.cache_read_tokens == 100
        assert record.cache_write_tokens == 500
        assert record.duration_ms == 15000
        assert record.task_run_id == 1
        assert record.phase == "develop"
        assert record.issue == "42"

        # Verify it was persisted
        async with await get_session() as session:
            stmt = select(CostRecord).where(CostRecord.issue == "42")
            rows = (await session.execute(stmt)).scalars().all()
            assert len(rows) == 1
            assert rows[0].cost_usd == Decimal("1.50")

    async def test_record_cost_without_task_run(self) -> None:
        from sova.llm.cost import record_cost
        from sova.llm.models import LLMResult

        result = LLMResult(text="output", model="sonnet", cost_usd=Decimal("0.01"))

        record = await record_cost(result=result, phase="triage", issue="10")

        assert record.task_run_id is None
        assert record.phase == "triage"

    async def test_record_cost_with_model_selection_reason(self) -> None:
        from sova.llm.cost import record_cost
        from sova.llm.models import LLMResult

        result = LLMResult(text="output", model="haiku", cost_usd=Decimal("0.005"))

        record = await record_cost(
            result=result,
            phase="triage",
            issue="55",
            model_selection_reason="role:triage->haiku",
        )

        assert record.model_selection_reason == "role:triage->haiku"

    async def test_record_cost_reason_defaults_to_none(self) -> None:
        from sova.llm.cost import record_cost
        from sova.llm.models import LLMResult

        result = LLMResult(text="output", model="opus", cost_usd=Decimal("0.50"))

        record = await record_cost(result=result, phase="develop", issue="56")

        assert record.model_selection_reason is None

    async def test_record_cost_cache_breakdown_zero_stored_as_zero(self) -> None:
        from sova.llm.cost import record_cost
        from sova.llm.models import LLMResult

        result = LLMResult(text="output", model="haiku", cost_usd=Decimal("0.01"))

        record = await record_cost(result=result, phase="triage", issue="57")

        assert record.cache_tokens == 0
        assert record.cache_read_tokens == 0
        assert record.cache_write_tokens == 0

    async def test_record_cost_cache_breakdown_partial_data(self) -> None:
        from sova.llm.cost import record_cost
        from sova.llm.models import LLMResult

        result = LLMResult(
            text="output",
            model="sonnet",
            cost_usd=Decimal("0.05"),
            cache_read_tokens=100,
            cache_creation_tokens=0,
        )

        record = await record_cost(result=result, phase="develop", issue="58")

        assert record.cache_tokens == 100
        assert record.cache_read_tokens == 100
        assert record.cache_write_tokens == 0

    async def test_record_cost_normalizes_prefixed_issue(self) -> None:
        from sqlalchemy import select

        from sova.db.models import CostRecord
        from sova.db.session import get_session
        from sova.llm.cost import record_cost
        from sova.llm.models import LLMResult

        result = LLMResult(text="output", model="sonnet", cost_usd=Decimal("0.10"))

        record = await record_cost(result=result, phase="develop", issue="#42")

        assert record.issue == "42"

        async with await get_session() as session:
            stmt = select(CostRecord).where(CostRecord.issue == "42")
            rows = (await session.execute(stmt)).scalars().all()
            assert any(r.cost_usd == Decimal("0.10") for r in rows)


# ---------------------------------------------------------------------------
# Streaming: invoke_streaming()
# ---------------------------------------------------------------------------


class TestInvokeStreaming:
    async def test_invoke_streaming_yields_events(self) -> None:
        from sova.llm.client import invoke_streaming

        stream_lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello "}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello world"}]}}),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "duration_ms": 3000,
                    "result": "Hello world",
                    "stop_reason": "end_turn",
                    "session_id": "stream-session",
                    "total_cost_usd": 0.03,
                    "usage": {
                        "input_tokens": 50,
                        "output_tokens": 20,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                    "modelUsage": {"claude-sonnet-4-5@20250929": {"costUSD": 0.03}},
                }
            ),
        ]

        async def mock_readline():
            if stream_lines:
                line = stream_lines.pop(0)
                return (line + "\n").encode()
            return b""

        mock_proc = AsyncMock()
        mock_proc.stdout.readline = mock_readline
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()

        with patch("sova.llm.providers.claude_code._start_streaming_process", return_value=mock_proc):
            events = []
            async for event in invoke_streaming("Say hello"):
                events.append(event)

        # Should have content events and a final result event
        assert any(e.type == "content" for e in events)
        result_events = [e for e in events if e.type == "result"]
        assert len(result_events) == 1
        assert result_events[0].result is not None
        assert result_events[0].result.text == "Hello world"
        assert result_events[0].result.cost_usd == Decimal("0.03")


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------


class TestLLMProvider:
    def test_abc_cannot_instantiate(self) -> None:
        from sova.llm.provider import LLMProvider

        with pytest.raises(TypeError):
            LLMProvider()  # type: ignore[abstract]

    def test_create_provider_default(self) -> None:
        from sova.llm.provider import create_provider
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        provider = create_provider("claude-code")
        assert isinstance(provider, ClaudeCodeProvider)

    def test_create_provider_unknown(self) -> None:
        from sova.llm.provider import create_provider

        with pytest.raises(ValueError, match="Unknown LLM provider"):
            create_provider("nonexistent")

    def test_create_provider_hybrid(self) -> None:
        from sova.llm.provider import create_provider

        with patch.dict("sys.modules", {"litellm": MagicMock(__version__="1.0.0")}):
            import sova.llm.litellm_provider as llm_mod

            llm_mod._HAS_LITELLM = True
            llm_mod.litellm = MagicMock()
            from sova.llm.litellm_provider import LiteLLMProvider

            provider = create_provider("hybrid")
            assert isinstance(provider, LiteLLMProvider)

    def test_get_provider_default(self) -> None:
        from sova.llm.client import get_provider
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        provider = get_provider()
        assert isinstance(provider, ClaudeCodeProvider)

    def test_set_provider(self) -> None:
        from sova.llm.client import get_provider, set_provider
        from sova.llm.provider import LLMProvider

        class FakeProvider(LLMProvider):
            async def invoke(self, prompt, **kwargs):
                return LLMResult(text="fake", model="fake")

            async def invoke_streaming(self, prompt, **kwargs):
                yield StreamEvent(type="result", text="fake")

            async def check_available(self):
                return True, "fake"

        fake = FakeProvider()
        set_provider(fake)
        assert get_provider() is fake

    def test_normalize_model_name_claude_code(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        p = ClaudeCodeProvider()
        assert p.normalize_model_name("fast") == "sonnet"
        assert p.normalize_model_name("smart") == "opus"
        assert p.normalize_model_name("cheap") == "haiku"
        assert p.normalize_model_name("opus") == "opus"

    async def test_check_available_claude_found(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        p = ClaudeCodeProvider()
        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch(
                "sova.llm.providers.claude_code.run",
                new_callable=AsyncMock,
                return_value=ShellResult(returncode=0, stdout="1.0.0\n", stderr=""),
            ),
        ):
            available, detail = await p.check_available()
            assert available is True
            assert "1.0.0" in detail

    async def test_check_available_claude_not_found(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        p = ClaudeCodeProvider()
        with patch("sova.llm.providers.claude_code.shutil.which", return_value=None):
            available, detail = await p.check_available()
            assert available is False
            assert "not found" in detail

    def test_normalize_model_name_base_default(self) -> None:
        from sova.llm.provider import LLMProvider

        class MinimalProvider(LLMProvider):
            async def invoke(self, prompt, **kwargs):
                return LLMResult(text="", model="")

            async def invoke_streaming(self, prompt, **kwargs):
                yield StreamEvent(type="result", text="")

            async def check_available(self):
                return True, ""

        p = MinimalProvider()
        assert p.normalize_model_name("opus") == "opus"
        assert p.normalize_model_name("anything") == "anything"

    async def test_check_available_version_fails(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        p = ClaudeCodeProvider()
        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch(
                "sova.llm.providers.claude_code.run",
                new_callable=AsyncMock,
                return_value=ShellResult(returncode=1, stdout="", stderr="error"),
            ),
        ):
            available, detail = await p.check_available()
            assert available is False
            assert "failed" in detail

    def test_parse_json_output_invalid_json(self) -> None:
        from sova.llm.providers.claude_code import _parse_json_output

        with pytest.raises(RuntimeError, match="Failed to parse Claude CLI JSON"):
            _parse_json_output("not valid json {{{")

    def test_build_args_includes_permission_mode_bypass(self) -> None:
        from sova.llm.providers.claude_code import _build_args

        args = _build_args("hello")
        assert "--permission-mode" in args
        pm_idx = args.index("--permission-mode")
        assert args[pm_idx + 1] == "bypassPermissions"

    def test_build_args_includes_all_flags(self) -> None:
        from sova.llm.providers.claude_code import _build_args

        args = _build_args(
            "test prompt",
            model="opus",
            max_budget_usd=Decimal("5.00"),
            output_format="stream-json",
        )
        assert args[0] == "claude"
        assert "-p" in args
        assert "--output-format" in args
        assert "stream-json" in args
        assert "--model" in args
        assert "opus" in args
        assert "--max-budget-usd" in args
        assert "5.00" in args
        assert "--permission-mode" in args

    def test_build_args_includes_fallback_model(self) -> None:
        from sova.llm.providers.claude_code import _build_args

        args = _build_args("prompt", model="opus", fallback_model="sonnet")
        assert "--fallback-model" in args
        fm_idx = args.index("--fallback-model")
        assert args[fm_idx + 1] == "sonnet"

    def test_build_args_omits_fallback_model_when_empty(self) -> None:
        from sova.llm.providers.claude_code import _build_args

        args = _build_args("prompt", model="opus")
        assert "--fallback-model" not in args

    def test_build_args_includes_system_prompt(self) -> None:
        from sova.llm.providers.claude_code import _build_args

        args = _build_args("prompt", system_prompt="You are a planner.")
        assert "--system-prompt" in args
        sp_idx = args.index("--system-prompt")
        assert args[sp_idx + 1] == "You are a planner."

    def test_build_args_omits_system_prompt_when_empty(self) -> None:
        from sova.llm.providers.claude_code import _build_args

        args = _build_args("prompt")
        assert "--system-prompt" not in args

    async def test_invoke_command_delegates_to_invoke(self) -> None:
        from sova.llm.provider import LLMProvider

        class TrackingProvider(LLMProvider):
            def __init__(self):
                self.last_prompt = ""

            async def invoke(self, prompt, **kwargs):
                self.last_prompt = prompt
                return LLMResult(text="ok", model="test")

            async def invoke_streaming(self, prompt, **kwargs):
                yield StreamEvent(type="result", text="ok")

            async def check_available(self):
                return True, "test"

        p = TrackingProvider()
        result = await p.invoke_command("/develop", args="42")
        assert p.last_prompt == "/develop 42"
        assert result.text == "ok"


# ---------------------------------------------------------------------------
# _assert_command_exists
# ---------------------------------------------------------------------------


class TestAssertCommandExists:
    def test_valid_command_exists(self, tmp_path: Path) -> None:
        from sova.llm.provider import _assert_command_exists

        cmd_dir = tmp_path / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "develop.md").write_text("# develop")
        _assert_command_exists("/develop", tmp_path)

    def test_command_file_missing(self, tmp_path: Path) -> None:
        from sova.llm.provider import _assert_command_exists

        cmd_dir = tmp_path / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)
        with pytest.raises(RuntimeError, match="not found"):
            _assert_command_exists("/develop", tmp_path)

    def test_empty_name_rejected(self, tmp_path: Path) -> None:
        from sova.llm.provider import _assert_command_exists

        with pytest.raises(RuntimeError, match="Invalid slash command"):
            _assert_command_exists("/", tmp_path)

    def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        from sova.llm.provider import _assert_command_exists

        with pytest.raises(RuntimeError, match="Invalid slash command"):
            _assert_command_exists("/../../etc/passwd", tmp_path)

    def test_slash_in_name_rejected(self, tmp_path: Path) -> None:
        from sova.llm.provider import _assert_command_exists

        with pytest.raises(RuntimeError, match="Invalid slash command"):
            _assert_command_exists("/foo/bar", tmp_path)

    def test_backslash_in_name_rejected(self, tmp_path: Path) -> None:
        from sova.llm.provider import _assert_command_exists

        with pytest.raises(RuntimeError, match="Invalid slash command"):
            _assert_command_exists("/foo\\bar", tmp_path)

    def test_missing_command_restored_from_primary_root(self, tmp_path: Path) -> None:
        from sova.llm.provider import _assert_command_exists

        cwd = tmp_path / "worktree"
        cwd.mkdir()
        cmd_dir = cwd / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)

        primary = tmp_path / "primary"
        primary.mkdir()
        primary_cmd_dir = primary / ".claude" / "commands"
        primary_cmd_dir.mkdir(parents=True)
        (primary_cmd_dir / "develop.md").write_text("# develop")

        def fake_ensure(project_root: Path, wt: Path) -> None:
            src = project_root / ".claude" / "commands" / "develop.md"
            dst = wt / ".claude" / "commands" / "develop.md"
            if src.is_file():
                dst.write_text(src.read_text())

        with patch("sova.llm.provider._resolve_primary_root", return_value=primary):
            with patch("sova.git.worktree.ensure_claude_artifacts", side_effect=fake_ensure):
                _assert_command_exists("/develop", cwd)

    def test_missing_command_restoration_fails_still_raises(self, tmp_path: Path) -> None:
        from sova.llm.provider import _assert_command_exists

        cwd = tmp_path / "worktree"
        cwd.mkdir()
        cmd_dir = cwd / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)

        with patch("sova.llm.provider._resolve_primary_root", return_value=tmp_path / "primary"):
            with patch("sova.git.worktree.ensure_claude_artifacts", side_effect=OSError("fail")):
                with pytest.raises(RuntimeError, match="not found"):
                    _assert_command_exists("/develop", cwd)

    def test_missing_command_no_primary_root_raises(self, tmp_path: Path) -> None:
        from sova.llm.provider import _assert_command_exists

        cwd = tmp_path / "worktree"
        cwd.mkdir()
        cmd_dir = cwd / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)

        with patch("sova.llm.provider._resolve_primary_root", return_value=None):
            with pytest.raises(RuntimeError, match="not found"):
                _assert_command_exists("/develop", cwd)


class TestResolvePrimaryRoot:
    def test_returns_root_from_absolute_git_common_dir(self, tmp_path: Path) -> None:
        from sova.llm.provider import _resolve_primary_root

        primary_root = tmp_path / "primary"
        primary_root.mkdir()
        common_dir = primary_root / ".git"
        common_dir.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=str(common_dir) + "\n")
            result = _resolve_primary_root(tmp_path / "worktree")

        assert result == primary_root

    def test_returns_root_from_relative_git_common_dir(self, tmp_path: Path) -> None:
        from sova.llm.provider import _resolve_primary_root

        cwd = tmp_path / "worktree"
        cwd.mkdir()
        primary_root = tmp_path / "primary"
        primary_root.mkdir()
        git_dir = primary_root / ".git"
        git_dir.mkdir()

        rel_path = os.path.relpath(git_dir, cwd)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=rel_path + "\n")
            result = _resolve_primary_root(cwd)

        assert result == primary_root

    def test_returns_none_when_root_equals_cwd(self, tmp_path: Path) -> None:
        from sova.llm.provider import _resolve_primary_root

        cwd = tmp_path / "repo"
        cwd.mkdir()
        git_dir = cwd / ".git"
        git_dir.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=str(git_dir) + "\n")
            result = _resolve_primary_root(cwd)

        assert result is None

    def test_returns_none_on_git_failure(self, tmp_path: Path) -> None:
        from sova.llm.provider import _resolve_primary_root

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = _resolve_primary_root(tmp_path)

        assert result is None

    def test_returns_none_on_exception(self, tmp_path: Path) -> None:
        from sova.llm.provider import _resolve_primary_root

        with patch("subprocess.run", side_effect=OSError("git not found")):
            result = _resolve_primary_root(tmp_path)

        assert result is None


# ---------------------------------------------------------------------------
# Config: LLMConfig
# ---------------------------------------------------------------------------


class TestLLMConfig:
    def test_default_provider(self) -> None:
        from sova.config.models import LLMConfig

        cfg = LLMConfig()
        assert cfg.provider == "claude-code"
        assert cfg.model == ""
        assert cfg.fallback_model == ""
        assert cfg.api_base == ""

    def test_project_config_has_llm(self) -> None:
        from sova.config.models import LLMConfig, ProjectConfig

        cfg = ProjectConfig()
        assert isinstance(cfg.llm, LLMConfig)
        assert cfg.llm.provider == "claude-code"

    def test_load_from_toml(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config

        toml_file = tmp_path / "sova.toml"
        toml_file.write_text('[llm]\nprovider = "claude-code"\n')
        cfg = load_config(tmp_path)
        assert cfg.llm.provider == "claude-code"

    def test_litellm_config(self) -> None:
        from sova.config.models import LLMConfig

        cfg = LLMConfig(
            provider="litellm",
            model="gpt-4o",
            fallback_model="ollama/qwen3-coder:32b",
            api_base="http://localhost:4000",
        )
        assert cfg.provider == "litellm"
        assert cfg.model == "gpt-4o"
        assert cfg.fallback_model == "ollama/qwen3-coder:32b"

    def test_litellm_defaults_model(self) -> None:
        from sova.config.models import LLMConfig

        cfg = LLMConfig(provider="litellm")
        assert cfg.model == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------


class TestProviderInitFromConfig:
    def test_init_provider_from_config(self, tmp_path: Path) -> None:
        """Provider is initialized from config when _init_llm_provider is called."""
        from sova.cli.app import _init_llm_provider
        from sova.llm.client import get_provider
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        toml_file = tmp_path / "sova.toml"
        toml_file.write_text('[llm]\nprovider = "claude-code"\n')

        with patch("sova.cli.app.load_config") as mock_load:
            from sova.config.models import ProjectConfig

            mock_load.return_value = ProjectConfig(llm={"provider": "claude-code"})
            _init_llm_provider()

        provider = get_provider()
        assert isinstance(provider, ClaudeCodeProvider)

    def test_init_provider_unknown_raises(self, tmp_path: Path) -> None:
        """Unknown provider type raises ValueError (not silently swallowed)."""
        from sova.llm.provider import create_provider

        with pytest.raises(ValueError, match="Unknown LLM provider"):
            create_provider("nonexistent")


class TestModuleExports:
    def test_imports(self) -> None:
        from sova.llm import (
            LLMProvider,
            LLMResult,
            StreamEvent,
            create_provider,
            get_provider,
            invoke,
            invoke_command,
            invoke_streaming,
            record_cost,
            reset_provider,
            set_provider,
        )

        assert callable(invoke)
        assert callable(invoke_command)
        assert callable(invoke_streaming)
        assert callable(record_cost)
        assert callable(reset_provider)
        assert callable(create_provider)
        assert callable(get_provider)
        assert callable(set_provider)
        assert LLMResult is not None
        assert StreamEvent is not None
        assert LLMProvider is not None


# ---------------------------------------------------------------------------
# ClaudeCodeProvider
# ---------------------------------------------------------------------------


class TestClaudeCodeProvider:
    @pytest.fixture
    def mock_run(self):
        with patch("sova.llm.providers.claude_code.run", new_callable=AsyncMock) as mock:
            yield mock

    async def test_invoke(self, mock_run: AsyncMock) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=_make_cli_json(result_text="Provider output"),
            stderr="",
        )

        provider = ClaudeCodeProvider()
        result = await provider.invoke("Hello")

        assert result.text == "Provider output"
        assert result.cost_usd == Decimal("0.05")

    async def test_invoke_with_model(self, mock_run: AsyncMock) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=_make_cli_json(),
            stderr="",
        )

        provider = ClaudeCodeProvider()
        await provider.invoke("Hello", model="sonnet")

        call_args = mock_run.call_args[0]
        assert "--model" in call_args
        assert "sonnet" in call_args

    async def test_invoke_streaming(self, mock_run: AsyncMock) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        stream_lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}]}}),
            json.dumps(
                {
                    "type": "result",
                    "result": "Hi",
                    "stop_reason": "end_turn",
                    "session_id": "s1",
                    "total_cost_usd": 0.01,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                    "modelUsage": {"sonnet": {"costUSD": 0.01}},
                    "duration_ms": 500,
                }
            ),
        ]

        async def mock_readline():
            if stream_lines:
                return (stream_lines.pop(0) + "\n").encode()
            return b""

        mock_proc = AsyncMock()
        mock_proc.stdout.readline = mock_readline
        mock_proc.wait = AsyncMock()

        with patch(
            "sova.llm.providers.claude_code._start_streaming_process",
            return_value=mock_proc,
        ):
            provider = ClaudeCodeProvider()
            events = []
            async for event in provider.invoke_streaming("Hello"):
                events.append(event)

        assert any(e.type == "content" for e in events)
        assert any(e.type == "result" for e in events)

    async def test_check_available(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        provider = ClaudeCodeProvider()
        with patch("sova.llm.providers.claude_code.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ShellResult(returncode=0, stdout="1.0.0\n", stderr="")
            with patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"):
                available, detail = await provider.check_available()

        assert available is True
        assert "1.0.0" in detail

    def test_normalize_model_name(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        provider = ClaudeCodeProvider()
        assert provider.normalize_model_name("fast") == "sonnet"
        assert provider.normalize_model_name("smart") == "opus"
        assert provider.normalize_model_name("cheap") == "haiku"
        assert provider.normalize_model_name("custom-model") == "custom-model"


# ---------------------------------------------------------------------------
# LiteLLMProvider
# ---------------------------------------------------------------------------


class _MockUsage:
    def __init__(self, prompt_tokens: int = 100, completion_tokens: int = 50) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _MockMessage:
    def __init__(self, content: str = "Hello from LiteLLM") -> None:
        self.content = content


class _MockChoice:
    def __init__(self, content: str = "Hello from LiteLLM", finish_reason: str = "stop") -> None:
        self.message = _MockMessage(content)
        self.finish_reason = finish_reason
        self.delta = _MockMessage(content)


class _MockResponse:
    def __init__(
        self,
        content: str = "Hello from LiteLLM",
        model: str = "gpt-4o",
        prompt_tokens: int = 100,
        completion_tokens: int = 50,
    ) -> None:
        self.choices = [_MockChoice(content)]
        self.model = model
        self.usage = _MockUsage(prompt_tokens, completion_tokens)


class _MockStreamChunk:
    def __init__(self, content: str = "", model: str = "gpt-4o", usage: _MockUsage | None = None) -> None:
        delta = _MockMessage(content)
        choice = _MockChoice(content)
        choice.delta = delta
        self.choices = [choice]
        self.model = model
        self.usage = usage


class TestLiteLLMProvider:
    @pytest.fixture
    def mock_litellm(self):
        """Mock litellm at module level so the import check passes."""
        import importlib
        import sys

        mock_module = MagicMock()
        mock_module.acompletion = AsyncMock()
        mock_module.completion_cost = MagicMock(return_value=0.005)
        mock_module.cost_per_token = MagicMock(return_value=(0.003, 0.002))
        mock_module.__version__ = "1.0.0"

        old = sys.modules.get("litellm")
        sys.modules["litellm"] = mock_module

        import sova.llm.litellm_provider as llm_mod

        llm_mod.litellm = mock_module
        llm_mod._HAS_LITELLM = True

        yield mock_module

        if old is not None:
            sys.modules["litellm"] = old
        else:
            sys.modules.pop("litellm", None)
        importlib.reload(llm_mod)

    async def test_invoke_basic(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        mock_litellm.acompletion.return_value = _MockResponse(
            content="LiteLLM response",
            model="gpt-4o",
            prompt_tokens=100,
            completion_tokens=50,
        )

        provider = LiteLLMProvider(model="gpt-4o")
        result = await provider.invoke("Hello")

        assert result.text == "LiteLLM response"
        assert result.model == "gpt-4o"
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.cost_usd == Decimal("0.005")
        assert result.stop_reason == "end_turn"
        assert result.duration_ms >= 0

        mock_litellm.acompletion.assert_called_once()
        call_kwargs = mock_litellm.acompletion.call_args
        assert call_kwargs[1]["model"] == "gpt-4o"
        messages = call_kwargs[1]["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"

    async def test_invoke_with_model_override(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        mock_litellm.acompletion.return_value = _MockResponse(model="claude-sonnet-4-6")

        provider = LiteLLMProvider(model="gpt-4o")
        result = await provider.invoke("Hello", model="claude-sonnet-4-6")

        assert result.model == "claude-sonnet-4-6"
        call_kwargs = mock_litellm.acompletion.call_args
        assert call_kwargs[1]["model"] == "claude-sonnet-4-6"

    async def test_invoke_with_api_base(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        mock_litellm.acompletion.return_value = _MockResponse()

        provider = LiteLLMProvider(model="gpt-4o", api_base="http://localhost:4000")
        await provider.invoke("Hello")

        call_kwargs = mock_litellm.acompletion.call_args
        assert call_kwargs[1]["api_base"] == "http://localhost:4000"

    async def test_invoke_with_timeout(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        mock_litellm.acompletion.return_value = _MockResponse()

        provider = LiteLLMProvider(model="gpt-4o")
        await provider.invoke("Hello", timeout=30.0)

        call_kwargs = mock_litellm.acompletion.call_args
        assert call_kwargs[1]["timeout"] == 30.0

    async def test_fallback_on_failure(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        mock_litellm.acompletion.side_effect = [
            RuntimeError("Primary model unavailable"),
            _MockResponse(content="Fallback response", model="ollama/qwen3-coder"),
        ]

        provider = LiteLLMProvider(model="gpt-4o", fallback_model="ollama/qwen3-coder")
        result = await provider.invoke("Hello")

        assert result.text == "Fallback response"
        assert result.model == "ollama/qwen3-coder"
        assert mock_litellm.acompletion.call_count == 2

    async def test_no_fallback_raises(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        mock_litellm.acompletion.side_effect = RuntimeError("Model unavailable")

        provider = LiteLLMProvider(model="gpt-4o")
        with pytest.raises(RuntimeError, match="Model unavailable"):
            await provider.invoke("Hello")

    async def test_fallback_same_model_raises(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        mock_litellm.acompletion.side_effect = RuntimeError("Model unavailable")

        provider = LiteLLMProvider(model="gpt-4o", fallback_model="gpt-4o")
        with pytest.raises(RuntimeError, match="Model unavailable"):
            await provider.invoke("Hello")

    async def test_invoke_streaming(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        chunks = [
            _MockStreamChunk(content="Hello ", model="gpt-4o"),
            _MockStreamChunk(content="world", model="gpt-4o"),
            _MockStreamChunk(
                content="",
                model="gpt-4o",
                usage=_MockUsage(prompt_tokens=50, completion_tokens=20),
            ),
        ]

        async def mock_stream():
            for chunk in chunks:
                yield chunk

        mock_litellm.acompletion.return_value = mock_stream()

        provider = LiteLLMProvider(model="gpt-4o")
        events = []
        async for event in provider.invoke_streaming("Hello"):
            events.append(event)

        content_events = [e for e in events if e.type == "content"]
        assert len(content_events) == 2
        assert content_events[0].text == "Hello "
        assert content_events[1].text == "world"

        result_events = [e for e in events if e.type == "result"]
        assert len(result_events) == 1
        assert result_events[0].result is not None
        assert result_events[0].result.text == "Hello world"
        assert result_events[0].result.input_tokens == 50
        assert result_events[0].result.output_tokens == 20

    async def test_invoke_streaming_mid_stream_failure(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        chunks = [
            _MockStreamChunk(content="Hello ", model="gpt-4o"),
            _MockStreamChunk(content="world", model="gpt-4o"),
        ]

        async def mock_stream():
            for chunk in chunks:
                yield chunk
            raise RuntimeError("Connection lost mid-stream")

        mock_litellm.acompletion.return_value = mock_stream()

        provider = LiteLLMProvider(model="gpt-4o")
        events = []
        with pytest.raises(RuntimeError, match="Connection lost mid-stream"):
            async for event in provider.invoke_streaming("Hello"):
                events.append(event)

        content_events = [e for e in events if e.type == "content"]
        assert len(content_events) == 2
        assert content_events[0].text == "Hello "
        assert content_events[1].text == "world"

        result_events = [e for e in events if e.type == "result"]
        assert len(result_events) == 1
        assert result_events[0].result is not None
        assert result_events[0].result.text == "Hello world"
        assert result_events[0].result.stop_reason == "error"

    async def test_cost_tracking_fallback(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        mock_litellm.acompletion.return_value = _MockResponse()
        mock_litellm.completion_cost.side_effect = ValueError("Unknown model")

        provider = LiteLLMProvider(model="custom-model")
        result = await provider.invoke("Hello")

        assert result.cost_usd == Decimal("0")

    async def test_stop_reason_mapping(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        response = _MockResponse()
        response.choices[0].finish_reason = "length"
        mock_litellm.acompletion.return_value = response

        provider = LiteLLMProvider(model="gpt-4o")
        result = await provider.invoke("Hello")

        assert result.stop_reason == "length"

    def test_create_provider_litellm(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.provider import create_provider

        provider = create_provider("litellm")
        assert isinstance(provider, LiteLLMProvider)

    async def test_invoke_streaming_fallback(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        chunks = [
            _MockStreamChunk(content="Fallback ", model="ollama/qwen3-coder"),
            _MockStreamChunk(content="response", model="ollama/qwen3-coder"),
            _MockStreamChunk(
                content="",
                model="ollama/qwen3-coder",
                usage=_MockUsage(prompt_tokens=30, completion_tokens=10),
            ),
        ]

        async def mock_fallback_stream():
            for chunk in chunks:
                yield chunk

        # Primary model fails at acompletion() call, fallback succeeds
        mock_litellm.acompletion.side_effect = [
            RuntimeError("Primary model unavailable"),
            mock_fallback_stream(),
        ]

        provider = LiteLLMProvider(model="gpt-4o", fallback_model="ollama/qwen3-coder")
        events = []
        async for event in provider.invoke_streaming("Hello"):
            events.append(event)

        content_events = [e for e in events if e.type == "content"]
        assert len(content_events) == 2
        assert content_events[0].text == "Fallback "

        result_events = [e for e in events if e.type == "result"]
        assert len(result_events) == 1
        assert result_events[0].result is not None
        assert result_events[0].result.text == "Fallback response"
        assert mock_litellm.acompletion.call_count == 2

    async def test_invoke_streaming_no_fallback_raises(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        mock_litellm.acompletion.side_effect = RuntimeError("Model unavailable")

        provider = LiteLLMProvider(model="gpt-4o")
        with pytest.raises(RuntimeError, match="Model unavailable"):
            async for _ in provider.invoke_streaming("Hello"):
                pass

    async def test_create_provider_forwards_config(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.provider import create_provider

        provider = create_provider(
            "litellm",
            model="gpt-4o",
            fallback_model="ollama/qwen3-coder",
            api_base="http://localhost:4000",
        )
        assert isinstance(provider, LiteLLMProvider)
        assert provider.model == "gpt-4o"
        assert provider.fallback_model == "ollama/qwen3-coder"
        assert provider.api_base == "http://localhost:4000"

    async def test_check_available(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        provider = LiteLLMProvider(model="gpt-4o")
        available, detail = await provider.check_available()

        assert available is True
        assert "1.0.0" in detail


# ---------------------------------------------------------------------------
# _is_connection_error
# ---------------------------------------------------------------------------


class TestIsConnectionError:
    def test_connection_refused(self) -> None:
        from sova.llm.litellm_provider import _is_connection_error

        assert _is_connection_error(ConnectionRefusedError("refused")) is True

    def test_connection_error(self) -> None:
        from sova.llm.litellm_provider import _is_connection_error

        assert _is_connection_error(ConnectionError("failed")) is True

    def test_wrapped_connection_error(self) -> None:
        from sova.llm.litellm_provider import _is_connection_error

        inner = ConnectionRefusedError("refused")
        outer = RuntimeError("wrapper")
        outer.__cause__ = inner
        assert _is_connection_error(outer) is True

    def test_connection_refused_in_message(self) -> None:
        from sova.llm.litellm_provider import _is_connection_error

        assert _is_connection_error(RuntimeError("Connection refused by server")) is True

    def test_unrelated_error(self) -> None:
        from sova.llm.litellm_provider import _is_connection_error

        assert _is_connection_error(ValueError("bad value")) is False

    def test_api_error_not_connection(self) -> None:
        from sova.llm.litellm_provider import _is_connection_error

        assert _is_connection_error(RuntimeError("Model not found")) is False


# ---------------------------------------------------------------------------
# LLMConfig hybrid provider
# ---------------------------------------------------------------------------


class TestHybridConfig:
    def test_hybrid_provider_literal_accepted(self) -> None:
        from sova.config.models import LLMConfig

        cfg = LLMConfig(provider="hybrid")
        assert cfg.provider == "hybrid"
        assert cfg.model == "claude-sonnet-4-6"

    def test_hybrid_defaults_model(self) -> None:
        from sova.config.models import LLMConfig

        cfg = LLMConfig(provider="hybrid", model="")
        assert cfg.model == "claude-sonnet-4-6"

    def test_hybrid_preserves_explicit_model(self) -> None:
        from sova.config.models import LLMConfig

        cfg = LLMConfig(provider="hybrid", model="gpt-4o")
        assert cfg.model == "gpt-4o"


# ---------------------------------------------------------------------------
# Doctor: Ollama check
# ---------------------------------------------------------------------------


class TestDoctorOllamaCheck:
    async def test_no_ollama_models_returns_empty(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_ollama

        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value.llm.routing = {"trivial": "haiku"}
            checks = await _check_ollama(tmp_path)
            assert checks == []

    async def test_ollama_not_installed(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_ollama

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.cli.commands.doctor.shutil.which", return_value=None),
        ):
            mock_cfg.return_value.llm.routing = {"triage": "ollama/qwen3:8b"}
            checks = await _check_ollama(tmp_path)
            assert len(checks) == 1
            assert checks[0][0] == "ollama CLI"
            assert checks[0][1] is False

    async def test_ollama_not_running(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_ollama

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.cli.commands.doctor.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.cli.commands.doctor.run", new_callable=AsyncMock) as mock_run,
        ):
            mock_cfg.return_value.llm.routing = {"triage": "ollama/qwen3:8b"}
            mock_run.return_value = MagicMock(success=False, stdout="")
            checks = await _check_ollama(tmp_path)
            assert any(c[0] == "ollama running" and c[1] is False for c in checks)

    async def test_ollama_model_installed(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_ollama

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.cli.commands.doctor.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.cli.commands.doctor.run", new_callable=AsyncMock) as mock_run,
        ):
            mock_cfg.return_value.llm.routing = {"triage": "ollama/qwen3:8b"}
            mock_run.return_value = MagicMock(
                success=True,
                stdout="NAME\tID\tSIZE\tMODIFIED\nqwen3:8b\tabc123\t5.0 GB\t2 hours ago\n",
            )
            checks = await _check_ollama(tmp_path)
            assert any(c[0] == "ollama running" and c[1] is True for c in checks)
            model_check = [c for c in checks if "qwen3" in c[0]]
            assert model_check
            assert model_check[0][1] is True

    async def test_ollama_model_not_pulled(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_ollama

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.cli.commands.doctor.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.cli.commands.doctor.run", new_callable=AsyncMock) as mock_run,
        ):
            mock_cfg.return_value.llm.routing = {"triage": "ollama/qwen3:8b"}
            mock_run.return_value = MagicMock(
                success=True,
                stdout="NAME\tID\tSIZE\tMODIFIED\nllama3:8b\tabc123\t5.0 GB\t2 hours ago\n",
            )
            checks = await _check_ollama(tmp_path)
            model_check = [c for c in checks if "qwen3" in c[0]]
            assert model_check
            assert model_check[0][1] is False
            assert "ollama pull" in model_check[0][2]


# ---------------------------------------------------------------------------
# Complexity scorer
# ---------------------------------------------------------------------------


class TestComplexityScorer:
    """Tests for sova.llm.complexity module."""

    def test_trivial_keywords(self) -> None:
        assert assess_complexity("fix typo in README", "") == ComplexityTier.TRIVIAL
        assert assess_complexity("rename variable foo", "") == ComplexityTier.TRIVIAL
        assert assess_complexity("bump version to 1.2.3", "") == ComplexityTier.TRIVIAL

    def test_simple_keywords(self) -> None:
        assert assess_complexity("add test for utils", "") == ComplexityTier.SIMPLE
        assert assess_complexity("minor fix in parser", "") == ComplexityTier.SIMPLE

    def test_moderate_keywords(self) -> None:
        assert assess_complexity("new endpoint for user search", "") == ComplexityTier.MODERATE

    def test_complex_keywords(self) -> None:
        assert assess_complexity("refactor auth module", "") == ComplexityTier.COMPLEX
        assert assess_complexity("migrate database schema", "") == ComplexityTier.COMPLEX
        assert assess_complexity("new module for notifications", "") == ComplexityTier.COMPLEX

    def test_epic_keywords(self) -> None:
        assert assess_complexity("cross-cutting concern overhaul", "") == ComplexityTier.EPIC
        assert assess_complexity("full rewrite of the pipeline", "") == ComplexityTier.EPIC

    def test_empty_input_defaults_to_moderate(self) -> None:
        assert assess_complexity("", "") == ComplexityTier.MODERATE

    def test_label_based_routing(self) -> None:
        result = assess_complexity("do something", "", labels=["good first issue"])
        assert result == ComplexityTier.TRIVIAL

    def test_label_easy(self) -> None:
        result = assess_complexity("do something", "", labels=["easy"])
        assert result == ComplexityTier.SIMPLE

    def test_file_count_influence(self) -> None:
        result = assess_complexity("fix a thing", "short desc", file_count_estimate=1)
        assert result == ComplexityTier.TRIVIAL

        result = assess_complexity("fix a thing", "short desc", file_count_estimate=20)
        assert result == ComplexityTier.COMPLEX

    def test_file_count_zero_treated_as_no_signal(self) -> None:
        result_with_zero = assess_complexity("fix typo", "", file_count_estimate=0)
        result_without = assess_complexity("fix typo", "")
        assert result_with_zero == result_without

    def test_conflicting_signals_keyword_wins_over_length(self) -> None:
        long_desc = "x " * 3000
        result = assess_complexity("fix typo", long_desc)
        assert result == ComplexityTier.TRIVIAL

    def test_low_keyword_vs_high_multi_signals(self) -> None:
        """Multiple strong signals (label + file count) override a misleading keyword."""
        result = assess_complexity(
            "Rename config",
            "Update 50 modules across the codebase",
            labels=["complex"],
            file_count_estimate=50,
        )
        assert result in (ComplexityTier.COMPLEX, ComplexityTier.EPIC)

    def test_description_length_as_signal(self) -> None:
        short = assess_complexity("do task", "short")
        long_ = assess_complexity("do task", "a " * 2500)
        tiers = list(ComplexityTier)
        assert tiers.index(short) <= tiers.index(long_)

    def test_enum_values_match_task_assessment(self) -> None:
        expected = {"trivial", "simple", "moderate", "complex", "epic"}
        actual = {t.value for t in ComplexityTier}
        assert actual == expected

    def test_multiple_signals_combined(self) -> None:
        result = assess_complexity(
            "refactor the auth system",
            "This is a large refactor affecting many files",
            labels=["complex"],
            file_count_estimate=10,
        )
        assert result == ComplexityTier.COMPLEX

    def test_none_labels_accepted(self) -> None:
        assess_complexity("title", "desc", labels=None)

    def test_none_file_count_accepted(self) -> None:
        assess_complexity("title", "desc", file_count_estimate=None)


# ---------------------------------------------------------------------------
# Anthropic rate card (compute_anthropic_cost)
# ---------------------------------------------------------------------------


class TestAnthropicRateCard:
    def test_known_model_cost(self) -> None:
        from sova.llm.models import compute_anthropic_cost

        cost = compute_anthropic_cost("claude-sonnet-5", input_tokens=1000, output_tokens=500)
        expected = Decimal("2") * 1000 / Decimal("1000000") + Decimal("10") * 500 / Decimal("1000000")
        assert cost == expected.quantize(Decimal("0.000001"))

    def test_unknown_model_returns_zero(self) -> None:
        from sova.llm.models import compute_anthropic_cost

        cost = compute_anthropic_cost("gpt-4o", input_tokens=1000, output_tokens=500)
        assert cost == Decimal("0")

    def test_prefix_matching(self) -> None:
        from sova.llm.models import compute_anthropic_cost

        cost = compute_anthropic_cost("claude-sonnet-5-20260101", input_tokens=1000, output_tokens=500)
        assert cost > Decimal("0")

    def test_cache_tokens(self) -> None:
        from sova.llm.models import compute_anthropic_cost

        cost_no_cache = compute_anthropic_cost("claude-sonnet-5", input_tokens=1000, output_tokens=100)
        cost_with_cache = compute_anthropic_cost(
            "claude-sonnet-5",
            input_tokens=1000,
            output_tokens=100,
            cache_read_tokens=500,
            cache_creation_tokens=0,
        )
        assert cost_with_cache < cost_no_cache

    def test_cache_creation_tokens_increase_cost(self) -> None:
        from sova.llm.models import compute_anthropic_cost

        cost_no_cache = compute_anthropic_cost("claude-sonnet-5", input_tokens=1000, output_tokens=100)
        cost_with_creation = compute_anthropic_cost(
            "claude-sonnet-5",
            input_tokens=1000,
            output_tokens=100,
            cache_creation_tokens=500,
        )
        assert cost_with_creation > cost_no_cache

    def test_zero_tokens(self) -> None:
        from sova.llm.models import compute_anthropic_cost

        cost = compute_anthropic_cost("claude-sonnet-5", input_tokens=0, output_tokens=0)
        assert cost == Decimal("0")

    def test_opus_rates(self) -> None:
        from sova.llm.models import compute_anthropic_cost

        cost = compute_anthropic_cost("claude-opus-5", input_tokens=1_000_000, output_tokens=0)
        assert cost == Decimal("5.000000")

    def test_haiku_rates(self) -> None:
        from sova.llm.models import compute_anthropic_cost

        cost = compute_anthropic_cost("claude-haiku-4-5-20251001", input_tokens=1_000_000, output_tokens=1_000_000)
        expected = Decimal("1") + Decimal("5")
        assert cost == expected.quantize(Decimal("0.000001"))

    def test_fable_rates(self) -> None:
        from sova.llm.models import compute_anthropic_cost

        cost = compute_anthropic_cost("claude-fable-5", input_tokens=1_000_000, output_tokens=0)
        assert cost == Decimal("10.000000")

    def test_batch_result_succeeded_with_error(self) -> None:
        from sova.llm.models import BatchRequest, BatchResult

        req = BatchRequest(custom_id="test", prompt="hello")
        result = BatchResult(request=req, error="something went wrong")
        assert result.succeeded is False


# ---------------------------------------------------------------------------
# AnthropicAPIProvider
# ---------------------------------------------------------------------------


class _MockAnthropicUsage:
    def __init__(
        self,
        input_tokens: int = 100,
        output_tokens: int = 50,
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class _MockTextBlock:
    def __init__(self, text: str = "Hello") -> None:
        self.type = "text"
        self.text = text


class _MockAnthropicResponse:
    def __init__(
        self,
        text: str = "Hello from Anthropic",
        model: str = "claude-sonnet-5",
        input_tokens: int = 100,
        output_tokens: int = 50,
    ) -> None:
        self.content = [_MockTextBlock(text)]
        self.model = model
        self.usage = _MockAnthropicUsage(input_tokens=input_tokens, output_tokens=output_tokens)
        self.stop_reason = "end_turn"


class TestAnthropicAPIProvider:
    @pytest.fixture
    def mock_anthropic(self):
        """Mock anthropic at module level so the import check passes."""
        import importlib
        import sys

        mock_module = MagicMock()
        mock_module.__version__ = "0.39.0"
        mock_client = AsyncMock()
        mock_module.AsyncAnthropic.return_value = mock_client

        old = sys.modules.get("anthropic")
        sys.modules["anthropic"] = mock_module

        import sova.llm.providers.anthropic_api as api_mod

        api_mod.anthropic = mock_module
        api_mod._HAS_ANTHROPIC = True

        yield mock_module

        if old is not None:
            sys.modules["anthropic"] = old
        else:
            sys.modules.pop("anthropic", None)
        importlib.reload(api_mod)

    async def test_invoke_basic(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.create = AsyncMock(
            return_value=_MockAnthropicResponse(text="API response", model="claude-sonnet-5"),
        )

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            result = await provider.invoke("Hello")

        assert result.text == "API response"
        assert result.model == "claude-sonnet-5"
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.cost_usd > Decimal("0")
        assert result.stop_reason == "end_turn"
        assert result.duration_ms >= 0

        client.messages.create.assert_called_once()
        call_kwargs = client.messages.create.call_args[1]
        assert call_kwargs["model"] == "claude-sonnet-5"
        assert call_kwargs["messages"] == [{"role": "user", "content": "Hello"}]

    async def test_invoke_with_system_prompt(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.create = AsyncMock(return_value=_MockAnthropicResponse())

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            await provider.invoke("Hello", system_prompt="Be helpful")

        call_kwargs = client.messages.create.call_args[1]
        assert call_kwargs["system"] == "Be helpful"

    async def test_invoke_with_model_override(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.create = AsyncMock(
            return_value=_MockAnthropicResponse(model="claude-opus-5"),
        )

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            await provider.invoke("Hello", model="claude-opus-5")

        call_kwargs = client.messages.create.call_args[1]
        assert call_kwargs["model"] == "claude-opus-5"

    async def test_invoke_resolves_aliases(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.create = AsyncMock(return_value=_MockAnthropicResponse())

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            await provider.invoke("Hello", model="sonnet")

        call_kwargs = client.messages.create.call_args[1]
        assert call_kwargs["model"] == "claude-sonnet-5"

    async def test_invoke_missing_api_key(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            provider = AnthropicAPIProvider()
            with pytest.raises(RuntimeError, match="API key is not configured"):
                await provider.invoke("Hello")

    async def test_invoke_uses_explicit_api_key(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.create = AsyncMock(return_value=_MockAnthropicResponse())

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            provider = AnthropicAPIProvider(api_key="sk-explicit-key")
            await provider.invoke("Hello")

        mock_anthropic.AsyncAnthropic.assert_called_once_with(api_key="sk-explicit-key")

    async def test_explicit_key_overrides_env(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.create = AsyncMock(return_value=_MockAnthropicResponse())

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-env-key"}):
            provider = AnthropicAPIProvider(api_key="sk-explicit-key")
            await provider.invoke("Hello")

        mock_anthropic.AsyncAnthropic.assert_called_once_with(api_key="sk-explicit-key")

    async def test_check_available_explicit_key(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.models.list = AsyncMock(return_value=[])

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            provider = AnthropicAPIProvider(api_key="sk-explicit-key")
            ok, msg = await provider.check_available()
            assert ok is True
            assert "anthropic SDK" in msg

    async def test_invoke_api_error_sanitized(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.create = AsyncMock(
            side_effect=Exception("auth failed for key sk-secret-123"),
        )

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-secret-123"}):
            provider = AnthropicAPIProvider()
            with pytest.raises(RuntimeError, match="Anthropic API error") as exc_info:
                await provider.invoke("Hello")
            assert "sk-secret-123" not in str(exc_info.value)

    async def test_check_available_no_sdk(self) -> None:
        import sova.llm.providers.anthropic_api as api_mod

        original = api_mod._HAS_ANTHROPIC
        api_mod._HAS_ANTHROPIC = False
        try:
            from sova.llm.providers.anthropic_api import AnthropicAPIProvider

            with pytest.raises(ImportError, match="anthropic is not installed"):
                AnthropicAPIProvider()
        finally:
            api_mod._HAS_ANTHROPIC = original

    async def test_check_available_no_key(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            provider = AnthropicAPIProvider()
            ok, msg = await provider.check_available()
            assert ok is False
            assert "ANTHROPIC_API_KEY" in msg

    async def test_check_available_success(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.models.list = AsyncMock(return_value=[])

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            ok, msg = await provider.check_available()
            assert ok is True
            assert "anthropic SDK" in msg

    async def test_check_available_auth_error(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        mock_anthropic.AsyncAnthropic.return_value.messages.create = AsyncMock(
            side_effect=Exception("invalid api key"),
        )

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            ok, msg = await provider.check_available()
            assert ok is False
            assert "unavailable" in msg.lower()

    async def test_check_available_no_models_attr(self, mock_anthropic: MagicMock) -> None:
        """Check available makes a real API call to validate credentials."""
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        # Mock the messages.create call to return successfully
        mock_anthropic.AsyncAnthropic.return_value.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(type="text", text="test")])
        )

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            ok, msg = await provider.check_available()
            assert ok is True
            assert "anthropic SDK" in msg

    def test_create_provider_forwards_api_key(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.provider import create_provider

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            provider = create_provider("anthropic", model="claude-opus-5", api_key="sk-from-config")

        assert provider._api_key == "sk-from-config"
        assert provider._default_model == "claude-opus-5"

    def test_normalize_model_name(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
        assert provider.normalize_model_name("sonnet") == "claude-sonnet-5"
        assert provider.normalize_model_name("opus") == "claude-opus-5"
        assert provider.normalize_model_name("haiku") == "claude-haiku-4-5-20251001"
        assert provider.normalize_model_name("fast") == "claude-sonnet-5"
        assert provider.normalize_model_name("smart") == "claude-opus-5"
        assert provider.normalize_model_name("cheap") == "claude-haiku-4-5-20251001"
        assert provider.normalize_model_name("claude-opus-5") == "claude-opus-5"

    async def test_invoke_with_max_tokens(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.create = AsyncMock(return_value=_MockAnthropicResponse())

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            await provider.invoke("Hello", max_tokens=8192)

        call_kwargs = client.messages.create.call_args[1]
        assert call_kwargs["max_tokens"] == 8192

    async def test_invoke_with_timeout(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.create = AsyncMock(return_value=_MockAnthropicResponse())

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            await provider.invoke("Hello", timeout=30.0)

        call_kwargs = client.messages.create.call_args[1]
        assert call_kwargs["timeout"] == 30.0

    async def test_invoke_streaming_basic(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        msg_usage = MagicMock(input_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        message_start_event = MagicMock(type="message_start")
        message_start_event.message = MagicMock(model="claude-sonnet-5", usage=msg_usage)

        delta1 = MagicMock(type="content_block_delta")
        delta1.delta = MagicMock(text="Hello ")

        delta2 = MagicMock(type="content_block_delta")
        delta2.delta = MagicMock(text="world")

        delta_usage = MagicMock(output_tokens=50)
        message_delta = MagicMock(type="message_delta")
        message_delta.delta = MagicMock(stop_reason="end_turn")
        message_delta.usage = delta_usage

        async def mock_events():
            for e in [message_start_event, delta1, delta2, message_delta]:
                yield e

        stream_ctx = AsyncMock()
        stream_ctx.__aenter__ = AsyncMock(return_value=stream_ctx)
        stream_ctx.__aexit__ = AsyncMock(return_value=False)
        stream_ctx.__aiter__ = lambda self: mock_events()

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.stream = MagicMock(return_value=stream_ctx)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            events = []
            async for event in provider.invoke_streaming("Hello"):
                events.append(event)

        content_events = [e for e in events if e.type == "content"]
        result_events = [e for e in events if e.type == "result"]
        assert len(content_events) == 2
        assert content_events[0].text == "Hello "
        assert content_events[1].text == "world"
        assert len(result_events) == 1
        assert result_events[0].result is not None
        assert result_events[0].result.text == "Hello world"
        assert result_events[0].result.input_tokens == 100
        assert result_events[0].result.output_tokens == 50
        assert result_events[0].result.stop_reason == "end_turn"

    async def test_invoke_streaming_error(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        async def mock_events():
            yield MagicMock(type="content_block_delta", delta=MagicMock(text="partial"))
            raise Exception("stream broke")

        stream_ctx = AsyncMock()
        stream_ctx.__aenter__ = AsyncMock(return_value=stream_ctx)
        stream_ctx.__aexit__ = AsyncMock(return_value=False)
        stream_ctx.__aiter__ = lambda self: mock_events()

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.stream = MagicMock(return_value=stream_ctx)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            with pytest.raises(RuntimeError, match="Anthropic streaming error"):
                events = []
                async for event in provider.invoke_streaming("Hello"):
                    events.append(event)

        result_events = [e for e in events if e.type == "result"]
        assert len(result_events) == 1
        assert result_events[0].result.stop_reason == "error"
        assert result_events[0].result.text == "partial"

    async def test_check_available_sdk_false_on_instance(self, mock_anthropic: MagicMock) -> None:
        """check_available returns False when _HAS_ANTHROPIC is set to False after construction."""
        import sova.llm.providers.anthropic_api as api_mod
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            api_mod._HAS_ANTHROPIC = False
            try:
                ok, msg = await provider.check_available()
                assert ok is False
                assert "not installed" in msg
            finally:
                api_mod._HAS_ANTHROPIC = True

    async def test_invoke_streaming_no_usage(self, mock_anthropic: MagicMock) -> None:
        """Stream events without usage info still produce a valid result."""
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        delta = MagicMock(type="content_block_delta")
        delta.delta = MagicMock(text="hello")

        unknown_event = MagicMock(type="unknown_event")

        async def mock_events():
            for e in [delta, unknown_event]:
                yield e

        stream_ctx = AsyncMock()
        stream_ctx.__aenter__ = AsyncMock(return_value=stream_ctx)
        stream_ctx.__aexit__ = AsyncMock(return_value=False)
        stream_ctx.__aiter__ = lambda self: mock_events()

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.stream = MagicMock(return_value=stream_ctx)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            events = []
            async for event in provider.invoke_streaming("test"):
                events.append(event)

        result_events = [e for e in events if e.type == "result"]
        assert len(result_events) == 1
        assert result_events[0].result.text == "hello"
        assert result_events[0].result.input_tokens == 0
        assert result_events[0].result.output_tokens == 0

    async def test_invoke_streaming_with_system_prompt(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        async def mock_events():
            delta = MagicMock(type="content_block_delta")
            delta.delta = MagicMock(text="ok")
            yield delta

        stream_ctx = AsyncMock()
        stream_ctx.__aenter__ = AsyncMock(return_value=stream_ctx)
        stream_ctx.__aexit__ = AsyncMock(return_value=False)
        stream_ctx.__aiter__ = lambda self: mock_events()

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.stream = MagicMock(return_value=stream_ctx)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            events = []
            async for event in provider.invoke_streaming("Hello", system_prompt="Be helpful"):
                events.append(event)

        call_kwargs = client.messages.stream.call_args[1]
        assert call_kwargs["system"] == "Be helpful"

    async def test_invoke_streaming_with_max_tokens(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        async def mock_events():
            delta = MagicMock(type="content_block_delta")
            delta.delta = MagicMock(text="ok")
            yield delta

        stream_ctx = AsyncMock()
        stream_ctx.__aenter__ = AsyncMock(return_value=stream_ctx)
        stream_ctx.__aexit__ = AsyncMock(return_value=False)
        stream_ctx.__aiter__ = lambda self: mock_events()

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.stream = MagicMock(return_value=stream_ctx)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            events = []
            async for event in provider.invoke_streaming("Hello", max_tokens=8192):
                events.append(event)

        call_kwargs = client.messages.stream.call_args[1]
        assert call_kwargs["max_tokens"] == 8192

    async def test_invoke_streaming_with_timeout(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        async def mock_events():
            delta = MagicMock(type="content_block_delta")
            delta.delta = MagicMock(text="ok")
            yield delta

        stream_ctx = AsyncMock()
        stream_ctx.__aenter__ = AsyncMock(return_value=stream_ctx)
        stream_ctx.__aexit__ = AsyncMock(return_value=False)
        stream_ctx.__aiter__ = lambda self: mock_events()

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.stream = MagicMock(return_value=stream_ctx)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            events = []
            async for event in provider.invoke_streaming("Hello", timeout=30.0):
                events.append(event)

        call_kwargs = client.messages.stream.call_args[1]
        assert call_kwargs["timeout"] == 30.0

    async def test_invoke_streaming_error_sanitized(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.stream = MagicMock(
            side_effect=Exception("auth failed for key sk-secret-456"),
        )

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-secret-456"}):
            provider = AnthropicAPIProvider()
            with pytest.raises(RuntimeError, match="Anthropic streaming error") as exc_info:
                async for _ in provider.invoke_streaming("Hello"):
                    pass
            assert "sk-secret-456" not in str(exc_info.value)

    async def test_invoke_non_text_content_blocks(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        class _MockToolUseBlock:
            type = "tool_use"
            id = "call_123"
            name = "my_tool"
            input = {}

        response = _MockAnthropicResponse(text="Hello")
        response.content = [_MockToolUseBlock()]

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.create = AsyncMock(return_value=response)

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            result = await provider.invoke("Hello")

        assert result.text == ""
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.cost_usd > Decimal("0")


class TestCreateProviderAnthropic:
    def test_create_provider_anthropic(self) -> None:
        from sova.llm.provider import create_provider

        with (
            patch.dict("sys.modules", {"anthropic": MagicMock(__version__="0.39.0")}),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}),
        ):
            import sova.llm.providers.anthropic_api as api_mod

            api_mod._HAS_ANTHROPIC = True
            api_mod.anthropic = MagicMock()

            from sova.llm.providers.anthropic_api import AnthropicAPIProvider

            provider = create_provider("anthropic")
            assert isinstance(provider, AnthropicAPIProvider)

    def test_create_provider_anthropic_in_available_list(self) -> None:
        from sova.llm.provider import create_provider

        with pytest.raises(ValueError, match="anthropic") as exc_info:
            create_provider("nonexistent")
        assert "anthropic" in str(exc_info.value)


class TestLLMConfigAnthropic:
    def test_anthropic_provider_accepted(self) -> None:
        from sova.config.models import LLMConfig

        cfg = LLMConfig(provider="anthropic")
        assert cfg.provider == "anthropic"

    def test_default_still_claude_code(self) -> None:
        from sova.config.models import LLMConfig

        cfg = LLMConfig()
        assert cfg.provider == "claude-code"


# ---------------------------------------------------------------------------
# Client: classify_content_type()
# ---------------------------------------------------------------------------


class TestClassifyContentType:
    def test_diff(self) -> None:
        from sova.llm.client import classify_content_type

        assert classify_content_type("diff --git a/f b/f\n@@ -1 +1 @@") == "diff"

    def test_diff_hunk_header(self) -> None:
        from sova.llm.client import classify_content_type

        assert classify_content_type("@@ -1,2 +1,2 @@ context") == "diff"

    def test_json_object(self) -> None:
        from sova.llm.client import classify_content_type

        assert classify_content_type('{"key": "value"}') == "json"

    def test_json_array(self) -> None:
        from sova.llm.client import classify_content_type

        assert classify_content_type("[1, 2, 3]") == "json"

    def test_code(self) -> None:
        from sova.llm.client import classify_content_type

        assert classify_content_type("def foo():\n    return 1") == "code"

    @pytest.mark.parametrize(
        "prefix",
        [
            "def foo():",
            "class Bar:",
            "import sys",
            "from typing import List",
            "function doSomething() {",
            "const x = 1;",
            "public class Main {",
            "package main",
            "#include <stdio.h>",
        ],
    )
    def test_code_all_prefixes(self, prefix: str) -> None:
        """Verify all code prefixes in _CODE_PREFIXES are detected."""
        from sova.llm.client import classify_content_type

        assert classify_content_type(prefix) == "code"

    def test_text_default(self) -> None:
        from sova.llm.client import classify_content_type

        assert classify_content_type("Just some prose describing a task.") == "text"

    def test_short_payload_safe(self) -> None:
        from sova.llm.client import classify_content_type

        assert classify_content_type("hi") == "text"

    def test_empty_payload_safe(self) -> None:
        from sova.llm.client import classify_content_type

        assert classify_content_type("") == "text"

    def test_leading_whitespace_stripped(self) -> None:
        from sova.llm.client import classify_content_type

        assert classify_content_type('   {"a": 1}') == "json"

    def test_only_prefix_inspected(self) -> None:
        from sova.llm.client import classify_content_type

        # A JSON marker beyond the first 100 chars must not flip the result.
        assert classify_content_type("x" * 200 + "{") == "text"


# ---------------------------------------------------------------------------
# Client: maybe_compress()
# ---------------------------------------------------------------------------


class TestMaybeCompress:
    def _cfg(self, *, enabled: bool):
        from sova.config.models import HeadroomConfig, ProjectConfig

        return ProjectConfig(compression=HeadroomConfig(enabled=enabled))

    def test_config_none_returns_prompt(self) -> None:
        from sova.llm import client

        with patch.object(client, "_try_load_config", return_value=None):
            assert client.maybe_compress("hello") == "hello"

    def test_disabled_returns_prompt(self) -> None:
        from sova.llm import client

        with patch.object(client, "_try_load_config", return_value=self._cfg(enabled=False)):
            assert client.maybe_compress("x" * 100) == "x" * 100

    def test_disabled_does_not_import_compression(self) -> None:
        from sova.llm import client

        with (
            patch.object(client, "_try_load_config", return_value=self._cfg(enabled=False)),
            patch("sova.llm.compression.compress") as mock_compress,
        ):
            client.maybe_compress("x" * 100)
        mock_compress.assert_not_called()

    def test_enabled_calls_compress_with_content_type(self) -> None:
        from sova.llm import client

        payload = '{"key": ' + '"' + "v" * 100 + '"}'
        with (
            patch.object(client, "_try_load_config", return_value=self._cfg(enabled=True)),
            patch("sova.llm.compression.compress", return_value="COMPRESSED") as mock_compress,
        ):
            assert client.maybe_compress(payload) == "COMPRESSED"
        mock_compress.assert_called_once_with(payload, content_type="json", cwd=None)

    def test_error_returns_prompt(self) -> None:
        from sova.llm import client

        with (
            patch.object(client, "_try_load_config", return_value=self._cfg(enabled=True)),
            patch("sova.llm.compression.compress", side_effect=RuntimeError("boom")),
        ):
            assert client.maybe_compress("hello world") == "hello world"


# ---------------------------------------------------------------------------
# Client: compression wiring into entry points
# ---------------------------------------------------------------------------


class TestCompressionWiring:
    async def test_invoke_compresses_prompt(self) -> None:
        from sova.llm import client

        provider = MagicMock()
        provider.invoke = AsyncMock(return_value=LLMResult(text="ok", model="test"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "maybe_compress", return_value="COMPRESSED") as mock_compress,
            patch.object(client, "_try_load_config", return_value=None),
        ):
            await client.invoke("original prompt")
        mock_compress.assert_called_once()
        assert provider.invoke.call_args[0][0] == "COMPRESSED"

    async def test_invoke_preserves_system_prompt_uncompressed(self) -> None:
        from sova.llm import client

        provider = MagicMock()
        provider.invoke = AsyncMock(return_value=LLMResult(text="ok", model="test"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "maybe_compress", return_value="COMPRESSED") as mock_compress,
            patch.object(client, "_try_load_config", return_value=None),
        ):
            await client.invoke("user prompt", system_prompt="system prompt")
        mock_compress.assert_called_once_with("user prompt", None)
        assert provider.invoke.call_args.kwargs["system_prompt"] == "system prompt"

    async def test_invoke_command_compresses_args_only(self) -> None:
        from sova.llm import client

        provider = MagicMock()
        provider.invoke_command = AsyncMock(return_value=LLMResult(text="ok", model="test"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "maybe_compress", return_value="COMPRESSED_ARGS") as mock_compress,
        ):
            await client.invoke_command("/develop", args="42")
        mock_compress.assert_called_once_with("42", None)
        assert provider.invoke_command.call_args[0][0] == "/develop"
        assert provider.invoke_command.call_args[0][1] == "COMPRESSED_ARGS"

    async def test_invoke_command_no_args_skips_compression(self) -> None:
        from sova.llm import client

        provider = MagicMock()
        provider.invoke_command = AsyncMock(return_value=LLMResult(text="ok", model="test"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "maybe_compress") as mock_compress,
        ):
            await client.invoke_command("/review")
        mock_compress.assert_not_called()

    async def test_invoke_batch_compresses_each_without_mutating(self) -> None:
        from sova.llm import client
        from sova.llm.models import BatchRequest

        provider = MagicMock()
        provider.invoke_batch = AsyncMock(return_value=[])
        reqs = [
            BatchRequest(custom_id="a", prompt="p1"),
            BatchRequest(custom_id="b", prompt="p2"),
        ]
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "maybe_compress", side_effect=lambda p, cwd=None: p.upper()) as mock_compress,
            patch("sova.llm.providers.anthropic_batch.create_batch_provider", return_value=None),
        ):
            await client.invoke_batch(reqs)
        assert mock_compress.call_count == 2
        sent = provider.invoke_batch.call_args[0][0]
        assert [r.prompt for r in sent] == ["P1", "P2"]
        # Caller-supplied requests must not be mutated in place.
        assert [r.prompt for r in reqs] == ["p1", "p2"]

    async def test_invoke_batch_empty_list_skips_compression(self) -> None:
        """Verify empty batch returns early without compression or config loading."""
        from sova.llm import client

        with patch.object(client, "maybe_compress") as mock_compress:
            result = await client.invoke_batch([])
        assert result == []
        mock_compress.assert_not_called()

    async def test_invoke_streaming_compresses_prompt(self) -> None:
        from sova.llm import client

        provider = MagicMock()
        captured: dict[str, str] = {}

        async def fake_stream(prompt: str, **_kwargs: object):
            captured["prompt"] = prompt
            if False:
                yield  # pragma: no cover - makes this an async generator

        provider.invoke_streaming = fake_stream
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "maybe_compress", return_value="COMPRESSED") as mock_compress,
        ):
            async for _event in client.invoke_streaming("original prompt"):
                pass
        mock_compress.assert_called_once()
        assert captured["prompt"] == "COMPRESSED"
