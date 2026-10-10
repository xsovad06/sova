"""Tests for sova.core.agent_dispatch: the LLMProvider/AgentRuntime dispatch boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.core.agent_dispatch import AgentRuntimeUnavailableError, dispatch_command, dispatch_prompt
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.ipc.runtime import AgentRuntime
from sova.llm.errors import LLMTimeoutError, RateLimitError
from sova.llm.models import LLMResult, StreamEvent


class _TextOnlyStep(BaseStep):
    name = "text_only"
    requires_tools = False

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        raise NotImplementedError

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)


class _ToolStep(BaseStep):
    name = "tool_step"
    requires_tools = True

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        raise NotImplementedError

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)


class _StubProcess:
    """Minimal AgentProcess double giving dispatch_command full control over the run."""

    def __init__(
        self,
        *,
        stdout: list[str] | None = None,
        stderr: list[str] | None = None,
        exit_code: int = 0,
        hang: bool = False,
    ) -> None:
        self._stdout = stdout or []
        self._stderr = stderr or []
        self._exit_code = exit_code
        self._hang = hang
        self._stopped = asyncio.Event()
        self.stop_calls: list[dict] = []
        self.returncode: int | None = None

    async def stdout_lines(self) -> AsyncIterator[str]:
        for line in self._stdout:
            yield line

    async def stderr_lines(self) -> AsyncIterator[str]:
        for line in self._stderr:
            yield line

    async def wait(self) -> int:
        if self._hang:
            await self._stopped.wait()
        self.returncode = self._exit_code
        return self._exit_code

    async def stop(self, timeout: float = 10.0, *, cause: str = "", requester: str = "") -> None:
        self.stop_calls.append({"cause": cause, "requester": requester})
        self._hang = False
        self._stopped.set()
        self.returncode = self._exit_code


class _StubRuntime(AgentRuntime):
    """AgentRuntime double that returns a terminal 'result' StreamEvent for a fixed marker line."""

    def __init__(self, process: _StubProcess, *, available: bool = True, detail: str = "ok") -> None:
        self._process = process
        self._available = available
        self._detail = detail
        self.spawn_calls: list[dict] = []

    @property
    def name(self) -> str:
        return "stub"

    async def spawn(self, prompt, cwd, **kwargs):  # noqa: ANN001, ANN003 (test double)
        self.spawn_calls.append({"prompt": prompt, "cwd": cwd, **kwargs})
        return self._process

    def parse_output(self, line: str) -> StreamEvent | None:
        if line == "__RESULT__":
            return StreamEvent(type="result", text="done", result=LLMResult(text="done", model="stub-model"))
        if not line.strip():
            return None
        return StreamEvent(type="content", text=line)

    async def check_available(self) -> tuple[bool, str]:
        return self._available, self._detail


def _write_command(tmp_path: Path, name: str, body: str) -> None:
    cmd_dir = tmp_path / ".claude" / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    (cmd_dir / f"{name}.md").write_text(f"---\nname: {name}\ndescription: test\n---\n{body}\n", encoding="utf-8")


def _stub_preflight(model: str | None = None, timeout: float | None = None):
    """Patch the shared pre-flight so a test asserts on dispatch, not on config.

    ``prepare_invocation()`` resolves the model through ``llm.routing`` and
    ``llm.model_aliases`` against whatever config the cwd resolves to, which
    is the repo's own when a tmp_path has none. Stubbing it keeps these tests
    about the dispatch boundary; that the pre-flight runs at all is asserted
    separately in ``test_preflight_applies_to_tool_path``.
    """
    return patch(
        "sova.core.agent_dispatch.client.prepare_invocation",
        new_callable=AsyncMock,
        return_value=(None, model, timeout),
    )


class TestDispatchTextOnly:
    async def test_delegates_to_llm_client_invoke_command(self) -> None:
        step = _TextOnlyStep()
        with patch("sova.core.agent_dispatch.client.invoke_command", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(text="hi", model="m")
            result = await dispatch_command(
                step,
                "/triage",
                args="42",
                model="m",
                task_type="triage",
                cwd="/tmp/project",
                timeout=30,
            )
        assert result.text == "hi"
        mock_invoke.assert_awaited_once_with(
            "/triage",
            "42",
            model="m",
            fallback_model=None,
            task_type="triage",
            cwd="/tmp/project",
            max_budget_usd=None,
            timeout=30,
        )

    async def test_never_touches_agent_runtime(self) -> None:
        step = _TextOnlyStep()
        with (
            patch("sova.core.agent_dispatch.client.invoke_command", new_callable=AsyncMock) as mock_invoke,
            patch("sova.core.agent_dispatch.get_runtime") as mock_get_runtime,
        ):
            mock_invoke.return_value = LLMResult(text="hi", model="m")
            await dispatch_command(step, "/triage", args="42")
        mock_get_runtime.assert_not_called()


class TestDispatchToolStep:
    async def test_fails_fast_when_runtime_unavailable_no_fallback(self) -> None:
        step = _ToolStep()
        unavailable_runtime = _StubRuntime(_StubProcess(), available=False, detail="codex CLI not found")
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=unavailable_runtime),
            patch("sova.core.agent_dispatch.client.invoke_command", new_callable=AsyncMock) as mock_invoke,
        ):
            with pytest.raises(AgentRuntimeUnavailableError, match="codex CLI not found"):
                await dispatch_command(step, "/develop", args="42", cwd="/tmp/project")
        mock_invoke.assert_not_called()

    async def test_missing_command_file_raises(self, tmp_path: Path) -> None:
        step = _ToolStep()
        runtime = _StubRuntime(_StubProcess())
        with patch("sova.core.agent_dispatch.get_runtime", return_value=runtime):
            # Same RuntimeError the LLMProvider path raises for a missing command;
            # not an AgentRuntimeUnavailableError, which means the runtime itself.
            with pytest.raises(RuntimeError, match="not found"):
                await dispatch_command(step, "/develop", args="42", cwd=tmp_path)

    async def test_happy_path_spawns_runtime_with_resolved_command_body(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing for issue")
        step = _ToolStep()
        process = _StubProcess(stdout=["__RESULT__"])
        runtime = _StubRuntime(process)
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight("routed-model", 30.0),
        ):
            result = await dispatch_command(
                step,
                "/develop",
                args="42",
                model="opus",
                fallback_model="sonnet",
                max_budget_usd=Decimal("5"),
                cwd=tmp_path,
                timeout=30,
            )

        assert result.text == "done"
        assert result.model == "stub-model"
        spawn_call = runtime.spawn_calls[0]
        assert "Do the thing for issue" in spawn_call["prompt"]
        assert "42" in spawn_call["prompt"]
        assert spawn_call["model"] == "routed-model"
        assert spawn_call["fallback_model"] == "sonnet"
        assert spawn_call["max_budget_usd"] == Decimal("5")

    async def test_preflight_applies_to_tool_path(self, tmp_path: Path) -> None:
        """The AgentRuntime path bypasses the invoke* entry points, so it has to
        run their shared pre-flight itself: the runaway call guard, llm.routing
        task-type routing, alias resolution and the config timeout default."""
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        runtime = _StubRuntime(_StubProcess(stdout=["__RESULT__"]))
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight("routed-model", 42.0) as mock_preflight,
        ):
            await dispatch_command(step, "/develop", args="42", model="opus", task_type="develop", cwd=tmp_path)

        mock_preflight.assert_awaited_once_with(model="opus", task_type="develop", timeout=None, cwd=tmp_path)
        assert runtime.spawn_calls[0]["model"] == "routed-model"

    async def test_arguments_placeholder_is_substituted_in_place(self, tmp_path: Path) -> None:
        """The CLI substitutes $ARGUMENTS when it expands a /command; the runtime
        path spawns the body literally, so it must substitute too. Appending
        instead would leave the agent reading a bare placeholder as its task."""
        _write_command(tmp_path, "develop", "**Task**: $ARGUMENTS\n\nNow follow the steps.")
        step = _ToolStep()
        runtime = _StubRuntime(_StubProcess(stdout=["__RESULT__"]))
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            await dispatch_command(step, "/develop", args="issue 42", cwd=tmp_path)

        prompt = runtime.spawn_calls[0]["prompt"]
        assert "$ARGUMENTS" not in prompt
        assert "**Task**: issue 42" in prompt
        assert prompt.endswith("Now follow the steps.")

    async def test_arguments_appended_when_body_has_no_placeholder(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        runtime = _StubRuntime(_StubProcess(stdout=["__RESULT__"]))
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            await dispatch_command(step, "/develop", args="42", cwd=tmp_path)

        assert runtime.spawn_calls[0]["prompt"] == "Do the thing\n\n42"

    async def test_timeout_raises_llm_timeout_error_and_stops_process(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        process = _StubProcess(hang=True)
        runtime = _StubRuntime(process)
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(timeout=0.05),
        ):
            with pytest.raises(LLMTimeoutError):
                await dispatch_command(step, "/develop", args="42", cwd=tmp_path, timeout=0.05)
        assert process.stop_calls
        assert process.stop_calls[0]["cause"] == "agent_dispatch_timeout"

    async def test_cancellation_stops_process_and_propagates(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        process = _StubProcess(hang=True)
        runtime = _StubRuntime(process)
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            task = asyncio.create_task(dispatch_command(step, "/develop", args="42", cwd=tmp_path))
            # Cancel only once the process is actually spawned: cancelling during
            # the pre-flight would prove nothing about orphan cleanup.
            while not runtime.spawn_calls:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert process.stop_calls
        assert process.stop_calls[0]["cause"] == "agent_dispatch_cancelled"

    async def test_failure_without_result_event_is_classified(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        process = _StubProcess(stderr=["rate_limit exceeded, try again later"], exit_code=1)
        runtime = _StubRuntime(process)
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            with pytest.raises(RateLimitError):
                await dispatch_command(step, "/develop", args="42", cwd=tmp_path)

    async def test_frontmatter_is_stripped_from_prompt(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Body text only")
        step = _ToolStep()
        process = _StubProcess(stdout=["__RESULT__"])
        runtime = _StubRuntime(process)
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            await dispatch_command(step, "/develop", cwd=tmp_path)
        prompt = runtime.spawn_calls[0]["prompt"]
        assert "name: develop" not in prompt
        assert "Body text only" in prompt


class TestDispatchPrompt:
    """dispatch_prompt() is the raw-prompt sibling of dispatch_command().

    DevelopStep's inner fix loop asks the agent to edit source files until the
    project's check command passes, so it is as tool-dependent as the
    /develop dispatch it follows.
    """

    async def test_text_only_step_delegates_to_client_invoke(self) -> None:
        step = _TextOnlyStep()
        with (
            patch("sova.core.agent_dispatch.client.invoke", new_callable=AsyncMock) as mock_invoke,
            patch("sova.core.agent_dispatch.get_runtime") as mock_get_runtime,
        ):
            mock_invoke.return_value = LLMResult(text="hi", model="m")
            result = await dispatch_prompt(step, "fix it", model="m", task_type="develop_fix", timeout=60)

        assert result.text == "hi"
        mock_get_runtime.assert_not_called()
        mock_invoke.assert_awaited_once_with(
            "fix it",
            model="m",
            fallback_model=None,
            task_type="develop_fix",
            cwd=None,
            max_budget_usd=None,
            timeout=60,
        )

    async def test_tool_step_spawns_prompt_verbatim_on_runtime(self, tmp_path: Path) -> None:
        step = _ToolStep()
        runtime = _StubRuntime(_StubProcess(stdout=["__RESULT__"]))
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight("routed-model", 60.0),
        ):
            result = await dispatch_prompt(step, "fix the failing checks", cwd=tmp_path)

        assert result.text == "done"
        assert runtime.spawn_calls[0]["prompt"] == "fix the failing checks"
        assert runtime.spawn_calls[0]["model"] == "routed-model"

    async def test_tool_step_fails_fast_when_runtime_unavailable(self) -> None:
        step = _ToolStep()
        runtime = _StubRuntime(_StubProcess(), available=False, detail="claude CLI not found")
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            patch("sova.core.agent_dispatch.client.invoke", new_callable=AsyncMock) as mock_invoke,
        ):
            with pytest.raises(AgentRuntimeUnavailableError, match="claude CLI not found"):
                await dispatch_prompt(step, "fix it", cwd="/tmp/project")
        mock_invoke.assert_not_called()

    async def test_timeout_raises_llm_timeout_error(self, tmp_path: Path) -> None:
        """format_fix_llm_failure() tags a timeout distinctly from a generic
        failure, so the runtime path must raise LLMTimeoutError like invoke()."""
        step = _ToolStep()
        process = _StubProcess(hang=True)
        runtime = _StubRuntime(process)
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(timeout=0.05),
        ):
            with pytest.raises(LLMTimeoutError):
                await dispatch_prompt(step, "fix it", cwd=tmp_path, timeout=0.05)
        assert process.stop_calls[0]["cause"] == "agent_dispatch_timeout"
