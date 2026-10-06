"""Tests for SOVA LLM interaction layer."""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from sova.llm import ComplexityTier, assess_complexity
from sova.llm.models import BatchRequest, LLMResult, StreamEvent, resolve_model_alias

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

        # Verify CLI args: prompt is sent via stdin, never on argv
        call_args = mock_run.call_args[0]
        assert "claude" in call_args
        assert "-p" in call_args
        assert "Say hello" not in call_args
        assert "--output-format" in call_args
        assert "json" in call_args
        assert mock_run.call_args.kwargs.get("stdin") == "Say hello"

    async def test_invoke_with_model(self, mock_run: AsyncMock) -> None:
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=_make_cli_json(),
            stderr="",
        )

        await invoke("Hello", model="sonnet")

        # "sonnet" is resolved by SOVA itself on the firstParty backend
        # (issue #1033), not sent to the CLI bare.
        call_args = mock_run.call_args[0]
        assert "--model" in call_args
        assert resolve_model_alias("sonnet") in call_args

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

    async def test_invoke_cli_failure_raises_typed_error(self, mock_run: AsyncMock) -> None:
        """A model availability rejection classifies as ModelUnavailableError."""
        from sova.llm.client import invoke
        from sova.llm.errors import ModelUnavailableError
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=1,
            stdout="",
            stderr="claude-opus-5 is not available on your vertex deployment",
        )

        with pytest.raises(ModelUnavailableError, match="Claude CLI failed"):
            await invoke("Hello")

    async def test_invoke_cli_failure_unknown_detail_is_invocation_error(self, mock_run: AsyncMock) -> None:
        """An unclassifiable failure falls back to LLMInvocationError, not a fallback-eligible type."""
        from sova.llm.client import invoke
        from sova.llm.errors import LLMInvocationError, is_fallback_eligible
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=1, stdout="not valid json {{{", stderr="")

        with pytest.raises(LLMInvocationError) as exc_info:
            await invoke("Hello")
        assert not is_fallback_eligible(exc_info.value)

    async def test_invoke_shell_timeout_is_timeout_error(self, mock_run: AsyncMock) -> None:
        """run() returns a timeout ShellResult rather than raising; it must not read as billing."""
        from sova.core.workflow import _is_billing_failure
        from sova.llm.client import invoke
        from sova.llm.errors import LLMTimeoutError
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=-1, stdout="", stderr="Command timed out after 30s")

        with pytest.raises(LLMTimeoutError) as exc_info:
            await invoke("Hello")
        assert not _is_billing_failure(str(exc_info.value))

    async def test_invoke_success_empty_output_is_invocation_error(self, mock_run: AsyncMock) -> None:
        """A structural provider fault is typed directly, never fallback-eligible."""
        from sova.llm.client import invoke
        from sova.llm.errors import LLMInvocationError
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=0, stdout="", stderr="")

        with pytest.raises(LLMInvocationError, match="produced no output"):
            await invoke("Hello")

    async def test_invoke_cli_exit_1_with_valid_output_never_classifies(self, mock_run: AsyncMock) -> None:
        """R15: the CLI's own internal fallback exits nonzero but still produced a usable result."""
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=1, stdout=_make_cli_json(), stderr="")

        with patch("sova.llm.providers.claude_code.classify_error") as mock_classify:
            result = await invoke("Hello")

        assert result.text == "Hello"
        mock_classify.assert_not_called()

    async def test_invoke_cli_failure_prefers_structured_stdout_over_stderr(self, mock_run: AsyncMock) -> None:
        """When stdout carries a structured error, it wins even if stderr is non-empty."""
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=1,
            stdout='{"result": "the real cause"}',
            stderr="Warning: Opus: Opus 5 not available, using configured fallback",
        )

        with pytest.raises(RuntimeError, match="the real cause"):
            await invoke("Hello")

    async def test_invoke_cli_failure_uses_stderr_when_no_structured_stdout(self, mock_run: AsyncMock) -> None:
        """When stdout has no structured error, stderr is used as the fallback."""
        from sova.llm.client import invoke
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=1,
            stdout="",
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

    def test_truncates_long_terminal_reason(self) -> None:
        from sova.llm.providers.claude_code import _extract_failure_detail
        from sova.utils.shell import ShellResult

        stdout = json.dumps({"terminal_reason": "x" * 1000})
        result = ShellResult(returncode=1, stdout=stdout, stderr="")
        detail = _extract_failure_detail(result)
        assert len(detail) <= 500

    def test_structured_stdout_wins_over_warning_stderr(self) -> None:
        from sova.llm.providers.claude_code import _extract_failure_detail
        from sova.utils.shell import ShellResult

        stdout = json.dumps({"is_error": True, "terminal_reason": "budget_exceeded", "result": "billing limit reached"})
        result = ShellResult(
            returncode=1,
            stdout=stdout,
            stderr="Warning: Opus: Opus 5 not available, using configured fallback",
        )
        detail = _extract_failure_detail(result)
        assert "budget_exceeded" in detail
        assert "billing limit reached" in detail
        assert "Warning" not in detail

    def test_warning_only_stderr_with_no_structured_stdout(self) -> None:
        from sova.llm.providers.claude_code import _extract_failure_detail
        from sova.utils.shell import ShellResult

        result = ShellResult(
            returncode=1,
            stdout="",
            stderr="Warning: Opus: Opus 5 not available, using configured fallback\n",
        )
        detail = _extract_failure_detail(result)
        assert detail == "(no error detail captured beyond warnings)"

    def test_strips_only_leading_warning_lines_from_stderr(self) -> None:
        from sova.llm.providers.claude_code import _extract_failure_detail
        from sova.utils.shell import ShellResult

        result = ShellResult(
            returncode=1,
            stdout="",
            stderr="Warning: Opus: Opus 5 not available\nreal error line",
        )
        detail = _extract_failure_detail(result)
        assert detail == "real error line"

    def test_strips_indented_lowercase_warning_lines(self) -> None:
        from sova.llm.providers.claude_code import _extract_failure_detail
        from sova.utils.shell import ShellResult

        result = ShellResult(
            returncode=1,
            stdout="",
            stderr="  warning: something\nreal error",
        )
        detail = _extract_failure_detail(result)
        assert detail == "real error"


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
        # The prompt is sent via stdin, never on argv, and should contain the command
        stdin_prompt = mock_run.call_args.kwargs.get("stdin")
        assert stdin_prompt is not None
        assert "/develop" in stdin_prompt
        assert "42" in stdin_prompt

    async def test_invoke_command_no_args(self, mock_run: AsyncMock) -> None:
        from sova.llm.client import invoke_command
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=_make_cli_json(),
            stderr="",
        )

        await invoke_command("/review")

        assert "/review" in mock_run.call_args.kwargs.get("stdin", "")

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

    async def test_invoke_command_routes_by_task_type(self) -> None:
        from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
        from sova.llm import client

        provider = MagicMock()
        provider.invoke_command = AsyncMock(return_value=LLMResult(text="ok", model="test"))
        cfg = ProjectConfig(llm=LLMConfig(routing={"develop": "haiku"}), agent=AgentConfig(model="opus"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=cfg),
        ):
            await client.invoke_command("/develop", args="42", model="opus", task_type="develop")

        # The route picks "haiku"; resolve_alias then resolves it to a
        # concrete, servable ID on the firstParty backend (issue #1033).
        assert provider.invoke_command.call_args.kwargs["model"] == resolve_model_alias("haiku")

    async def test_invoke_routes_by_task_type_over_explicit_model(self) -> None:
        from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
        from sova.llm import client

        provider = MagicMock()
        provider.invoke = AsyncMock(return_value=LLMResult(text="ok", model="test"))
        cfg = ProjectConfig(llm=LLMConfig(routing={"triage": "haiku"}), agent=AgentConfig(model="opus"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=cfg),
        ):
            await client.invoke("hello", model="opus", task_type="triage")

        assert provider.invoke.call_args.kwargs["model"] == resolve_model_alias("haiku")

    async def test_invoke_command_without_route_keeps_model(self) -> None:
        from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
        from sova.llm import client

        provider = MagicMock()
        provider.invoke_command = AsyncMock(return_value=LLMResult(text="ok", model="test"))
        cfg = ProjectConfig(llm=LLMConfig(routing={}), agent=AgentConfig(model="opus"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "_try_load_config", return_value=cfg),
        ):
            await client.invoke_command("/develop", args="42", model="opus", task_type="develop")

        # No route configured: the explicit model wins over routing (unchanged
        # by #1033), but tier-alias resolution still runs on top of it.
        assert provider.invoke_command.call_args.kwargs["model"] == resolve_model_alias("opus")


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

    @staticmethod
    async def _models_sent(
        req: BatchRequest,
        *,
        task_type: str | None = None,
        routing: dict[str, str] | None = None,
        batch_provider: bool = True,
    ) -> list[str]:
        """Run invoke_batch and return the models the receiving provider saw.

        Both provider sources resolve to the same mock, so *batch_provider*
        only selects which path invoke_batch takes to reach it.
        """
        from sova.llm.client import invoke_batch

        provider = AsyncMock()
        provider.invoke_batch = AsyncMock(return_value=[])
        with (
            patch(
                "sova.llm.providers.anthropic_batch.create_batch_provider",
                return_value=provider if batch_provider else None,
            ),
            patch("sova.llm.client.get_provider", return_value=provider),
            patch("sova.llm.client._try_load_config") as mock_cfg,
        ):
            mock_cfg.return_value.llm.routing = routing or {}
            mock_cfg.return_value.llm.model_aliases = {}
            mock_cfg.return_value.compression.enabled = False
            mock_cfg.return_value.runaway.max_llm_calls = 0
            await invoke_batch([req], task_type=task_type)

        return [r.model for r in provider.invoke_batch.await_args.args[0]]

    @pytest.mark.parametrize(
        ("model", "task_type", "routing", "expected"),
        [
            # A bare family alias becomes a full model ID before the batch API sees it.
            ("sonnet", None, None, "claude-sonnet-5"),
            ("claude-opus-5", None, None, "claude-opus-5"),
            # No model and no task_type leaves the provider default in charge.
            ("", None, None, ""),
            ("", "triage", {"triage": "haiku"}, "claude-haiku-4-5-20251001"),
            # An explicit request model outranks task_type routing.
            ("opus", "triage", {"triage": "haiku"}, "claude-opus-5"),
        ],
    )
    async def test_model_resolved_before_provider(
        self, model: str, task_type: str | None, routing: dict[str, str] | None, expected: str
    ) -> None:
        req = BatchRequest(custom_id="req-1", prompt="hello", model=model)

        assert await self._models_sent(req, task_type=task_type, routing=routing) == [expected]
        # The caller's own request object is never mutated.
        assert req.model == model

    async def test_sequential_fallback_also_normalizes(self) -> None:
        """The non-batch provider path gets the same resolved model."""
        req = BatchRequest(custom_id="req-1", prompt="hello", model="cheap")

        assert await self._models_sent(req, batch_provider=False) == ["claude-haiku-4-5-20251001"]

    async def test_config_load_failure_does_not_retry_routing_per_request(self) -> None:
        """A failed config load is not retried once per request in the batch.

        _resolve_task_type_model() reloads config itself when passed cfg=None,
        so without a short-circuit a batch of N requests with unset models would
        call _try_load_config N+1 times (once upfront, then once per request).
        """
        from sova.llm.client import invoke_batch

        reqs = [BatchRequest(custom_id=f"req-{i}", prompt="hello") for i in range(5)]
        provider = AsyncMock()
        provider.invoke_batch = AsyncMock(return_value=[])

        with (
            patch("sova.llm.providers.anthropic_batch.create_batch_provider", return_value=None),
            patch("sova.llm.client.get_provider", return_value=provider),
            patch("sova.llm.client._try_load_config", return_value=None) as mock_load,
        ):
            await invoke_batch(reqs, task_type="triage")

        mock_load.assert_called_once()


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

    def test_task_type_route_pins_to_same_family_agent_model(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.routing import route_model

        llm_cfg = LLMConfig(routing={"review": "haiku"})
        result = route_model(
            ComplexityTier.COMPLEX,
            task_type="review",
            llm_config=llm_cfg,
            agent_model="claude-haiku-4-5-20251001",
        )
        assert result == (
            "claude-haiku-4-5-20251001",
            "task_type:review->haiku,pinned->claude-haiku-4-5-20251001",
        )

    def test_task_type_route_does_not_pin_across_families(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.routing import route_model

        llm_cfg = LLMConfig(routing={"review": "haiku"})
        result = route_model(
            ComplexityTier.COMPLEX, task_type="review", llm_config=llm_cfg, agent_model="claude-opus-4-6"
        )
        assert result == ("haiku", "task_type:review->haiku")

    def test_task_type_route_to_local_model_is_never_pinned(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.routing import route_model

        llm_cfg = LLMConfig(routing={"triage": "ollama/qwen3:8b"})
        result = route_model(
            ComplexityTier.TRIVIAL,
            task_type="triage",
            llm_config=llm_cfg,
            agent_model="claude-haiku-4-5-20251001",
        )
        assert result == ("ollama/qwen3:8b", "task_type:triage->ollama/qwen3:8b")


class TestTaskTypeKeys:
    def test_disjoint_from_complexity_tiers(self) -> None:
        from sova.llm.routing import TASK_TYPE_KEYS

        complexity_keys = {t.value for t in ComplexityTier}
        overlap = TASK_TYPE_KEYS & complexity_keys
        assert not overlap, f"Overlapping keys: {overlap}"

    def test_known_keys_present(self) -> None:
        from sova.llm.routing import TASK_TYPE_KEYS

        for key in (
            "triage",
            "extraction",
            "pr_body",
            "develop",
            "develop_fix",
            "simplify",
            "review",
            "review_panel",
            "self_review",
            "validate",
            "monitor_ci",
            "rebase",
            "address_review",
            "rearrange_commits",
            "research",
            "spec",
            "harden",
            "generate_tasks",
            "planner",
        ):
            assert key in TASK_TYPE_KEYS

    def test_every_tagged_step_key_is_registered(self) -> None:
        from sova.core.steps import (
            get_address_review_steps,
            get_developer_steps,
            get_planner_steps,
            get_researcher_steps,
        )
        from sova.llm.routing import TASK_TYPE_KEYS

        steps = [
            *get_developer_steps(),
            *get_address_review_steps(),
            *get_researcher_steps(),
            *get_planner_steps(),
        ]
        tagged = {step.TASK_TYPE for step in steps if step.TASK_TYPE}
        assert tagged, "no step declares a TASK_TYPE"
        assert tagged <= TASK_TYPE_KEYS, f"unregistered task types: {tagged - TASK_TYPE_KEYS}"


class TestResolveTaskTypeModel:
    def test_unrouted_task_type_keeps_model(self) -> None:
        """No cfg passed: the sentinel path loads config, finds no route, keeps the model.

        ``load_config`` is patched rather than left to resolve on its own. Without
        the patch this reads whatever ``.claude/sova.db`` the cwd resolves to, so a
        developer with ``llm.routing.triage`` configured got that route back and the
        assertion failed while CI passed (issue #1092). The patch keeps the
        ``_CFG_UNSET`` loading branch under test without depending on the machine.
        """
        from sova.config.models import ProjectConfig
        from sova.llm.client import _resolve_task_type_model

        with patch("sova.config.loader.load_config", return_value=ProjectConfig()):
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

    def test_configured_route_beats_explicit_model(self) -> None:
        from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
        from sova.llm.client import _resolve_task_type_model

        cfg = ProjectConfig(llm=LLMConfig(routing={"develop": "opus"}), agent=AgentConfig(model="sonnet"))
        assert _resolve_task_type_model("sonnet", "develop", cfg=cfg) == "opus"

    def test_unconfigured_route_leaves_explicit_model_untouched(self) -> None:
        from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
        from sova.llm.client import _resolve_task_type_model

        cfg = ProjectConfig(llm=LLMConfig(routing={}), agent=AgentConfig(model="sonnet"))
        assert _resolve_task_type_model("sonnet", "develop", cfg=cfg) == "sonnet"

    def test_route_pins_to_same_family_agent_model(self) -> None:
        from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
        from sova.llm.client import _resolve_task_type_model

        cfg = ProjectConfig(
            llm=LLMConfig(routing={"review": "haiku"}),
            agent=AgentConfig(model="claude-haiku-4-5-20251001"),
        )
        assert _resolve_task_type_model("sonnet", "review", cfg=cfg) == "claude-haiku-4-5-20251001"

    def test_route_does_not_pin_across_families(self) -> None:
        from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
        from sova.llm.client import _resolve_task_type_model

        cfg = ProjectConfig(llm=LLMConfig(routing={"review": "haiku"}), agent=AgentConfig(model="claude-opus-4-6"))
        assert _resolve_task_type_model("sonnet", "review", cfg=cfg) == "haiku"

    def test_local_model_route_returned_verbatim(self) -> None:
        from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
        from sova.llm.client import _resolve_task_type_model

        cfg = ProjectConfig(
            llm=LLMConfig(routing={"develop": "ollama/qwen3:8b"}),
            agent=AgentConfig(model="claude-haiku-4-5-20251001"),
        )
        assert _resolve_task_type_model("sonnet", "develop", cfg=cfg) == "ollama/qwen3:8b"

    def test_empty_task_type_falls_through(self) -> None:
        from sova.config.models import LLMConfig, ProjectConfig
        from sova.llm.client import _resolve_task_type_model

        cfg = ProjectConfig(llm=LLMConfig(routing={"": "haiku"}))
        assert _resolve_task_type_model("sonnet", "", cfg=cfg) == "sonnet"

    def test_config_load_failure_keeps_explicit_model(self) -> None:
        from sova.llm.client import _resolve_task_type_model

        with patch("sova.config.loader.load_config", side_effect=FileNotFoundError):
            assert _resolve_task_type_model("sonnet", "develop") == "sonnet"

    def test_caller_supplied_none_cfg_is_not_reloaded_from_process_cwd(self) -> None:
        """A caller whose own load failed passes cfg=None; reloading it here would
        pick up whatever project the process happens to be sitting in."""
        from sova.llm.client import _resolve_task_type_model

        with patch("sova.config.loader.load_config") as mock_load:
            assert _resolve_task_type_model("sonnet", "develop", cfg=None) == "sonnet"

        mock_load.assert_not_called()

    def test_resolve_timeout_does_not_reload_when_caller_cfg_is_none(self) -> None:
        from sova.llm.client import _resolve_timeout

        with patch("sova.config.loader.load_config") as mock_load:
            assert _resolve_timeout(None, cfg=None) == 900.0

        mock_load.assert_not_called()


class TestConfigRoot:
    """Config lookups must survive the linked worktree every pipeline step runs in."""

    def setup_method(self) -> None:
        from sova.llm.client import reset_config_root_cache

        reset_config_root_cache()

    teardown_method = setup_method

    def test_directory_with_own_toml_resolves_to_itself(self, tmp_path: Path) -> None:
        from sova.llm.client import _config_root

        (tmp_path / "sova.toml").write_text("")
        assert _config_root(tmp_path) == tmp_path

    def test_directory_with_own_db_resolves_to_itself(self, tmp_path: Path) -> None:
        from sova.llm.client import _config_root

        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "sova.db").write_text("")
        assert _config_root(tmp_path) == tmp_path

    def test_worktree_without_config_resolves_to_primary_checkout(self, tmp_path: Path) -> None:
        from sova.llm.client import _config_root

        primary = tmp_path / "primary"
        worktree = tmp_path / "primary" / ".claude" / "worktrees" / "913"
        worktree.mkdir(parents=True)
        with patch("sova.llm.provider._resolve_primary_root", return_value=primary):
            assert _config_root(worktree) == primary

    def test_falls_back_to_cwd_outside_a_repo(self, tmp_path: Path) -> None:
        from sova.llm.client import _config_root

        with patch("sova.llm.provider._resolve_primary_root", return_value=None):
            assert _config_root(tmp_path) == tmp_path

    def test_unset_cwd_stays_unset(self) -> None:
        """None and "" both mean "process default", as load_config expects."""
        from sova.llm.client import _config_root

        assert _config_root(None) is None
        assert _config_root("") is None

    def test_resolution_is_cached_per_directory(self, tmp_path: Path) -> None:
        from sova.llm.client import _config_root

        primary = tmp_path / "primary"
        primary.mkdir()
        with patch("sova.llm.provider._resolve_primary_root", return_value=primary) as mock_resolve:
            assert _config_root(tmp_path) == primary
            assert _config_root(tmp_path) == primary
        assert mock_resolve.call_count == 1

    def test_worktree_route_matches_primary_config(self, tmp_path: Path) -> None:
        """The end-to-end gap: a route configured at the primary checkout must
        still apply when the step passes its worktree as cwd."""
        from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
        from sova.llm.client import _resolve_task_type_model

        primary = tmp_path / "primary"
        worktree = primary / ".claude" / "worktrees" / "913"
        worktree.mkdir(parents=True)
        cfg = ProjectConfig(llm=LLMConfig(routing={"develop": "haiku"}), agent=AgentConfig(model="opus"))

        with (
            patch("sova.llm.provider._resolve_primary_root", return_value=primary),
            patch("sova.config.loader.load_config", return_value=cfg) as mock_load,
        ):
            assert _resolve_task_type_model("opus", "develop", cwd=worktree) == "haiku"

        assert mock_load.call_args.args[0] == primary


class TestTryLoadConfigAsync:
    """Config resolution may shell out to git (``_resolve_primary_root``) on a
    worktree cwd; the async wrapper must offload that to a worker thread so it
    never blocks the event loop."""

    def setup_method(self) -> None:
        from sova.llm.client import reset_config_root_cache

        reset_config_root_cache()

    teardown_method = setup_method

    async def test_returns_same_result_as_sync_variant(self, tmp_path: Path) -> None:
        from sova.llm.client import _try_load_config, _try_load_config_async

        (tmp_path / "sova.toml").write_text("")
        assert await _try_load_config_async(tmp_path) == _try_load_config(tmp_path)

    async def test_offloads_to_a_worker_thread_without_blocking_the_loop(self, tmp_path: Path) -> None:
        import asyncio
        import time

        from sova.llm.client import _try_load_config_async

        worktree = tmp_path / "primary" / ".claude" / "worktrees" / "913"
        worktree.mkdir(parents=True)

        def _slow_resolve(start):
            time.sleep(0.2)
            return tmp_path / "primary"

        first_tick_at: float | None = None

        async def _tick_while_waiting() -> None:
            nonlocal first_tick_at
            await asyncio.sleep(0.05)
            first_tick_at = time.monotonic()

        with patch("sova.llm.provider._resolve_primary_root", side_effect=_slow_resolve):
            start = time.monotonic()
            await asyncio.gather(_try_load_config_async(worktree), _tick_while_waiting())

        # A blocked loop could only run the tick's sleep callback after the
        # 0.2s resolve finished, so its completion would land at ~0.2s instead
        # of its own ~0.05s schedule. This fails if asyncio.to_thread in
        # _try_load_config_async is ever reverted to a direct blocking call.
        assert first_tick_at is not None
        assert first_tick_at - start < 0.15


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

    @pytest.mark.parametrize("usage", ["not-a-dict", [1, 2, 3], 42])
    def test_parse_survives_truthy_non_dict_usage(self, usage: object) -> None:
        from sova.llm.providers.claude_code import _parse_result

        raw = {"result": "ok", "type": "result", "usage": usage, "modelUsage": usage}
        result = _parse_result(raw)
        assert result.text == "ok"
        assert result.input_tokens == 0
        assert result.output_tokens == 0
        assert result.model == ""


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

    async def test_record_cost_persists_compression_savings(self) -> None:
        from sova.llm.cost import record_cost
        from sova.llm.models import LLMResult

        result = LLMResult(
            text="output",
            model="sonnet",
            cost_usd=Decimal("0.01"),
            input_tokens=1000,
            pre_compression_input_tokens=1075,
            tokens_saved=75,
        )

        record = await record_cost(result=result, phase="develop", issue="60")

        assert record.pre_compression_input_tokens == 1075
        assert record.tokens_saved == 75

    async def test_record_cost_compression_columns_default_null(self) -> None:
        from sova.llm.cost import record_cost
        from sova.llm.models import LLMResult

        result = LLMResult(text="output", model="sonnet", cost_usd=Decimal("0.01"))

        record = await record_cost(result=result, phase="develop", issue="61")

        assert record.pre_compression_input_tokens is None
        assert record.tokens_saved is None


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

    async def test_invoke_streaming_gates_budget_for_non_cap_provider(self) -> None:
        """A provider that cannot enforce max_budget_usd natively must not be called
        when the remaining budget is already exhausted."""
        from sova.llm import client
        from sova.llm.errors import BillingError
        from sova.llm.provider import LLMProvider

        class _NoCapProvider(LLMProvider):
            async def invoke(self, prompt, **kwargs):
                raise AssertionError("should not be called")

            async def invoke_streaming(self, prompt, **kwargs):
                raise AssertionError("should not be called")
                yield  # pragma: no cover

            async def check_available(self):
                return True, "ok"

        client.set_provider(_NoCapProvider())
        with pytest.raises(BillingError):
            async for _event in client.invoke_streaming("Say hello", max_budget_usd=Decimal("0")):
                pass


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------


class TestLLMProvider:
    def test_abc_cannot_instantiate(self) -> None:
        from sova.llm.provider import LLMProvider

        with pytest.raises(TypeError):
            LLMProvider()  # type: ignore[abstract]

    def test_create_provider_default(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.provider import create_provider
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        provider = create_provider(LLMConfig())
        assert isinstance(provider, ClaudeCodeProvider)

    def test_create_provider_unknown(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.provider import create_provider

        # model_construct bypasses the provider Literal so the defensive
        # ValueError branch stays reachable from a test.
        with pytest.raises(ValueError, match="Unknown LLM provider") as exc_info:
            create_provider(LLMConfig.model_construct(provider="nonexistent"))
        message = str(exc_info.value)
        for name in ("claude-code", "litellm", "hybrid", "anthropic", "openai", "ollama", "vertex"):
            assert name in message

    def test_create_provider_hybrid(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.provider import create_provider

        with patch.dict("sys.modules", {"litellm": MagicMock(__version__="1.0.0")}):
            import sova.llm.litellm_provider as llm_mod

            llm_mod._HAS_LITELLM = True
            llm_mod.litellm = MagicMock()
            from sova.llm.litellm_provider import LiteLLMProvider

            provider = create_provider(LLMConfig(provider="hybrid"))
            assert isinstance(provider, LiteLLMProvider)

    @pytest.mark.parametrize(
        ("provider_type", "model"),
        [
            ("openai", "gpt-5"),
            ("ollama", "ollama/llama3.1"),
            ("vertex", "vertex_ai/gemini-2.5-pro"),
        ],
    )
    def test_create_provider_vendor_types(self, provider_type: str, model: str) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.provider import create_provider

        with patch.dict("sys.modules", {"litellm": MagicMock(__version__="1.0.0")}):
            import sova.llm.litellm_provider as llm_mod

            llm_mod._HAS_LITELLM = True
            llm_mod.litellm = MagicMock()
            from sova.llm.litellm_provider import LiteLLMProvider

            provider = create_provider(LLMConfig(provider=provider_type, model=model))
            assert isinstance(provider, LiteLLMProvider)
            assert provider.model == model

    def test_create_provider_vendor_type_requires_model(self) -> None:
        from sova.config.models import LLMConfig

        with pytest.raises(ValueError, match="requires an explicit llm.model"):
            LLMConfig(provider="ollama")

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

    def test_capabilities_default_conservative(self) -> None:
        """A provider that doesn't override capabilities is assumed unreliable."""
        from sova.llm.provider import LLMProvider, ProviderCapabilities

        class MinimalProvider(LLMProvider):
            async def invoke(self, prompt, **kwargs):
                return LLMResult(text="", model="")

            async def invoke_streaming(self, prompt, **kwargs):
                yield StreamEvent(type="result", text="")

            async def check_available(self):
                return True, ""

        p = MinimalProvider()
        assert p.capabilities == ProviderCapabilities(
            supports_cli_fallback=False,
            supports_budget_cap=False,
            reports_cost=False,
            dynamic_models=False,
        )

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

    def test_build_args_is_shared_cli_args_builder(self) -> None:
        """The provider re-exports the shared builder under its historical name.

        Behaviour of the builder itself is covered in tests/test_cli_args.py;
        this identity assertion is what makes that coverage apply here too.
        """
        from sova.llm.cli_args import build_claude_cli_args
        from sova.llm.providers.claude_code import _build_args

        assert _build_args is build_claude_cli_args

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
        from sova.config.models import LLMConfig
        from sova.llm.provider import create_provider

        with pytest.raises(ValueError, match="Unknown LLM provider"):
            create_provider(LLMConfig.model_construct(provider="nonexistent"))

    def test_init_llm_provider_makes_no_network_calls(self) -> None:
        """Regression guard for R7: startup init must stay network-call-free.

        _init_llm_provider only constructs objects (load_config is a local DB
        read, create_provider/create_runtime build lazy objects). If a future
        change adds a live availability probe here, this test fails loudly
        instead of the probe silently running on every command.
        """
        import socket

        from sova.cli.app import _init_llm_provider
        from sova.config.models import ProjectConfig

        with (
            patch("sova.cli.app.load_config", return_value=ProjectConfig(llm={"provider": "claude-code"})),
            patch.object(socket.socket, "connect", side_effect=AssertionError("unexpected network call")),
        ):
            _init_llm_provider()  # must not raise

    async def test_spawn_direct_makes_no_network_calls_before_exec(self) -> None:
        """Regression guard for R7: spawn_direct must not probe the network.

        The subprocess boundary (asyncio.create_subprocess_exec) is a local
        exec, not a network call; only a live socket connection would
        indicate a regression toward startup availability probing.
        """
        import socket

        from sova.ipc.runtime import spawn_direct

        with patch.object(socket.socket, "connect", side_effect=AssertionError("unexpected network call")):
            proc = await spawn_direct(["true"], cwd="/tmp")
        await proc.stop()


class TestReloadProviderCapabilityWarning:
    """reload_provider is the single chokepoint (CLI callback, dashboard lifespan,
    settings hot-reload) that must warn when the active provider can't be trusted
    to report real cost, since that makes the dollar-based budget guard blind (R6).
    """

    def test_warns_when_reports_cost_false(self) -> None:
        """A provider declaring reports_cost=False must warn at the chokepoint.

        Stubs create_provider rather than standing up LiteLLM: the concrete
        capability values are asserted in TestLiteLLMProvider.test_capabilities,
        and mutating litellm_provider's module globals here would leak an
        import-guard override into every later test in the session.
        """
        from sova.config.models import ProjectConfig
        from sova.llm import ProviderCapabilities
        from sova.llm.client import reload_provider, reset_provider_warning_state

        stub = MagicMock(capabilities=ProviderCapabilities(reports_cost=False))
        reset_provider_warning_state()
        with (
            patch("sova.llm.client.create_provider", return_value=stub),
            patch("sova.llm.client.log") as mock_log,
        ):
            reload_provider(ProjectConfig(llm={"provider": "litellm"}))

        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args[0][0] == "llm.provider_reports_cost_false"
        assert mock_log.warning.call_args[1]["provider"] == "litellm"

    def test_warning_suppressed_on_repeat_call_same_provider_type(self) -> None:
        """A second reload_provider() with the same reports_cost=False provider type must not re-warn."""
        from sova.config.models import ProjectConfig
        from sova.llm import ProviderCapabilities
        from sova.llm.client import reload_provider, reset_provider_warning_state

        stub = MagicMock(capabilities=ProviderCapabilities(reports_cost=False))
        reset_provider_warning_state()
        with (
            patch("sova.llm.client.create_provider", return_value=stub),
            patch("sova.llm.client.log") as mock_log,
        ):
            reload_provider(ProjectConfig(llm={"provider": "litellm"}))
            reload_provider(ProjectConfig(llm={"provider": "litellm"}))

        mock_log.warning.assert_called_once()

    def test_no_warning_when_reports_cost_true(self) -> None:
        from sova.config.models import ProjectConfig
        from sova.llm.client import reload_provider, reset_provider_warning_state

        reset_provider_warning_state()
        with patch("sova.llm.client.log") as mock_log:
            reload_provider(ProjectConfig(llm={"provider": "claude-code"}))

        mock_log.warning.assert_not_called()


class TestModuleExports:
    def test_imports(self) -> None:
        from sova.llm import (
            LLMProvider,
            LLMResult,
            ProviderCapabilities,
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
        # A third-party provider overriding LLMProvider.capabilities needs the
        # return type from the same package it imports the ABC from.
        assert ProviderCapabilities is not None


# ---------------------------------------------------------------------------
# ClaudeCodeProvider
# ---------------------------------------------------------------------------


class TestClaudeCodeProvider:
    @pytest.fixture
    def mock_run(self):
        with patch("sova.llm.providers.claude_code.run", new_callable=AsyncMock) as mock:
            yield mock

    def test_capabilities(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        caps = ClaudeCodeProvider().capabilities
        assert caps.supports_cli_fallback is True
        assert caps.supports_budget_cap is True
        assert caps.reports_cost is True
        assert caps.dynamic_models is True

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

    async def test_invoke_sends_prompt_via_stdin_not_argv(self, mock_run: AsyncMock) -> None:
        """The prompt must never appear on argv; it is sent over stdin instead."""
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(
            returncode=0,
            stdout=_make_cli_json(),
            stderr="",
        )

        provider = ClaudeCodeProvider()
        await provider.invoke("secret prompt text")

        call_args = mock_run.call_args[0]
        assert "secret prompt text" not in call_args
        assert mock_run.call_args.kwargs.get("stdin") == "secret prompt text"

    async def test_invoke_with_system_prompt_writes_temp_file_not_argv(
        self, mock_run: AsyncMock, tmp_path: Path
    ) -> None:
        """The system prompt must never appear on argv either; it goes to a temp file."""
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=0, stdout=_make_cli_json(), stderr="")

        sp_path = tmp_path / "sova-system-prompt-test.txt"
        with patch("sova.llm.providers.claude_code.write_system_prompt_file", return_value=sp_path) as mock_write:
            provider = ClaudeCodeProvider()
            await provider.invoke("Hello", system_prompt="Be a planner")

        mock_write.assert_called_once_with("Be a planner")
        call_args = mock_run.call_args[0]
        assert "--system-prompt-file" in call_args
        idx = call_args.index("--system-prompt-file")
        assert call_args[idx + 1] == str(sp_path)
        assert "--system-prompt" not in call_args
        assert "Be a planner" not in call_args

    async def test_invoke_deletes_system_prompt_temp_file_after_call(self, mock_run: AsyncMock, tmp_path: Path) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=0, stdout=_make_cli_json(), stderr="")

        sp_path = tmp_path / "sova-system-prompt-cleanup.txt"
        sp_path.write_text("temp", encoding="utf-8")
        with patch("sova.llm.providers.claude_code.write_system_prompt_file", return_value=sp_path):
            provider = ClaudeCodeProvider()
            await provider.invoke("Hello", system_prompt="Be a planner")

        assert not sp_path.exists()

    async def test_invoke_deletes_system_prompt_temp_file_even_on_failure(
        self, mock_run: AsyncMock, tmp_path: Path
    ) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        mock_run.side_effect = RuntimeError("boom")

        sp_path = tmp_path / "sova-system-prompt-fail.txt"
        sp_path.write_text("temp", encoding="utf-8")
        with patch("sova.llm.providers.claude_code.write_system_prompt_file", return_value=sp_path):
            provider = ClaudeCodeProvider()
            with pytest.raises(RuntimeError):
                await provider.invoke("Hello", system_prompt="Be a planner")

        assert not sp_path.exists()

    async def test_invoke_without_system_prompt_does_not_write_temp_file(self, mock_run: AsyncMock) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        mock_run.return_value = ShellResult(returncode=0, stdout=_make_cli_json(), stderr="")

        with patch("sova.llm.providers.claude_code.write_system_prompt_file") as mock_write:
            provider = ClaudeCodeProvider()
            await provider.invoke("Hello")

        mock_write.assert_not_called()

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

    async def test_invoke_streaming_failure_raises_typed_error(self) -> None:
        """The streaming raise site classifies from the same redacted stderr as invoke()."""
        from sova.llm.errors import ModelUnavailableError
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        mock_proc = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(return_value=b"")
        mock_proc.stderr.read = AsyncMock(return_value=b"claude-opus-5 is not available on your vertex deployment")
        mock_proc.wait = AsyncMock()
        mock_proc.returncode = 1

        with patch(
            "sova.llm.providers.claude_code._start_streaming_process",
            return_value=mock_proc,
        ):
            provider = ClaudeCodeProvider()
            with pytest.raises(ModelUnavailableError, match="Claude CLI streaming failed"):
                async for _ in provider.invoke_streaming("Hello"):
                    pass

    async def test_start_streaming_process_includes_verbose(self) -> None:
        """Regression: streaming previously omitted --verbose, which the CLI requires
        alongside -p plus --output-format stream-json."""
        from sova.llm.providers.claude_code import _start_streaming_process

        mock_proc = AsyncMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        with patch(
            "sova.llm.providers.claude_code.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ) as mock_exec:
            await _start_streaming_process("hello")

        call_args = mock_exec.call_args[0]
        assert "--verbose" in call_args
        assert "--output-format" in call_args
        assert "stream-json" in call_args

    async def test_start_streaming_process_sends_prompt_via_stdin_not_argv(self) -> None:
        """The prompt must never appear on argv; it is written to stdin and the pipe closed."""
        from sova.llm.providers.claude_code import _start_streaming_process

        mock_proc = AsyncMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        with patch(
            "sova.llm.providers.claude_code.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ) as mock_exec:
            await _start_streaming_process("secret prompt text")

        call_args = mock_exec.call_args[0]
        assert "secret prompt text" not in call_args
        assert mock_exec.call_args.kwargs.get("stdin") is asyncio.subprocess.PIPE
        mock_proc.stdin.write.assert_called_once_with(b"secret prompt text")
        mock_proc.stdin.drain.assert_awaited_once()
        mock_proc.stdin.close.assert_called_once()

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
    @pytest.fixture(autouse=True)
    def _reset_model_cache(self):
        """The enumeration cache is a process global, and several tests in this
        class share one identity (same model + api_base), so without this the
        first to run would answer for the rest depending on ordering."""
        from sova.llm.client import reset_availability_cache

        reset_availability_cache()
        yield
        reset_availability_cache()

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

    def test_capabilities(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        caps = LiteLLMProvider(model="gpt-4o").capabilities
        assert caps.supports_cli_fallback is False
        assert caps.supports_budget_cap is False
        assert caps.reports_cost is True
        assert caps.dynamic_models is True

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

    async def test_ollama_provider_round_trip_no_daemon(self, mock_litellm: MagicMock) -> None:
        """create_provider('ollama', ...) -> invoke(...) works against a faked backend.

        No real `ollama serve` daemon is required: litellm is faked at the
        module level by the mock_litellm fixture, exactly as the other
        LiteLLMProvider tests in this class do.
        """
        from sova.config.models import LLMConfig
        from sova.llm.provider import create_provider

        mock_litellm.acompletion.return_value = _MockResponse(
            content="local response",
            model="ollama/llama3.1",
        )

        provider = create_provider(LLMConfig(provider="ollama", model="ollama/llama3.1"))
        result = await provider.invoke("Hello")

        assert result.text == "local response"
        call_kwargs = mock_litellm.acompletion.call_args
        assert call_kwargs[1]["model"] == "ollama/llama3.1"

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

    async def test_no_fallback_raises_typed_error(self, mock_litellm: MagicMock) -> None:
        from sova.llm.errors import RateLimitError
        from sova.llm.litellm_provider import LiteLLMProvider

        mock_litellm.acompletion.side_effect = type("RateLimitError", (Exception,), {})("slow down")

        provider = LiteLLMProvider(model="gpt-4o")
        with pytest.raises(RateLimitError, match="slow down"):
            await provider.invoke("Hello")

    async def test_connection_failure_is_provider_unavailable(self, mock_litellm: MagicMock) -> None:
        """Ollama being down maps to ProviderUnavailableError, the fallback-eligible type."""
        from sova.llm.errors import ProviderUnavailableError, is_fallback_eligible
        from sova.llm.litellm_provider import LiteLLMProvider

        mock_litellm.acompletion.side_effect = ConnectionRefusedError("connection refused")

        provider = LiteLLMProvider(model="ollama/qwen3-coder")
        with pytest.raises(ProviderUnavailableError) as exc_info:
            await provider.invoke("Hello")
        assert is_fallback_eligible(exc_info.value)

    async def test_wrapping_preserves_connection_error_detection(self, mock_litellm: MagicMock) -> None:
        """The transport error stays one hop down, so the fallback log reason is unchanged.

        The message deliberately omits the "connection refused" text so the
        assertion pins _is_connection_error's cause-chain walk, not its
        string check.
        """
        from sova.llm.litellm_provider import LiteLLMProvider, _is_connection_error

        mock_litellm.acompletion.side_effect = ConnectionRefusedError("[Errno 61] socket unavailable")

        provider = LiteLLMProvider(model="ollama/qwen3-coder")
        with pytest.raises(RuntimeError) as exc_info:
            await provider.invoke("Hello")

        assert isinstance(exc_info.value.__cause__, ConnectionRefusedError)
        assert _is_connection_error(exc_info.value)

    async def test_fallback_failure_is_also_typed(self, mock_litellm: MagicMock) -> None:
        """The second attempt must not leak a raw SDK exception past the provider boundary."""
        from sova.llm.errors import LLMError
        from sova.llm.litellm_provider import LiteLLMProvider

        mock_litellm.acompletion.side_effect = [
            RuntimeError("Primary model unavailable"),
            type("RateLimitError", (Exception,), {})("fallback throttled"),
        ]

        provider = LiteLLMProvider(model="gpt-4o", fallback_model="ollama/qwen3-coder")
        with pytest.raises(LLMError, match="fallback throttled"):
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

    async def test_streaming_no_fallback_raises_typed_error(self, mock_litellm: MagicMock) -> None:
        """The bare re-raise in invoke_streaming propagates the type set by _stream."""
        from sova.llm.errors import RateLimitError
        from sova.llm.litellm_provider import LiteLLMProvider

        async def mock_stream():
            yield _MockStreamChunk(content="Hello ", model="gpt-4o")
            raise type("RateLimitError", (Exception,), {})("slow down")

        mock_litellm.acompletion.return_value = mock_stream()

        provider = LiteLLMProvider(model="gpt-4o")
        events = []
        with pytest.raises(RateLimitError, match="slow down"):
            async for event in provider.invoke_streaming("Hello"):
                events.append(event)

        assert [e.type for e in events] == ["content", "result"]
        assert events[-1].result is not None
        assert events[-1].result.stop_reason == "error"

    async def test_cost_tracking_fallback(self, mock_litellm: MagicMock) -> None:
        from sova.llm import litellm_provider as llm_mod
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CostSource

        mock_litellm.acompletion.return_value = _MockResponse()
        mock_litellm.completion_cost.side_effect = ValueError("Unknown model")

        provider = LiteLLMProvider(model="custom-model")
        with patch.object(llm_mod.log, "warning") as mock_warning, patch.object(llm_mod.log, "debug") as mock_debug:
            result = await provider.invoke("Hello")

        assert result.cost_usd == Decimal("0")
        assert result.cost_source == CostSource.UNKNOWN
        mock_warning.assert_called_once()
        mock_debug.assert_not_called()

    async def test_cost_tracking_fallback_logs_debug_for_ollama_model(self, mock_litellm: MagicMock) -> None:
        from sova.llm import litellm_provider as llm_mod
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CostSource

        mock_litellm.acompletion.return_value = _MockResponse(model="ollama/qwen3-coder")
        mock_litellm.completion_cost.side_effect = ValueError("Unknown model")

        provider = LiteLLMProvider(model="ollama/qwen3-coder")
        with patch.object(llm_mod.log, "warning") as mock_warning, patch.object(llm_mod.log, "debug") as mock_debug:
            result = await provider.invoke("Hello")

        assert result.cost_usd == Decimal("0")
        assert result.cost_source == CostSource.FREE_LOCAL
        mock_debug.assert_called_once()
        mock_warning.assert_not_called()

    async def test_cost_tracking_fallback_local_model_without_echoed_prefix(self, mock_litellm: MagicMock) -> None:
        """LiteLLM may echo a local model ID without its provider prefix."""
        from sova.llm import litellm_provider as llm_mod
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CostSource

        mock_litellm.acompletion.return_value = _MockResponse(model="qwen3-coder")
        mock_litellm.completion_cost.side_effect = ValueError("Unknown model")

        provider = LiteLLMProvider(model="ollama/qwen3-coder")
        with patch.object(llm_mod.log, "warning") as mock_warning, patch.object(llm_mod.log, "debug") as mock_debug:
            result = await provider.invoke("Hello")

        assert result.cost_usd == Decimal("0")
        assert result.cost_source == CostSource.FREE_LOCAL
        mock_debug.assert_called_once()
        mock_warning.assert_not_called()

    async def test_cost_tracking_non_raising_zero_cost_still_warns(self, mock_litellm: MagicMock) -> None:
        """litellm.completion_cost() returns 0.0 for an unknown model instead of raising."""
        from sova.llm import litellm_provider as llm_mod
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CostSource

        mock_litellm.acompletion.return_value = _MockResponse()
        mock_litellm.completion_cost.return_value = 0.0

        provider = LiteLLMProvider(model="custom-model")
        with patch.object(llm_mod.log, "warning") as mock_warning, patch.object(llm_mod.log, "debug") as mock_debug:
            result = await provider.invoke("Hello")

        assert result.cost_usd == Decimal("0")
        assert result.cost_source == CostSource.UNKNOWN
        mock_warning.assert_called_once()
        mock_debug.assert_not_called()

    async def test_cost_tracking_non_raising_zero_cost_local_model_logs_debug(self, mock_litellm: MagicMock) -> None:
        """The non-raising zero-cost path must still distinguish local models."""
        from sova.llm import litellm_provider as llm_mod
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CostSource

        mock_litellm.acompletion.return_value = _MockResponse(model="ollama/qwen3-coder")
        mock_litellm.completion_cost.return_value = 0.0

        provider = LiteLLMProvider(model="ollama/qwen3-coder")
        with patch.object(llm_mod.log, "warning") as mock_warning, patch.object(llm_mod.log, "debug") as mock_debug:
            result = await provider.invoke("Hello")

        assert result.cost_usd == Decimal("0")
        assert result.cost_source == CostSource.FREE_LOCAL
        mock_debug.assert_called_once()
        mock_warning.assert_not_called()

    async def test_cost_tracking_warns_once_per_model_per_instance(self, mock_litellm: MagicMock) -> None:
        """Repeated calls for the same unpriced hosted model must not repeat the warning."""
        from sova.llm import litellm_provider as llm_mod
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CostSource

        mock_litellm.acompletion.return_value = _MockResponse()
        mock_litellm.completion_cost.side_effect = ValueError("Unknown model")

        provider = LiteLLMProvider(model="custom-model")
        with patch.object(llm_mod.log, "warning") as mock_warning:
            first = await provider.invoke("Hello")
            second = await provider.invoke("Hello again")

        assert first.cost_source == CostSource.UNKNOWN
        assert second.cost_source == CostSource.UNKNOWN
        mock_warning.assert_called_once()

    async def test_cost_tracking_warning_dedup_is_scoped_to_instance(self, mock_litellm: MagicMock) -> None:
        """A fresh provider instance must warn again, independent of any prior instance."""
        from sova.llm import litellm_provider as llm_mod
        from sova.llm.litellm_provider import LiteLLMProvider

        mock_litellm.acompletion.return_value = _MockResponse()
        mock_litellm.completion_cost.side_effect = ValueError("Unknown model")

        await LiteLLMProvider(model="custom-model").invoke("Hello")

        with patch.object(llm_mod.log, "warning") as mock_warning:
            await LiteLLMProvider(model="custom-model").invoke("Hello")

        mock_warning.assert_called_once()

    async def test_stop_reason_mapping(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        response = _MockResponse()
        response.choices[0].finish_reason = "length"
        mock_litellm.acompletion.return_value = response

        provider = LiteLLMProvider(model="gpt-4o")
        result = await provider.invoke("Hello")

        assert result.stop_reason == "length"

    def test_create_provider_litellm(self, mock_litellm: MagicMock) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.provider import create_provider

        provider = create_provider(LLMConfig(provider="litellm"))
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
        """Every LiteLLM-relevant field is read off cfg, not silently dropped."""
        from sova.config.models import LLMConfig
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.provider import create_provider

        provider = create_provider(
            LLMConfig(
                provider="litellm",
                model="gpt-4o",
                fallback_model="ollama/qwen3-coder",
                api_base="http://localhost:4000",
            )
        )
        assert isinstance(provider, LiteLLMProvider)
        assert provider.model == "gpt-4o"
        assert provider.fallback_model == "ollama/qwen3-coder"
        assert provider.api_base == "http://localhost:4000"

    def test_create_provider_resolves_model_aliases(self, mock_litellm: MagicMock) -> None:
        """llm.model and llm.fallback_model may be alias names, not just native IDs."""
        from sova.config.models import LLMConfig
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.provider import create_provider

        provider = create_provider(
            LLMConfig(
                provider="litellm",
                model="smart",
                fallback_model="cheap",
                model_aliases={"smart": "ollama/llama3.1:70b", "cheap": "ollama/qwen3-coder"},
            )
        )
        assert isinstance(provider, LiteLLMProvider)
        assert provider.model == "ollama/llama3.1:70b"
        assert provider.fallback_model == "ollama/qwen3-coder"

    async def test_check_available(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        provider = LiteLLMProvider(model="gpt-4o")
        available, detail = await provider.check_available()

        assert available is True
        assert "1.0.0" in detail

    async def test_list_available_models_allow_probe_false_skips_everything(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CURATED_MODELS

        with patch.dict(os.environ, {"ANTHROPIC_VERTEX_PROJECT_ID": "proj"}):
            provider = LiteLLMProvider(model="claude-sonnet-4-6")
            models = await provider.list_available_models(allow_probe=False)

        assert models == list(CURATED_MODELS)

    async def test_list_available_models_nothing_configured_returns_curated(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CURATED_MODELS

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_VERTEX_PROJECT_ID", None)
            provider = LiteLLMProvider(model="claude-sonnet-4-6")
            models = await provider.list_available_models()

        assert models == list(CURATED_MODELS)

    @respx.mock
    async def test_list_available_models_vertex_success_filters_openai_publisher(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import ModelFamily

        respx.get(url__regex=r".*/v1beta1/publishers/anthropic/models").mock(
            return_value=httpx.Response(
                200,
                json={"publisherModels": [{"name": "publishers/anthropic/models/claude-opus-5"}]},
            )
        )
        respx.get(url__regex=r".*/v1beta1/publishers/google/models").mock(
            return_value=httpx.Response(
                200,
                json={"publisherModels": [{"name": "publishers/google/models/gemini-2.5-pro"}]},
            )
        )
        respx.get(url__regex=r".*/v1beta1/publishers/openai/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "publisherModels": [
                        {"name": "publishers/openai/models/gpt-oss-120b"},
                        {"name": "publishers/openai/models/gpt-4o"},
                    ]
                },
            )
        )

        with patch.dict(os.environ, {"ANTHROPIC_VERTEX_PROJECT_ID": "proj"}):
            provider = LiteLLMProvider(model="claude-sonnet-4-6")
            with patch.object(provider._vertex_token_provider, "get_token", AsyncMock(return_value="tok")):
                models = await provider.list_available_models()

        ids = {m.id for m in models}
        assert ids == {"claude-opus-5", "gemini-2.5-pro", "gpt-oss-120b"}
        assert "gpt-4o" not in ids
        assert next(m for m in models if m.id == "claude-opus-5").family == ModelFamily.ANTHROPIC
        assert next(m for m in models if m.id == "gemini-2.5-pro").family == ModelFamily.GOOGLE
        assert next(m for m in models if m.id == "gpt-oss-120b").family == ModelFamily.OPENAI_OSS
        assert all(m.source == "vertex" for m in models)

    @respx.mock
    async def test_list_available_models_vertex_single_publisher_failure_is_non_fatal(
        self, mock_litellm: MagicMock
    ) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        respx.get(url__regex=r".*/v1beta1/publishers/anthropic/models").mock(
            return_value=httpx.Response(
                200,
                json={"publisherModels": [{"name": "publishers/anthropic/models/claude-opus-5"}]},
            )
        )
        respx.get(url__regex=r".*/v1beta1/publishers/google/models").mock(return_value=httpx.Response(500))
        respx.get(url__regex=r".*/v1beta1/publishers/openai/models").mock(
            return_value=httpx.Response(200, json={"publisherModels": []})
        )

        with patch.dict(os.environ, {"ANTHROPIC_VERTEX_PROJECT_ID": "proj"}):
            provider = LiteLLMProvider(model="claude-sonnet-4-6")
            with patch.object(provider._vertex_token_provider, "get_token", AsyncMock(return_value="tok")):
                models = await provider.list_available_models()

        assert {m.id for m in models} == {"claude-opus-5"}

    async def test_list_available_models_vertex_token_unavailable_returns_curated(
        self, mock_litellm: MagicMock
    ) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CURATED_MODELS

        with patch.dict(os.environ, {"ANTHROPIC_VERTEX_PROJECT_ID": "proj"}):
            provider = LiteLLMProvider(model="claude-sonnet-4-6")
            with patch.object(
                provider._vertex_token_provider,
                "get_token",
                AsyncMock(side_effect=ImportError("google-auth not installed")),
            ):
                models = await provider.list_available_models()

        assert models == list(CURATED_MODELS)

    @respx.mock
    async def test_list_available_models_ollama_success(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import ModelFamily

        respx.get("http://localhost:11434/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "llama3:latest"}]})
        )

        provider = LiteLLMProvider(model="ollama/llama3")
        models = await provider.list_available_models()

        assert len(models) == 1
        assert models[0].id == "ollama/llama3:latest"
        assert models[0].family == ModelFamily.LOCAL
        assert models[0].source == "ollama"

    @respx.mock
    async def test_list_available_models_ollama_daemon_down_returns_curated(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CURATED_MODELS

        respx.get("http://localhost:11434/api/tags").mock(side_effect=httpx.ConnectError("refused"))

        provider = LiteLLMProvider(model="ollama/llama3")
        models = await provider.list_available_models()

        assert models == list(CURATED_MODELS)

    @respx.mock
    async def test_list_available_models_openai_compatible_success(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        respx.get("http://localhost:8000/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "local-model-1"}]})
        )

        provider = LiteLLMProvider(model="local-model-1", api_base="http://localhost:8000/v1")
        models = await provider.list_available_models()

        assert len(models) == 1
        assert models[0].id == "local-model-1"
        assert models[0].source == "openai_compatible"

    @respx.mock
    async def test_list_available_models_openai_compatible_sends_bearer_on_loopback(
        self, mock_litellm: MagicMock
    ) -> None:
        """A loopback endpoint still uses the same OPENAI_API_KEY credential
        the LiteLLM invocation itself resolves this endpoint from: without
        it, a key-protected probe gets 401'd, _fetch_json_entries silently
        turns that into [], and enumeration never caches, retrying forever."""
        from sova.llm.litellm_provider import LiteLLMProvider

        route = respx.get("http://localhost:8000/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "local-model-1"}]})
        )

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}):
            provider = LiteLLMProvider(model="local-model-1", api_base="http://localhost:8000/v1")
            models = await provider.list_available_models()

        assert len(models) == 1
        assert route.calls.last.request.headers["authorization"] == "Bearer sk-test-key"

    @respx.mock
    async def test_list_available_models_openai_compatible_no_key_sends_no_auth_header(
        self, mock_litellm: MagicMock
    ) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        route = respx.get("http://localhost:8000/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "local-model-1"}]})
        )

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            provider = LiteLLMProvider(model="local-model-1", api_base="http://localhost:8000/v1")
            models = await provider.list_available_models()

        assert len(models) == 1
        assert "authorization" not in route.calls.last.request.headers

    @respx.mock
    async def test_list_available_models_openai_compatible_key_withheld_from_plaintext_remote(
        self, mock_litellm: MagicMock
    ) -> None:
        """A non-loopback, non-HTTPS api_base must never receive the bearer
        credential: plaintext HTTP to a remote host is not a safe channel for
        it, unlike a loopback shim or an HTTPS endpoint."""
        from sova.llm.litellm_provider import LiteLLMProvider

        route = respx.get("http://models.example.com/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "remote-model-1"}]})
        )

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}):
            provider = LiteLLMProvider(model="remote-model-1", api_base="http://models.example.com/v1")
            models = await provider.list_available_models()

        assert len(models) == 1
        assert "authorization" not in route.calls.last.request.headers

    @respx.mock
    async def test_enumeration_identity_scoped_by_openai_api_key(self, mock_litellm: MagicMock) -> None:
        """Two accounts hitting the same api_base must not share one cached
        catalog: _enumerate_openai_compatible() resolves OPENAI_API_KEY at
        call time, so the cache key must change with it or a key rotation
        serves the previous account's models for up to the enumeration TTL."""
        from sova.llm.litellm_provider import LiteLLMProvider

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-account-a"}):
            provider_a = LiteLLMProvider(model="local-model-1", api_base="http://localhost:8000/v1")
            identity_a = provider_a._enumeration_identity()

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-account-b"}):
            provider_b = LiteLLMProvider(model="local-model-1", api_base="http://localhost:8000/v1")
            identity_b = provider_b._enumeration_identity()

        assert identity_a != identity_b
        assert "sk-account-a" not in identity_a
        assert "sk-account-b" not in identity_b

    @respx.mock
    async def test_enumeration_identity_no_key_is_stable(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            provider = LiteLLMProvider(model="local-model-1", api_base="http://localhost:8000/v1")
            identity = provider._enumeration_identity()

        assert identity.startswith("litellm:local-model-1:http://localhost:8000/v1::")

    @respx.mock
    async def test_list_available_models_openai_compatible_skipped_for_local_model(
        self, mock_litellm: MagicMock
    ) -> None:
        """api_base set, but the model ID already identifies an Ollama target:
        /v1/models must never be attempted, only /api/tags."""
        from sova.llm.litellm_provider import LiteLLMProvider

        tags_route = respx.get("http://localhost:11434/api/tags").mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        models_route = respx.get("http://localhost:11434/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "should-not-be-fetched"}]})
        )

        provider = LiteLLMProvider(model="ollama/llama3", api_base="http://localhost:11434")
        models = await provider.list_available_models()

        assert tags_route.called
        assert not models_route.called
        assert all(m.id != "should-not-be-fetched" for m in models)

    async def test_list_available_models_openai_compatible_skipped_for_vertex_model(
        self, mock_litellm: MagicMock
    ) -> None:
        """A LiteLLM "vertex_ai/..." model ID must never trigger an OpenAI-
        compatible /v1/models probe, even with api_base set: no HTTP call
        of any kind should be attempted (unmocked respx raises on any call)."""
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CURATED_MODELS

        with respx.mock:
            provider = LiteLLMProvider(model="vertex_ai/gemini-2.5-pro", api_base="http://localhost:9999")
            models = await provider.list_available_models()

        assert models == list(CURATED_MODELS)

    @respx.mock
    async def test_list_available_models_dedup_keeps_first_occurrence(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        respx.get("http://localhost:11434/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "shared-model"}]})
        )
        respx.get("http://localhost:11434/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "ollama/shared-model"}]})
        )

        provider = LiteLLMProvider(model="ollama/shared-model", api_base="http://localhost:11434")
        models = await provider.list_available_models()

        ids = [m.id for m in models]
        assert ids.count("ollama/shared-model") == 1

    @respx.mock
    async def test_list_available_models_results_are_cached(self, mock_litellm: MagicMock) -> None:
        from sova.llm.litellm_provider import LiteLLMProvider

        route = respx.get("http://localhost:11434/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "llama3"}]})
        )

        provider = LiteLLMProvider(model="ollama/llama3")
        first = await provider.list_available_models()
        second = await provider.list_available_models()

        assert first == second
        assert route.call_count == 1

    @respx.mock
    async def test_configured_but_unreachable_fallback_is_not_cached(self, mock_litellm: MagicMock) -> None:
        """A configured-but-down backend is an outage, not a fact about the
        deployment, so the curated fallback must not be pinned for the TTL."""
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CURATED_MODELS

        route = respx.get("http://localhost:11434/api/tags").mock(side_effect=httpx.ConnectError("refused"))

        provider = LiteLLMProvider(model="ollama/llama3")
        first = await provider.list_available_models()
        second = await provider.list_available_models()

        assert first == list(CURATED_MODELS)
        assert second == list(CURATED_MODELS)
        assert route.call_count == 2  # retried, not served from a cached fallback

    async def test_nothing_configured_fallback_is_cached(self, mock_litellm: MagicMock) -> None:
        """With no enumeration source configured at all, curated is the stable
        answer for this deployment, so it is cached rather than recomputed."""
        from sova.llm.client import get_availability_cache
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CURATED_MODELS

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_VERTEX_PROJECT_ID", None)
            provider = LiteLLMProvider(model="claude-sonnet-4-6")
            models = await provider.list_available_models()
            cached = get_availability_cache().get_enumeration(provider._enumeration_identity())

        assert models == list(CURATED_MODELS)
        assert cached == list(CURATED_MODELS)

    @respx.mock
    @pytest.mark.parametrize(
        "payload",
        [
            "not-an-object",
            {"models": "not-a-list"},
            {"models": ["bare-scalar", 7, None]},
            {},
        ],
        ids=["scalar-body", "non-list-key", "non-dict-entries", "missing-key"],
    )
    async def test_malformed_enumeration_payload_yields_curated(self, mock_litellm: MagicMock, payload: object) -> None:
        """An operator-supplied endpoint can return any shape; every layer is
        checked so a caller's entry.get() can never raise an AttributeError."""
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CURATED_MODELS

        respx.get("http://localhost:11434/api/tags").mock(return_value=httpx.Response(200, json=payload))

        provider = LiteLLMProvider(model="ollama/llama3")
        models = await provider.list_available_models()

        assert models == list(CURATED_MODELS)

    async def test_enumeration_source_raising_is_treated_as_an_outage(self, mock_litellm: MagicMock) -> None:
        """A source growing an unguarded failure path must not escape
        list_available_models(), and must map to "configured, produced nothing"
        (retried) rather than "not configured" (curated cached for the TTL)."""
        from sova.llm.client import get_availability_cache
        from sova.llm.litellm_provider import LiteLLMProvider
        from sova.llm.models import CURATED_MODELS

        provider = LiteLLMProvider(model="ollama/llama3")
        with patch.object(provider, "_enumerate_ollama", AsyncMock(side_effect=RuntimeError("boom"))):
            models = await provider.list_available_models()

        assert models == list(CURATED_MODELS)
        assert get_availability_cache().get_enumeration(provider._enumeration_identity()) is None

    async def test_enumeration_identity_includes_vertex_project_and_region(self, mock_litellm: MagicMock) -> None:
        """Vertex project/region select which publisher catalog is read, so two
        deployments differing only in those must not share a cache entry."""
        from sova.llm.litellm_provider import LiteLLMProvider

        provider = LiteLLMProvider(model="claude-sonnet-4-6")
        with patch.dict(os.environ, {"ANTHROPIC_VERTEX_PROJECT_ID": "proj-a", "CLOUD_ML_REGION": "us-east5"}):
            first = provider._enumeration_identity()
        with patch.dict(os.environ, {"ANTHROPIC_VERTEX_PROJECT_ID": "proj-b", "CLOUD_ML_REGION": "us-east5"}):
            second = provider._enumeration_identity()
        with patch.dict(os.environ, {"ANTHROPIC_VERTEX_PROJECT_ID": "proj-a", "CLOUD_ML_REGION": "europe-west1"}):
            third = provider._enumeration_identity()

        assert first != second
        assert first != third


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


def _mock_llm_config(
    mock_cfg: MagicMock,
    *,
    routing: dict[str, str] | None = None,
    model_aliases: dict[str, str] | None = None,
    model: str = "",
    fallback_model: str = "",
    provider: str = "claude-code",
) -> None:
    """Populate a patched load_config mock with concrete llm fields.

    Every field _check_ollama reads must be a real string or dict: a bare
    MagicMock attribute satisfies `.startswith("ollama/")` (and compares
    unequal to "ollama") and would add a phantom model to every check.
    """
    mock_cfg.return_value.llm.routing = routing or {}
    mock_cfg.return_value.llm.model_aliases = model_aliases or {}
    mock_cfg.return_value.llm.model = model
    mock_cfg.return_value.llm.fallback_model = fallback_model
    mock_cfg.return_value.llm.provider = provider


class TestDoctorOllamaCheck:
    async def test_no_ollama_models_returns_empty(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_ollama

        with patch("sova.config.loader.load_config") as mock_cfg:
            _mock_llm_config(mock_cfg, routing={"trivial": "haiku"})
            checks = await _check_ollama(tmp_path)
            assert checks == []

    async def test_ollama_not_installed(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_ollama

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.cli.commands.doctor.shutil.which", return_value=None),
        ):
            _mock_llm_config(mock_cfg, routing={"triage": "ollama/qwen3:8b"})
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
            _mock_llm_config(mock_cfg, routing={"triage": "ollama/qwen3:8b"})
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
            _mock_llm_config(mock_cfg, routing={"triage": "ollama/qwen3:8b"})
            mock_run.return_value = MagicMock(
                success=True,
                stdout="NAME\tID\tSIZE\tMODIFIED\nqwen3:8b\tabc123\t5.0 GB\t2 hours ago\n",
            )
            checks = await _check_ollama(tmp_path)
            assert any(c[0] == "ollama running" and c[1] is True for c in checks)
            model_check = [c for c in checks if "qwen3" in c[0]]
            assert model_check
            assert model_check[0][1] is True

    async def test_alias_map_targets_are_checked(self, tmp_path: Path) -> None:
        """An alias pointing at a local model must be checked like a routing entry."""
        from sova.cli.commands.doctor import _check_ollama

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.cli.commands.doctor.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.cli.commands.doctor.run", new_callable=AsyncMock) as mock_run,
        ):
            _mock_llm_config(mock_cfg, model_aliases={"smart": "ollama/llama3.1:70b"})
            mock_run.return_value = MagicMock(success=True, stdout="NAME\tID\nqwen3:8b\tabc123\n")
            checks = await _check_ollama(tmp_path)

        model_check = [c for c in checks if "llama3.1" in c[0]]
        assert model_check
        assert model_check[0][1] is False

    async def test_ollama_model_tag_mismatch_is_not_reported_installed(self, tmp_path: Path) -> None:
        """A configured tag must not match a different installed tag of the same base model."""
        from sova.cli.commands.doctor import _check_ollama

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.cli.commands.doctor.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.cli.commands.doctor.run", new_callable=AsyncMock) as mock_run,
        ):
            _mock_llm_config(mock_cfg, routing={"triage": "ollama/llama3.1:70b"})
            mock_run.return_value = MagicMock(success=True, stdout="NAME\tID\nllama3.1:8b\tabc123\n")
            checks = await _check_ollama(tmp_path)

        model_check = [c for c in checks if "llama3.1" in c[0]]
        assert model_check
        assert model_check[0][1] is False

    async def test_ollama_model_without_tag_matches_latest(self, tmp_path: Path) -> None:
        """A configured model with no explicit tag matches an installed ':latest' pull."""
        from sova.cli.commands.doctor import _check_ollama

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.cli.commands.doctor.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.cli.commands.doctor.run", new_callable=AsyncMock) as mock_run,
        ):
            _mock_llm_config(mock_cfg, routing={"triage": "ollama/llama3.1"})
            mock_run.return_value = MagicMock(success=True, stdout="NAME\tID\nllama3.1:latest\tabc123\n")
            checks = await _check_ollama(tmp_path)

        model_check = [c for c in checks if "llama3.1" in c[0]]
        assert model_check
        assert model_check[0][1] is True

    async def test_ollama_provider_without_prefix_is_flagged(self, tmp_path: Path) -> None:
        """provider = 'ollama' with an unprefixed model must not silently check nothing."""
        from sova.cli.commands.doctor import _check_ollama

        with patch("sova.config.loader.load_config") as mock_cfg:
            _mock_llm_config(mock_cfg, provider="ollama", model="llama3.1")
            checks = await _check_ollama(tmp_path)

        assert len(checks) == 1
        assert checks[0][0] == "ollama model prefix"
        assert checks[0][1] is False
        assert "ollama/" in checks[0][2]

    async def test_non_ollama_provider_without_models_stays_silent(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_ollama

        with patch("sova.config.loader.load_config") as mock_cfg:
            _mock_llm_config(mock_cfg, provider="openai", model="gpt-5")
            assert await _check_ollama(tmp_path) == []

    async def test_llm_model_field_is_checked(self, tmp_path: Path) -> None:
        """provider = 'ollama' with llm.model set directly (no routing entry) is checked."""
        from sova.cli.commands.doctor import _check_ollama

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.cli.commands.doctor.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.cli.commands.doctor.run", new_callable=AsyncMock) as mock_run,
        ):
            _mock_llm_config(mock_cfg, model="ollama/llama3.1")
            mock_run.return_value = MagicMock(success=True, stdout="NAME\tID\nqwen3:8b\tabc123\n")
            checks = await _check_ollama(tmp_path)

        model_check = [c for c in checks if "llama3.1" in c[0]]
        assert model_check
        assert model_check[0][1] is False

    async def test_llm_fallback_model_field_is_checked(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_ollama

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.cli.commands.doctor.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.cli.commands.doctor.run", new_callable=AsyncMock) as mock_run,
        ):
            _mock_llm_config(mock_cfg, model="gpt-5", fallback_model="ollama/llama3.1")
            mock_run.return_value = MagicMock(success=True, stdout="NAME\tID\nllama3.1:latest\tabc123\n")
            checks = await _check_ollama(tmp_path)

        model_check = [c for c in checks if "llama3.1" in c[0]]
        assert model_check
        assert model_check[0][1] is True

    async def test_ollama_model_not_pulled(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_ollama

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.cli.commands.doctor.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.cli.commands.doctor.run", new_callable=AsyncMock) as mock_run,
        ):
            _mock_llm_config(mock_cfg, routing={"triage": "ollama/qwen3:8b"})
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

    def test_input_rate_per_mtok_known_model(self) -> None:
        from sova.llm.models import input_rate_per_mtok

        assert input_rate_per_mtok("claude-sonnet-5") == Decimal("2")

    def test_input_rate_per_mtok_unknown_model(self) -> None:
        from sova.llm.models import input_rate_per_mtok

        assert input_rate_per_mtok("gpt-4o") == Decimal("0")

    def test_input_rate_per_mtok_bare_alias_resolves(self) -> None:
        """Bare family aliases (the config default, e.g. "opus") must resolve to
        a nonzero rate; otherwise the compression-savings estimate is always $0."""
        from sova.llm.models import input_rate_per_mtok

        assert input_rate_per_mtok("opus") == Decimal("5")
        assert input_rate_per_mtok("sonnet") == Decimal("2")
        assert input_rate_per_mtok("haiku") == Decimal("1")

    def test_resolve_model_alias_known(self) -> None:
        from sova.llm.models import resolve_model_alias

        assert resolve_model_alias("sonnet") == "claude-sonnet-5"
        assert resolve_model_alias("opus") == "claude-opus-5"
        assert resolve_model_alias("haiku") == "claude-haiku-4-5-20251001"
        assert resolve_model_alias("cheap") == "claude-haiku-4-5-20251001"

    def test_resolve_model_alias_passes_through_unknown(self) -> None:
        from sova.llm.models import resolve_model_alias

        assert resolve_model_alias("claude-sonnet-5") == "claude-sonnet-5"
        assert resolve_model_alias("ollama/qwen3:8b") == "ollama/qwen3:8b"
        assert resolve_model_alias("") == ""

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


class _MockAnthropicModelEntry:
    def __init__(self, id: str, display_name: str = "") -> None:  # noqa: A002 (mirrors the SDK's own field name)
        self.id = id
        self.display_name = display_name


class _MockAnthropicModelsPage:
    """Minimal async-iterable double for the SDK's AsyncPage[ModelInfo]."""

    def __init__(self, models: list) -> None:
        self._models = models

    def __aiter__(self):
        return self._agen()

    async def _agen(self):
        for m in self._models:
            yield m


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

    def test_capabilities(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            caps = AnthropicAPIProvider().capabilities
        assert caps.supports_cli_fallback is False
        assert caps.supports_budget_cap is False
        assert caps.reports_cost is False
        # True since list_available_models() enumerates the account's real
        # catalog via the SDK's models.list().
        assert caps.dynamic_models is True

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

    async def test_invoke_sdk_error_maps_to_typed_error(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.errors import RateLimitError
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.create = AsyncMock(
            side_effect=type("RateLimitError", (Exception,), {})("slow down"),
        )

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            with pytest.raises(RateLimitError, match="Anthropic API error"):
                await provider.invoke("Hello")

    async def test_streaming_sdk_error_yields_result_then_raises_typed(self, mock_anthropic: MagicMock) -> None:
        """The partial result event is still emitted before the typed raise."""
        from sova.llm.errors import ProviderUnavailableError
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.stream = MagicMock(side_effect=ConnectionRefusedError("daemon down"))

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            events = []
            with pytest.raises(ProviderUnavailableError, match="Anthropic streaming error"):
                async for event in provider.invoke_streaming("Hello"):
                    events.append(event)

        assert [e.type for e in events] == ["result"]
        assert events[0].result is not None
        assert events[0].result.stop_reason == "error"

    async def test_invoke_sdk_error_stays_catchable_as_runtime_error(self, mock_anthropic: MagicMock) -> None:
        """Existing `except RuntimeError` call sites keep catching provider failures."""
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.messages.create = AsyncMock(side_effect=Exception("upstream exploded"))

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            provider = AnthropicAPIProvider()
            with pytest.raises(RuntimeError, match="upstream exploded"):
                await provider.invoke("Hello")

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
        from sova.config.models import LLMConfig
        from sova.llm.provider import create_provider

        with (
            patch.dict(os.environ, {}, clear=False),
            patch("sova.llm.keyring_store.get_secret", return_value=None),
        ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            provider = create_provider(LLMConfig(provider="anthropic", model="claude-opus-5", api_key="sk-from-config"))

        assert provider._api_key == "sk-from-config"
        assert provider._default_model == "claude-opus-5"

    def test_create_provider_prefers_keyring_over_db_api_key(self, mock_anthropic: MagicMock) -> None:
        """When the OS keyring holds the key, it wins over the plaintext db value."""
        from sova.config.models import LLMConfig
        from sova.llm.provider import create_provider

        with patch("sova.llm.keyring_store.get_secret", return_value="sk-from-keyring"):
            provider = create_provider(LLMConfig(provider="anthropic", api_key="sk-from-db"))

        assert provider._api_key == "sk-from-keyring"

    def test_create_provider_falls_back_to_db_when_sentinel_unresolvable(self, mock_anthropic: MagicMock) -> None:
        """A sentinel db value with no matching keyring entry resolves to empty, not the sentinel."""
        from sova.config.models import LLMConfig
        from sova.llm.keyring_store import SENTINEL
        from sova.llm.provider import create_provider

        with (
            patch.dict(os.environ, {}, clear=False),
            patch("sova.llm.keyring_store.get_secret", return_value=None),
        ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            provider = create_provider(LLMConfig(provider="anthropic", api_key=SENTINEL))

        assert provider._api_key == ""

    def test_create_provider_resolves_model_alias(self, mock_anthropic: MagicMock) -> None:
        """llm.model may be an alias name, not just a native ID."""
        from sova.config.models import LLMConfig
        from sova.llm.provider import create_provider

        provider = create_provider(
            LLMConfig(
                provider="anthropic",
                model="smart",
                model_aliases={"smart": "claude-opus-5"},
            )
        )
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

    async def test_list_available_models_maps_sdk_results(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.client import reset_availability_cache
        from sova.llm.models import ModelFamily
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.models.list = AsyncMock(
            return_value=_MockAnthropicModelsPage(
                [
                    _MockAnthropicModelEntry("claude-opus-5", "Claude Opus 5"),
                    _MockAnthropicModelEntry("mystery-model-9000"),
                ]
            )
        )

        reset_availability_cache()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            models = await AnthropicAPIProvider().list_available_models()
        reset_availability_cache()

        assert {m.id for m in models} == {"claude-opus-5", "mystery-model-9000"}
        opus = next(m for m in models if m.id == "claude-opus-5")
        assert opus.family == ModelFamily.ANTHROPIC
        assert opus.tier == "smart"
        assert opus.display_name == "Claude Opus 5"
        assert opus.source == "anthropic_api"
        mystery = next(m for m in models if m.id == "mystery-model-9000")
        assert mystery.family == ModelFamily.UNKNOWN
        assert mystery.tier == ""
        assert mystery.display_name == "mystery-model-9000"

    async def test_list_available_models_allow_probe_false_skips_network(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.models import CURATED_MODELS
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.models.list = AsyncMock(side_effect=AssertionError("must not be called"))

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            models = await AnthropicAPIProvider().list_available_models(allow_probe=False)

        assert models == list(CURATED_MODELS)
        client.models.list.assert_not_called()

    async def test_list_available_models_no_api_key_returns_curated(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.models import CURATED_MODELS
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            models = await AnthropicAPIProvider(api_key="").list_available_models()

        assert models == list(CURATED_MODELS)

    async def test_list_available_models_sdk_failure_falls_back_to_curated(self, mock_anthropic: MagicMock) -> None:
        from sova.llm.client import reset_availability_cache
        from sova.llm.models import CURATED_MODELS
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        client = mock_anthropic.AsyncAnthropic.return_value
        client.models.list = AsyncMock(side_effect=RuntimeError("boom"))

        reset_availability_cache()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            models = await AnthropicAPIProvider().list_available_models()
        reset_availability_cache()

        assert models == list(CURATED_MODELS)

    async def test_list_available_models_empty_catalog_is_not_cached(self, mock_anthropic: MagicMock) -> None:
        """An authenticated account that lists no model at all is a service-side
        anomaly, handled like the other providers' outage path: curated is
        served, but never cached as this deployment's answer."""
        from sova.llm.client import get_availability_cache, reset_availability_cache
        from sova.llm.models import CURATED_MODELS
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider, _anthropic_api_enumeration_identity

        client = mock_anthropic.AsyncAnthropic.return_value
        client.models.list = AsyncMock(return_value=_MockAnthropicModelsPage([]))

        reset_availability_cache()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            models = await AnthropicAPIProvider().list_available_models()
            cached = get_availability_cache().get_enumeration(_anthropic_api_enumeration_identity("sk-test-key"))
        reset_availability_cache()

        assert models == list(CURATED_MODELS)
        assert cached is None
        assert client.models.list.await_count == 1


class TestCreateProviderAnthropic:
    def test_create_provider_anthropic(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.provider import create_provider

        with (
            patch.dict("sys.modules", {"anthropic": MagicMock(__version__="0.39.0")}),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}),
        ):
            import sova.llm.providers.anthropic_api as api_mod

            api_mod._HAS_ANTHROPIC = True
            api_mod.anthropic = MagicMock()

            from sova.llm.providers.anthropic_api import AnthropicAPIProvider

            provider = create_provider(LLMConfig(provider="anthropic"))
            assert isinstance(provider, AnthropicAPIProvider)

    def test_create_provider_anthropic_in_available_list(self) -> None:
        from sova.config.models import LLMConfig
        from sova.llm.provider import create_provider

        with pytest.raises(ValueError, match="anthropic") as exc_info:
            create_provider(LLMConfig.model_construct(provider="nonexistent"))
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
        mock_compress.assert_called_once_with("user prompt", None, cfg=None)
        assert provider.invoke.call_args.kwargs["system_prompt"] == "system prompt"

    async def test_invoke_command_compresses_args_only(self) -> None:
        from sova.llm import client

        provider = MagicMock()
        provider.invoke_command = AsyncMock(return_value=LLMResult(text="ok", model="test"))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "maybe_compress", return_value="COMPRESSED_ARGS") as mock_compress,
            patch.object(client, "_try_load_config", return_value=None),
        ):
            await client.invoke_command("/develop", args="42")
        mock_compress.assert_called_once_with("42", None, cfg=None)
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
            patch.object(
                client, "maybe_compress", side_effect=lambda p, cwd=None, cfg=None: p.upper()
            ) as mock_compress,
            patch.object(client, "_try_load_config", return_value=None) as mock_load,
            patch("sova.llm.providers.anthropic_batch.create_batch_provider", return_value=None),
        ):
            await client.invoke_batch(reqs)
        assert mock_compress.call_count == 2
        # Config is loaded once for the whole batch, not once per request: each
        # load opens a DB connection and reparses sova.toml.
        assert mock_load.call_count == 1
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


# ---------------------------------------------------------------------------
# Client: compression savings recording on invoke()
# ---------------------------------------------------------------------------


class TestCompressionSavingsRecording:
    async def _invoke_with_compression(self, original: str, compressed: str, input_tokens: int = 0) -> LLMResult:
        from sova.llm import client

        provider = MagicMock()
        provider.invoke = AsyncMock(return_value=LLMResult(text="ok", model="test", input_tokens=input_tokens))
        with (
            patch.object(client, "get_provider", return_value=provider),
            patch.object(client, "maybe_compress", return_value=compressed),
            patch.object(client, "_try_load_config", return_value=None),
        ):
            return await client.invoke(original)

    async def test_records_savings_when_compressed(self) -> None:
        result = await self._invoke_with_compression("x" * 400, "y" * 100, input_tokens=1000)
        assert result.tokens_saved == 75  # (400 - 100) // 4
        assert result.pre_compression_input_tokens == 1075

    async def test_identity_passthrough_leaves_columns_null(self) -> None:
        from sova.llm import client

        provider = MagicMock()
        provider.invoke = AsyncMock(return_value=LLMResult(text="ok", model="test", input_tokens=500))
        prompt = "x" * 400
        with (
            patch.object(client, "get_provider", return_value=provider),
            # maybe_compress returns the exact same object -> compression not applied.
            patch.object(client, "maybe_compress", side_effect=lambda p, cwd=None, cfg=None: p),
            patch.object(client, "_try_load_config", return_value=None),
        ):
            result = await client.invoke(prompt)
        assert result.tokens_saved is None
        assert result.pre_compression_input_tokens is None

    async def test_expanded_payload_clamps_to_zero(self) -> None:
        result = await self._invoke_with_compression("short", "longer output", input_tokens=200)
        assert result.tokens_saved == 0
        assert result.pre_compression_input_tokens == 200


class TestClaudeCodeAuthAwareness:
    """check_available() must report authentication, not just installation.

    An installed but logged-out CLI passes --version and then fails every
    agent run at invocation time.
    """

    @staticmethod
    def _results(version_out: str, auth_out: str, auth_rc: int = 0):
        from sova.utils.shell import ShellResult

        return [
            ShellResult(returncode=0, stdout=version_out, stderr=""),
            ShellResult(returncode=auth_rc, stdout=auth_out, stderr=""),
        ]

    async def test_logged_in_reports_account_and_subscription(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        auth = '{"loggedIn": true, "email": "dev@example.com", "subscriptionType": "max"}'
        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch(
                "sova.llm.providers.claude_code.run",
                new_callable=AsyncMock,
                side_effect=self._results("2.1.259 (Claude Code)\n", auth),
            ),
        ):
            available, detail = await ClaudeCodeProvider().check_available()

        assert available is True
        assert "2.1.259" in detail
        assert "dev@example.com" in detail
        assert "max" in detail

    async def test_logged_out_is_unavailable(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch(
                "sova.llm.providers.claude_code.run",
                new_callable=AsyncMock,
                side_effect=self._results("2.1.259\n", '{"loggedIn": false}'),
            ),
        ):
            available, detail = await ClaudeCodeProvider().check_available()

        assert available is False
        assert "claude auth login" in detail

    async def test_version_probe_uses_auth_check_timeout(self) -> None:
        """The `claude --version` probe must not inherit run()'s 300s default:
        get_auth_status() holds its per-project lock for the whole call, so an
        unresponsive CLI would otherwise stall every widget poll for 5 minutes."""
        from sova.llm.providers.claude_code import _AUTH_CHECK_TIMEOUT, ClaudeCodeProvider

        auth = '{"loggedIn": true, "email": "dev@example.com", "subscriptionType": "max"}'
        mock_run = AsyncMock(side_effect=self._results("2.1.259\n", auth))
        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch("sova.llm.providers.claude_code.run", mock_run),
        ):
            await ClaudeCodeProvider().check_available()

        version_call = mock_run.await_args_list[0]
        assert version_call.kwargs["timeout"] == _AUTH_CHECK_TIMEOUT

    async def test_missing_auth_subcommand_fails_open(self) -> None:
        """Older CLI builds have no `auth` subcommand; do not block them."""
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch(
                "sova.llm.providers.claude_code.run",
                new_callable=AsyncMock,
                side_effect=self._results("1.0.0\n", "", auth_rc=1),
            ),
        ):
            available, detail = await ClaudeCodeProvider().check_available()

        assert available is True
        assert "auth state unknown" in detail

    async def test_unparseable_auth_output_fails_open(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch(
                "sova.llm.providers.claude_code.run",
                new_callable=AsyncMock,
                side_effect=self._results("1.0.0\n", "not json at all"),
            ),
        ):
            available, detail = await ClaudeCodeProvider().check_available()

        assert available is True
        assert "auth state unknown" in detail

    async def test_non_dict_auth_payload_fails_open(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch(
                "sova.llm.providers.claude_code.run",
                new_callable=AsyncMock,
                side_effect=self._results("1.0.0\n", "[1, 2, 3]"),
            ),
        ):
            available, detail = await ClaudeCodeProvider().check_available()

        assert available is True
        assert "auth state unknown" in detail

    async def test_missing_logged_in_key_fails_open(self) -> None:
        """A dict without ``loggedIn`` (e.g. unrecognized future CLI output) must not
        be treated as logged out: only an explicit `false` means unavailable."""
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch(
                "sova.llm.providers.claude_code.run",
                new_callable=AsyncMock,
                side_effect=self._results("1.0.0\n", "{}"),
            ),
        ):
            available, detail = await ClaudeCodeProvider().check_available()

        assert available is True
        assert "auth state unknown" in detail

    async def test_non_boolean_logged_in_fails_open(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch(
                "sova.llm.providers.claude_code.run",
                new_callable=AsyncMock,
                side_effect=self._results("1.0.0\n", '{"loggedIn": "yes"}'),
            ),
        ):
            available, detail = await ClaudeCodeProvider().check_available()

        assert available is True
        assert "auth state unknown" in detail

    async def test_logged_in_without_account_details(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch(
                "sova.llm.providers.claude_code.run",
                new_callable=AsyncMock,
                side_effect=self._results("1.0.0\n", '{"loggedIn": true}'),
            ),
        ):
            available, detail = await ClaudeCodeProvider().check_available()

        assert available is True
        assert "authenticated" in detail

    async def test_version_failure_skips_auth_check(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        mock_run = AsyncMock(return_value=ShellResult(returncode=1, stdout="", stderr="boom"))
        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch("sova.llm.providers.claude_code.run", mock_run),
        ):
            available, _ = await ClaudeCodeProvider().check_available()

        assert available is False
        assert mock_run.await_count == 1

    async def test_cli_invocations_receive_scrubbed_env(self) -> None:
        """Both the version and auth probes must run with a sanitized environment."""
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        auth = '{"loggedIn": true, "email": "d@e.com", "subscriptionType": "pro"}'
        mock_run = AsyncMock(side_effect=self._results("1.0.0\n", auth))
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_USE_VERTEX": "1"}, clear=False),
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch("sova.llm.providers.claude_code.run", mock_run),
        ):
            await ClaudeCodeProvider().check_available()

        assert mock_run.await_count == 2
        for call in mock_run.await_args_list:
            assert "CLAUDE_CODE_USE_VERTEX" not in call.kwargs["env"]

    async def test_cli_invocations_honor_configured_passthrough(self) -> None:
        """A deployment that opts CLAUDE_CODE_USE_VERTEX back in must see it on every probe.

        Regression guard: the provider's own scrub_agent_env() calls used to ignore
        agent.env_passthrough entirely, so a Vertex/Bedrock deployment that opted the
        variable back in for spawned agents (sova/ipc/runtime.py) still had it stripped
        here, breaking check_available() and invoke() for that same deployment.
        """
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        auth = '{"loggedIn": true, "email": "d@e.com", "subscriptionType": "pro"}'
        mock_run = AsyncMock(side_effect=self._results("1.0.0\n", auth))
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_USE_VERTEX": "1"}, clear=False),
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch("sova.llm.providers.claude_code.run", mock_run),
            patch(
                "sova.llm.providers.claude_code.configured_passthrough",
                return_value=("CLAUDE_CODE_USE_VERTEX",),
            ),
        ):
            await ClaudeCodeProvider().check_available()

        assert mock_run.await_count == 2
        for call in mock_run.await_args_list:
            assert call.kwargs["env"]["CLAUDE_CODE_USE_VERTEX"] == "1"


class TestClaudeCodeGetAuthDetails:
    """get_auth_details() shares its probe with check_available() via _probe_auth()."""

    async def test_logged_in_returns_full_payload(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        auth = (
            '{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", '
            '"email": "dev@example.com", "orgId": "org_1", "orgName": "Example Org", '
            '"subscriptionType": "max"}'
        )
        with patch(
            "sova.llm.providers.claude_code.run",
            new_callable=AsyncMock,
            return_value=ShellResult(returncode=0, stdout=auth, stderr=""),
        ):
            details = await ClaudeCodeProvider().get_auth_details()

        assert details is not None
        assert details["email"] == "dev@example.com"
        assert details["orgName"] == "Example Org"
        assert details["subscriptionType"] == "max"

    async def test_logged_out_returns_none(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        with patch(
            "sova.llm.providers.claude_code.run",
            new_callable=AsyncMock,
            return_value=ShellResult(returncode=0, stdout='{"loggedIn": false}', stderr=""),
        ):
            details = await ClaudeCodeProvider().get_auth_details()

        assert details is None

    async def test_probe_failure_returns_none(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        with patch(
            "sova.llm.providers.claude_code.run",
            new_callable=AsyncMock,
            return_value=ShellResult(returncode=1, stdout="", stderr="no such subcommand"),
        ):
            details = await ClaudeCodeProvider().get_auth_details()

        assert details is None

    async def test_missing_cli_on_fresh_probe_returns_none(self) -> None:
        """A fresh get_auth_details() (no preceding check_available()) must not
        propagate the FileNotFoundError create_subprocess_exec raises when the
        `claude` executable is absent; it degrades to the documented None."""
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        with patch(
            "sova.llm.providers.claude_code.run",
            new_callable=AsyncMock,
            side_effect=FileNotFoundError("claude"),
        ):
            details = await ClaudeCodeProvider().get_auth_details()

        assert details is None

    async def test_unparseable_output_returns_none(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        with patch(
            "sova.llm.providers.claude_code.run",
            new_callable=AsyncMock,
            return_value=ShellResult(returncode=0, stdout="not json", stderr=""),
        ):
            details = await ClaudeCodeProvider().get_auth_details()

        assert details is None

    async def test_missing_logged_in_key_returns_none(self) -> None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        with patch(
            "sova.llm.providers.claude_code.run",
            new_callable=AsyncMock,
            return_value=ShellResult(returncode=0, stdout="{}", stderr=""),
        ):
            details = await ClaudeCodeProvider().get_auth_details()

        assert details is None

    async def test_single_subprocess_call(self) -> None:
        """get_auth_details() makes exactly one CLI call, not a duplicate probe."""
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        mock_run = AsyncMock(
            return_value=ShellResult(returncode=0, stdout='{"loggedIn": true, "email": "d@e.com"}', stderr="")
        )
        with patch("sova.llm.providers.claude_code.run", mock_run):
            await ClaudeCodeProvider().get_auth_details()

        assert mock_run.await_count == 1

    async def test_reuses_probe_from_preceding_check_available(self) -> None:
        """setup_service.get_auth_status() calls check_available() then
        get_auth_details() on the same provider instance; the second call must
        not spawn a second `claude auth status --json` process for data
        already fetched by the first."""
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        auth = '{"loggedIn": true, "email": "dev@example.com", "subscriptionType": "max"}'
        mock_run = AsyncMock(
            side_effect=[
                ShellResult(returncode=0, stdout="2.1.259\n", stderr=""),
                ShellResult(returncode=0, stdout=auth, stderr=""),
            ]
        )
        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch("sova.llm.providers.claude_code.run", mock_run),
        ):
            provider = ClaudeCodeProvider()
            await provider.check_available()
            details = await provider.get_auth_details()

        assert mock_run.await_count == 2  # --version + one auth status, not two
        assert details is not None
        assert details["email"] == "dev@example.com"

    async def test_no_second_probe_after_check_available_finds_cli_unavailable(self) -> None:
        """When check_available() fails before ever reaching the auth probe
        (CLI missing or --version failing), get_auth_details() on the same
        instance must not spawn a second, guaranteed-to-fail
        `claude auth status --json` process for data that was never fetched."""
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        mock_run = AsyncMock(return_value=ShellResult(returncode=1, stdout="", stderr="boom"))
        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch("sova.llm.providers.claude_code.run", mock_run),
        ):
            provider = ClaudeCodeProvider()
            available, _ = await provider.check_available()
            details = await provider.get_auth_details()

        assert available is False
        assert details is None
        assert mock_run.await_count == 1  # only --version; no auth probe attempted at all

    async def test_stale_probe_not_served_after_later_check_available_failure(self) -> None:
        """A second check_available() call that fails must invalidate the
        cached probe from an earlier successful call on the same instance, so
        get_auth_details() never serves stale account data."""
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        ok_auth = '{"loggedIn": true, "email": "dev@example.com", "subscriptionType": "max"}'
        mock_run = AsyncMock(
            side_effect=[
                ShellResult(returncode=0, stdout="2.1.259\n", stderr=""),  # 1st check_available: --version
                ShellResult(returncode=0, stdout=ok_auth, stderr=""),  # 1st check_available: auth probe
                ShellResult(returncode=1, stdout="", stderr="boom"),  # 2nd check_available: --version fails
            ]
        )
        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch("sova.llm.providers.claude_code.run", mock_run),
        ):
            provider = ClaudeCodeProvider()
            await provider.check_available()
            available, _ = await provider.check_available()
            details = await provider.get_auth_details()

        assert available is False
        assert details is None
        assert mock_run.await_count == 3  # no extra auth probe spawned for the now-stale data


# ---------------------------------------------------------------------------
# ModelInfo / ModelFamily / model classification (sova/llm/models.py)
# ---------------------------------------------------------------------------


class TestClassifyModelFamily:
    @pytest.mark.parametrize(
        ("model_id", "expected"),
        [
            ("claude-opus-5", "anthropic"),
            ("claude-sonnet-4-6", "anthropic"),
            ("gemini-2.5-pro", "google"),
            ("gpt-oss-120b", "openai-oss"),
            ("gpt-4o", "openai"),
            ("o3-mini", "openai"),
            ("chatgpt-4o-latest", "openai"),
            ("ollama/llama3", "local"),
            ("vllm/mistral-7b", "local"),
            ("mystery-model-9000", "unknown"),
        ],
    )
    def test_classify(self, model_id: str, expected: str) -> None:
        from sova.llm.models import classify_model_family

        assert classify_model_family(model_id) == expected

    def test_case_insensitive(self) -> None:
        from sova.llm.models import ModelFamily, classify_model_family

        assert classify_model_family("CLAUDE-OPUS-5") == ModelFamily.ANTHROPIC

    def test_gpt_oss_not_classified_as_openai(self) -> None:
        """The Vertex openai publisher only ever serves gpt-oss; it must never
        collapse into the same family as a real GPT/o-series model."""
        from sova.llm.models import ModelFamily, classify_model_family

        assert classify_model_family("gpt-oss-20b") == ModelFamily.OPENAI_OSS
        assert classify_model_family("gpt-4o") == ModelFamily.OPENAI
        assert classify_model_family("gpt-oss-20b") != classify_model_family("gpt-4o")


class TestModelTierForId:
    def test_known_ids(self) -> None:
        from sova.llm.models import model_tier_for_id

        assert model_tier_for_id("claude-opus-5") == "smart"
        assert model_tier_for_id("claude-sonnet-5") == "fast"
        assert model_tier_for_id("claude-haiku-4-5-20251001") == "cheap"

    def test_unknown_id_returns_empty_string(self) -> None:
        from sova.llm.models import model_tier_for_id

        assert model_tier_for_id("gpt-4o") == ""
        assert model_tier_for_id("claude-fable-5") == ""


class TestCuratedModels:
    def test_all_entries_are_curated_anthropic(self) -> None:
        from sova.llm.models import CURATED_MODELS, ModelFamily

        assert len(CURATED_MODELS) > 0
        for model in CURATED_MODELS:
            assert model.family == ModelFamily.ANTHROPIC
            assert model.source == "curated"
            assert model.id

    def test_includes_the_generic_tiers(self) -> None:
        from sova.llm.models import CURATED_MODELS

        tiers = {m.tier for m in CURATED_MODELS}
        assert {"smart", "fast", "cheap"}.issubset(tiers)


# ---------------------------------------------------------------------------
# LLMProvider.list_available_models(): concrete ABC default
# ---------------------------------------------------------------------------


def _make_fake_provider():
    from sova.llm.provider import LLMProvider

    class FakeProvider(LLMProvider):
        async def invoke(self, prompt, **kwargs):
            return LLMResult(text="fake", model="fake")

        async def invoke_streaming(self, prompt, **kwargs):
            yield StreamEvent(type="result", text="fake")

        async def check_available(self):
            return True, "fake"

    return FakeProvider()


class TestLLMProviderDefaultListAvailableModels:
    async def test_default_returns_curated_list(self) -> None:
        from sova.llm.models import CURATED_MODELS

        models = await _make_fake_provider().list_available_models()
        assert models == list(CURATED_MODELS)

    async def test_default_ignores_allow_probe_false(self) -> None:
        from sova.llm.models import CURATED_MODELS

        models = await _make_fake_provider().list_available_models(allow_probe=False)
        assert models == list(CURATED_MODELS)

    def test_not_abstract(self) -> None:
        """Do NOT make list_available_models() abstract: existing and
        third-party LLMProvider subclasses that predate this method must keep
        working without implementing it."""
        from sova.llm.provider import LLMProvider

        assert "list_available_models" not in LLMProvider.__abstractmethods__


# ---------------------------------------------------------------------------
# _credential_safe_target(): bearer credential channel gate
# ---------------------------------------------------------------------------


class TestCredentialSafeTarget:
    @pytest.mark.parametrize(
        "url",
        [
            "https://api.example.com/v1/models",
            "http://localhost:8000/v1/models",
            "http://127.0.0.1:8000/v1/models",
            "http://[::1]:8000/v1/models",
        ],
    )
    def test_safe_targets(self, url: str) -> None:
        from sova.llm.litellm_provider import _credential_safe_target

        assert _credential_safe_target(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://models.example.com/v1/models",
            "http://192.168.1.5:8000/v1/models",
            "http://not a valid host/v1/models",
        ],
    )
    def test_unsafe_targets(self, url: str) -> None:
        from sova.llm.litellm_provider import _credential_safe_target

        assert _credential_safe_target(url) is False


# ---------------------------------------------------------------------------
# ModelAvailabilityCache enumeration/probe-outcome storage
# ---------------------------------------------------------------------------


class TestModelAvailabilityCacheEnumeration:
    def test_enumeration_round_trip(self) -> None:
        from sova.llm.client import ModelAvailabilityCache
        from sova.llm.models import CURATED_MODELS

        cache = ModelAvailabilityCache()
        assert cache.get_enumeration("id1") is None
        cache.set_enumeration("id1", list(CURATED_MODELS))
        assert cache.get_enumeration("id1") == list(CURATED_MODELS)

    def test_enumeration_expires_after_its_own_ttl(self) -> None:
        from sova.llm.client import ModelAvailabilityCache
        from sova.llm.models import CURATED_MODELS

        cache = ModelAvailabilityCache(enumeration_ttl_seconds=-1)
        cache.set_enumeration("id1", list(CURATED_MODELS))
        assert cache.get_enumeration("id1") is None

    def test_probe_outcome_round_trip(self) -> None:
        from sova.llm.client import ModelAvailabilityCache

        cache = ModelAvailabilityCache()
        assert cache.get_probe_outcome("id1", "model-a") is None
        cache.set_probe_outcome("id1", "model-a", True)
        assert cache.get_probe_outcome("id1", "model-a") is True
        cache.set_probe_outcome("id1", "model-b", False)
        assert cache.get_probe_outcome("id1", "model-b") is False

    def test_probe_outcome_expires_after_its_own_ttl(self) -> None:
        from sova.llm.client import ModelAvailabilityCache

        cache = ModelAvailabilityCache(enumeration_ttl_seconds=-1)
        cache.set_probe_outcome("id1", "model-a", True)
        assert cache.get_probe_outcome("id1", "model-a") is None

    def test_enumeration_ttl_is_independent_of_negative_cache_ttl(self) -> None:
        """A new _ENUMERATION_TTL_SECONDS, separate from the reactive negative
        cache's fast-expiry TTL: the two must never share a lifetime."""
        from sova.llm.client import ModelAvailabilityCache

        cache = ModelAvailabilityCache(ttl_seconds=-1, enumeration_ttl_seconds=10_000)
        cache.mark_unavailable("id1", "model-a")
        assert cache.is_unavailable("id1", "model-a") is False  # fast TTL already expired

        cache.set_probe_outcome("id1", "model-a", True)
        assert cache.get_probe_outcome("id1", "model-a") is True  # enumeration TTL still alive

    def test_failed_probe_outcome_uses_the_short_negative_ttl(self) -> None:
        """A whole-list probe wipeout makes cached_enumeration() serve curated
        without caching it, precisely so the next call retries. Pinning the
        negative probe answers for the long enumeration TTL would leave that
        retry with nothing to re-probe, so failures expire on the short TTL."""
        from sova.llm.client import ModelAvailabilityCache

        cache = ModelAvailabilityCache(ttl_seconds=-1, enumeration_ttl_seconds=10_000)
        cache.set_probe_outcome("id1", "works", True)
        cache.set_probe_outcome("id1", "broken", False)

        assert cache.get_probe_outcome("id1", "works") is True
        assert cache.get_probe_outcome("id1", "broken") is None  # re-probed on the next pass

    def test_enumeration_lock_is_stable_per_identity(self) -> None:
        from sova.llm.client import ModelAvailabilityCache

        cache = ModelAvailabilityCache()
        lock1 = cache.enumeration_lock("id1")
        lock2 = cache.enumeration_lock("id1")
        lock3 = cache.enumeration_lock("id2")
        assert lock1 is lock2
        assert lock1 is not lock3

    def test_reset_clears_enumeration_and_probe_state(self) -> None:
        from sova.llm.client import ModelAvailabilityCache
        from sova.llm.models import CURATED_MODELS

        cache = ModelAvailabilityCache()
        cache.set_enumeration("id1", list(CURATED_MODELS))
        cache.set_probe_outcome("id1", "model-a", True)
        cache.enumeration_lock("id1")

        cache.reset()

        assert cache.get_enumeration("id1") is None
        assert cache.get_probe_outcome("id1", "model-a") is None

    def test_clear_enumeration_drops_enumeration_but_not_probe_outcomes(self) -> None:
        """A forced refresh clears the enumeration pass but must not erase a
        per-model probe outcome: a model that worked is a stable positive
        fact independent of which pass discovered it."""
        from sova.llm.client import ModelAvailabilityCache
        from sova.llm.models import CURATED_MODELS

        cache = ModelAvailabilityCache()
        cache.set_enumeration("id1", list(CURATED_MODELS))
        cache.set_probe_outcome("id1", "model-a", True)

        cache.clear_enumeration()

        assert cache.get_enumeration("id1") is None
        assert cache.get_probe_outcome("id1", "model-a") is True

    def test_module_level_clear_enumeration_targets_the_shared_cache(self) -> None:
        from sova.llm.client import clear_enumeration, get_availability_cache, reset_availability_cache
        from sova.llm.models import CURATED_MODELS

        reset_availability_cache()
        get_availability_cache().set_enumeration("id1", list(CURATED_MODELS))
        get_availability_cache().set_probe_outcome("id1", "model-a", True)

        clear_enumeration()

        assert get_availability_cache().get_enumeration("id1") is None
        assert get_availability_cache().get_probe_outcome("id1", "model-a") is True
        reset_availability_cache()

    def test_get_enumeration_returns_a_copy(self) -> None:
        """The cached list is handed straight to callers, so an in-place edit by
        one caller must not corrupt every later read for the whole TTL."""
        from sova.llm.client import ModelAvailabilityCache
        from sova.llm.models import CURATED_MODELS

        cache = ModelAvailabilityCache()
        stored = list(CURATED_MODELS)
        cache.set_enumeration("id1", stored)

        stored.clear()  # the caller's own list must not reach into the cache
        handed_out = cache.get_enumeration("id1")
        assert handed_out == list(CURATED_MODELS)

        handed_out.clear()  # nor must a mutation of what was handed out
        assert cache.get_enumeration("id1") == list(CURATED_MODELS)


# ---------------------------------------------------------------------------
# VertexTokenProvider (sova/llm/gcp_auth.py)
# ---------------------------------------------------------------------------


class TestVertexTokenProvider:
    async def test_import_error_when_google_auth_missing(self) -> None:
        from sova.llm.gcp_auth import VertexTokenProvider

        with patch.dict("sys.modules", {"google.auth": None, "google.auth.transport.requests": None}):
            provider = VertexTokenProvider()
            with pytest.raises(ImportError, match="google-auth"):
                await provider.get_token()

    @staticmethod
    def _mocked_google_auth_modules(mock_google_auth: MagicMock) -> dict[str, MagicMock]:
        """Build a sys.modules patch dict where attribute traversal (not just
        the module cache) resolves correctly: ``import google.auth`` binds the
        local name ``google`` to whatever sys.modules['google'] is, so the
        ``.auth`` attribute on that object must be wired to the same mock
        sys.modules['google.auth'] holds, not left to the real (auth-less)
        namespace package or a second unrelated auto-generated MagicMock."""
        mock_google = MagicMock()
        mock_google.auth = mock_google_auth
        return {
            "google": mock_google,
            "google.auth": mock_google_auth,
            "google.auth.transport": mock_google_auth.transport,
            "google.auth.transport.requests": mock_google_auth.transport.requests,
        }

    async def test_fetches_and_reuses_credentials(self) -> None:
        from sova.llm.gcp_auth import VertexTokenProvider

        mock_creds = MagicMock()
        mock_creds.token = "tok-1"
        mock_creds.expired = False

        mock_google_auth = MagicMock()
        mock_google_auth.default.return_value = (mock_creds, "proj")

        with patch.dict("sys.modules", self._mocked_google_auth_modules(mock_google_auth)):
            provider = VertexTokenProvider()
            first = await provider.get_token()
            second = await provider.get_token()

        assert first == "tok-1"
        assert second == "tok-1"
        mock_google_auth.default.assert_called_once()  # credentials resolved once, then reused

    async def test_refreshes_expired_credentials(self) -> None:
        from sova.llm.gcp_auth import VertexTokenProvider

        mock_creds = MagicMock()
        mock_creds.token = ""
        mock_creds.expired = True

        def _refresh(request):
            mock_creds.token = "refreshed-tok"
            mock_creds.expired = False

        mock_creds.refresh.side_effect = _refresh

        mock_google_auth = MagicMock()
        mock_google_auth.default.return_value = (mock_creds, "proj")

        with patch.dict("sys.modules", self._mocked_google_auth_modules(mock_google_auth)):
            provider = VertexTokenProvider()
            token = await provider.get_token()

        assert token == "refreshed-tok"
        mock_creds.refresh.assert_called_once()


# ---------------------------------------------------------------------------
# ClaudeCodeProvider.list_available_models()
# ---------------------------------------------------------------------------


class TestClaudeCodeListAvailableModels:
    @pytest.fixture(autouse=True)
    def _reset_model_cache(self):
        from sova.llm.client import reset_availability_cache

        reset_availability_cache()
        yield
        reset_availability_cache()

    @staticmethod
    def _run_side_effect(available_ids: set[str] | None = None, logged_in: bool = True):
        """Build an async run() replacement keyed off argv, not call order.

        *available_ids*: None means every probed model succeeds; otherwise
        only ids in the set report success.
        """

        async def _run(*args, **kwargs):
            from sova.utils.shell import ShellResult

            if "auth" in args:
                if not logged_in:
                    return ShellResult(returncode=0, stdout='{"loggedIn": false}', stderr="")
                return ShellResult(
                    returncode=0,
                    stdout='{"loggedIn": true, "email": "dev@example.com", "subscriptionType": "max"}',
                    stderr="",
                )
            if "--model" in args:
                idx = args.index("--model")
                model_id = args[idx + 1]
                ok = available_ids is None or model_id in available_ids
                return ShellResult(returncode=0 if ok else 1, stdout="{}", stderr="boom" if not ok else "")
            return ShellResult(returncode=0, stdout="{}", stderr="")

        return _run

    async def test_allow_probe_false_returns_curated_without_running_anything(self) -> None:
        from sova.llm.models import CURATED_MODELS
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        mock_run = AsyncMock(side_effect=self._run_side_effect())
        with patch("sova.llm.providers.claude_code.run", mock_run):
            models = await ClaudeCodeProvider().list_available_models(allow_probe=False)

        assert models == list(CURATED_MODELS)
        assert mock_run.await_count == 0

    async def test_not_authenticated_returns_curated_and_spends_no_probe_calls(self) -> None:
        from sova.llm.models import CURATED_MODELS
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        mock_run = AsyncMock(side_effect=self._run_side_effect(logged_in=False))
        with patch("sova.llm.providers.claude_code.run", mock_run):
            models = await ClaudeCodeProvider().list_available_models()

        assert models == list(CURATED_MODELS)
        assert mock_run.await_count == 1  # only the auth probe; never spent on model probes

    async def test_authenticated_returns_only_working_models(self) -> None:
        from sova.llm.models import CURATED_MODELS
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        mock_run = AsyncMock(side_effect=self._run_side_effect(available_ids={"claude-opus-5", "claude-sonnet-5"}))
        with patch("sova.llm.providers.claude_code.run", mock_run):
            models = await ClaudeCodeProvider().list_available_models()

        assert {m.id for m in models} == {"claude-opus-5", "claude-sonnet-5"}
        assert all(m.source == "probed" for m in models)
        assert mock_run.await_count == 1 + len(CURATED_MODELS)

    async def test_probe_results_are_cached_across_calls(self) -> None:
        """get_auth_details() re-probes auth on every call (it is a cheap local
        read, not a billed network probe), but the identity it resolves to is
        unchanged, so the per-model probe pass itself must not repeat."""
        from sova.llm.models import CURATED_MODELS
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        mock_run = AsyncMock(side_effect=self._run_side_effect())
        with patch("sova.llm.providers.claude_code.run", mock_run):
            provider = ClaudeCodeProvider()
            first = await provider.list_available_models()
            calls_after_first = mock_run.await_count
            second = await provider.list_available_models()

        assert first == second
        assert calls_after_first == 1 + len(CURATED_MODELS)
        assert mock_run.await_count == calls_after_first + 1  # +1 auth re-probe; no new model probes

    async def test_concurrent_calls_single_flight(self) -> None:
        """Two concurrent callers on the same identity trigger one model-probe
        pass: each call independently re-probes auth to resolve its identity
        (cheap, local, not single-flighted), but once both resolve to the same
        identity, the per-model probing itself happens exactly once."""
        from sova.llm.models import CURATED_MODELS
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        call_count = 0
        model_probe_count = 0
        inner = self._run_side_effect()

        async def _counting_run(*args, **kwargs):
            nonlocal call_count, model_probe_count
            call_count += 1
            if "--model" in args:
                model_probe_count += 1
            await asyncio.sleep(0.01)
            return await inner(*args, **kwargs)

        with patch("sova.llm.providers.claude_code.run", _counting_run):
            provider = ClaudeCodeProvider()
            first, second = await asyncio.gather(
                provider.list_available_models(),
                provider.list_available_models(),
            )

        assert first == second
        assert model_probe_count == len(CURATED_MODELS)  # single-flighted, not doubled
        assert call_count == 2 + len(CURATED_MODELS)  # 2 independent auth probes + 1 model-probe pass

    async def test_probe_subprocess_oserror_marks_only_that_model_unavailable(self) -> None:
        from sova.llm.models import CURATED_MODELS
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        async def _flaky_run(*args, **kwargs):
            if "auth" in args:
                return ShellResult(
                    returncode=0,
                    stdout='{"loggedIn": true, "email": "dev@example.com"}',
                    stderr="",
                )
            if "--model" in args:
                idx = args.index("--model")
                if args[idx + 1] == "claude-opus-5":
                    raise OSError("claude binary vanished")
                return ShellResult(returncode=0, stdout="{}", stderr="")
            return ShellResult(returncode=0, stdout="{}", stderr="")

        with patch("sova.llm.providers.claude_code.run", _flaky_run):
            models = await ClaudeCodeProvider().list_available_models()

        assert "claude-opus-5" not in {m.id for m in models}
        assert len(models) == len(CURATED_MODELS) - 1

    async def test_different_accounts_get_independent_probed_sets(self) -> None:
        """Identity is scoped by account email, so two ClaudeCodeProvider
        instances logged in as different accounts never share a probed set."""
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        async def _run_for(email: str, working_id: str):
            async def _run(*args, **kwargs):
                if "auth" in args:
                    return ShellResult(
                        returncode=0,
                        stdout=f'{{"loggedIn": true, "email": "{email}"}}',
                        stderr="",
                    )
                idx = args.index("--model")
                ok = args[idx + 1] == working_id
                # A real unavailable-model failure carries stderr (or an
                # is_error payload); a nonzero exit with clean JSON and empty
                # stderr is what invoke() itself treats as a success, so
                # simulating failure that way would not be a failure at all.
                return ShellResult(returncode=0 if ok else 1, stdout="{}", stderr="" if ok else "model not found")

            return _run

        with patch("sova.llm.providers.claude_code.run", await _run_for("a@example.com", "claude-opus-5")):
            models_a = await ClaudeCodeProvider().list_available_models()

        with patch("sova.llm.providers.claude_code.run", await _run_for("b@example.com", "claude-sonnet-5")):
            models_b = await ClaudeCodeProvider().list_available_models()

        assert {m.id for m in models_a} == {"claude-opus-5"}
        assert {m.id for m in models_b} == {"claude-sonnet-5"}

    async def test_every_probe_failing_returns_curated_and_is_not_cached(self) -> None:
        """A whole-list wipeout is a CLI/account outage, not "this account can
        reach no models", so curated is served and never cached as empty."""
        from sova.llm.client import get_availability_cache
        from sova.llm.models import CURATED_MODELS
        from sova.llm.providers.claude_code import ClaudeCodeProvider, _claude_code_enumeration_identity

        mock_run = AsyncMock(side_effect=self._run_side_effect(available_ids=set()))
        with patch("sova.llm.providers.claude_code.run", mock_run):
            models = await ClaudeCodeProvider().list_available_models()

        identity = _claude_code_enumeration_identity({"email": "dev@example.com"})
        assert models == list(CURATED_MODELS)
        assert get_availability_cache().get_enumeration(identity) is None

    async def test_probe_accepts_nonzero_exit_with_clean_json(self) -> None:
        """invoke() treats a nonzero exit that still produced a clean JSON
        result as success (fallback warnings do this), so a probe that rejected
        it would drop a model invoke() would have used."""
        from sova.llm.models import CURATED_MODELS
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        async def _run(*args, **kwargs):
            if "auth" in args:
                return ShellResult(returncode=0, stdout='{"loggedIn": true, "email": "dev@example.com"}', stderr="")
            return ShellResult(returncode=1, stdout='{"result": "hi", "total_cost_usd": 0}', stderr="")

        with patch("sova.llm.providers.claude_code.run", _run):
            models = await ClaudeCodeProvider().list_available_models()

        assert {m.id for m in models} == {m.id for m in CURATED_MODELS}

    async def test_probe_rejects_nonzero_exit_reporting_is_error(self) -> None:
        from sova.llm.models import CURATED_MODELS
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        async def _run(*args, **kwargs):
            if "auth" in args:
                return ShellResult(returncode=0, stdout='{"loggedIn": true, "email": "dev@example.com"}', stderr="")
            return ShellResult(returncode=1, stdout='{"is_error": true}', stderr="")

        with patch("sova.llm.providers.claude_code.run", _run):
            models = await ClaudeCodeProvider().list_available_models()

        assert models == list(CURATED_MODELS)  # no model confirmed, so the fallback is served

    async def test_account_switch_after_check_available_uses_fresh_identity(self) -> None:
        """list_available_models() must not serve get_auth_details()'s cached
        _last_auth_probe: that cache reflects whatever account check_available()
        last saw on this instance, and can go stale the moment the CLI account
        is switched afterward. Enumeration has to key its cache (and store any
        probe results) under the account that is active right now, not a
        possibly-stale prior identity, or it can return (or populate) the
        previous account's catalog under the current one's name."""
        from sova.llm.providers.claude_code import ClaudeCodeProvider, _claude_code_enumeration_identity
        from sova.utils.shell import ShellResult

        current_email = "a@example.com"

        async def _run(*args, **kwargs):
            if "--version" in args:
                return ShellResult(returncode=0, stdout="2.1.259\n", stderr="")
            if "auth" in args:
                return ShellResult(returncode=0, stdout=f'{{"loggedIn": true, "email": "{current_email}"}}', stderr="")
            if "--model" in args:
                idx = args.index("--model")
                model_id = args[idx + 1]
                ok = model_id == "claude-opus-5"
                return ShellResult(returncode=0 if ok else 1, stdout="{}", stderr="" if ok else "boom")
            return ShellResult(returncode=0, stdout="{}", stderr="")

        with (
            patch("sova.llm.providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"),
            patch("sova.llm.providers.claude_code.run", _run),
        ):
            provider = ClaudeCodeProvider()
            await provider.check_available()  # caches _last_auth_probe for a@example.com

            current_email = "b@example.com"  # the CLI account changes underneath the same instance
            models = await provider.list_available_models()

        identity_a = _claude_code_enumeration_identity({"email": "a@example.com"})
        identity_b = _claude_code_enumeration_identity({"email": "b@example.com"})
        assert {m.id for m in models} == {"claude-opus-5"}

        from sova.llm.client import get_availability_cache

        assert get_availability_cache().get_enumeration(identity_b) is not None
        assert get_availability_cache().get_enumeration(identity_a) is None

    async def test_probe_model_launch_isolates_workspace_configuration(self) -> None:
        """_probe_model() must run in a launch configuration that cannot load
        project hooks, MCP servers, or CLAUDE.md, and cannot execute tools:
        bypassPermissions mode would otherwise let a contributor-controlled
        repository run arbitrary commands merely by being probed for model
        availability."""
        from sova.llm.providers.claude_code import ClaudeCodeProvider
        from sova.utils.shell import ShellResult

        captured_args: list[tuple] = []

        async def _run(*args, **kwargs):
            captured_args.append(args)
            if "auth" in args:
                return ShellResult(returncode=0, stdout='{"loggedIn": true, "email": "dev@example.com"}', stderr="")
            return ShellResult(returncode=0, stdout="{}", stderr="")

        with patch("sova.llm.providers.claude_code.run", _run):
            await ClaudeCodeProvider().list_available_models()

        model_probe_calls = [args for args in captured_args if "--model" in args]
        assert model_probe_calls, "expected at least one per-model probe call"
        for args in model_probe_calls:
            assert "--safe-mode" in args
            tools_idx = args.index("--tools")
            assert args[tools_idx + 1] == ""
