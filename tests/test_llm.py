"""Tests for SOVA LLM interaction layer."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

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


# ---------------------------------------------------------------------------
# Client: resolve_model()
# ---------------------------------------------------------------------------


class TestResolveModel:
    def test_resolve_from_roles_config(self) -> None:
        from sova.config.models import RolesConfig
        from sova.llm.client import resolve_model

        roles = RolesConfig(researcher_model="opus", triage_model="haiku")
        assert resolve_model("researcher", roles) == "opus"
        assert resolve_model("triage", roles) == "haiku"

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
    def test_create_provider_default(self) -> None:
        from sova.llm.provider import create_provider
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        provider = create_provider("claude-code")
        assert isinstance(provider, ClaudeCodeProvider)

    def test_create_provider_unknown(self) -> None:
        from sova.llm.provider import create_provider

        with pytest.raises(ValueError, match="Unknown LLM provider"):
            create_provider("nonexistent")

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
# Config: LLMConfig
# ---------------------------------------------------------------------------


class TestLLMConfig:
    def test_default_provider(self) -> None:
        from sova.config.models import LLMConfig

        cfg = LLMConfig()
        assert cfg.provider == "claude-code"

    def test_project_config_has_llm(self) -> None:
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        assert cfg.llm.provider == "claude-code"

    def test_load_from_toml(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config

        toml_file = tmp_path / "sova.toml"
        toml_file.write_text('[llm]\nprovider = "claude-code"\n')
        cfg = load_config(tmp_path)
        assert cfg.llm.provider == "claude-code"


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
        from sova.cli.app import _init_llm_provider

        with patch("sova.cli.app.load_config") as mock_load:
            from sova.config.models import ProjectConfig

            mock_load.return_value = ProjectConfig(llm={"provider": "nonexistent"})
            with pytest.raises(ValueError, match="Unknown LLM provider"):
                _init_llm_provider()


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
