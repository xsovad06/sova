"""Tests for sova.core.agent_dispatch: the LLMProvider/AgentRuntime dispatch boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
from sova.core.agent_dispatch import AgentRuntimeUnavailableError, dispatch_command, dispatch_prompt
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.ipc.runtime import AgentRuntime
from sova.llm.errors import BillingError, LLMTimeoutError, RateLimitError
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


class _CappingRuntime(_StubRuntime):
    """Runtime that enforces ``max_budget_usd`` itself, like ClaudeCodeRuntime."""

    @property
    def enforces_budget_cap(self) -> bool:
        return True


class _FlakyModelRuntime(_StubRuntime):
    """Runtime that fails a named model with a fallback-eligible error."""

    def __init__(self, process: _StubProcess, *, failing_model: str) -> None:
        super().__init__(process)
        self._failing_model = failing_model

    async def spawn(self, prompt, cwd, **kwargs):  # noqa: ANN001, ANN003 (test double)
        await super().spawn(prompt, cwd, **kwargs)
        if kwargs.get("model") == self._failing_model:
            raise RateLimitError("rate limit exceeded, try again later")
        return self._process


def _config_with_fallbacks(*fallbacks: str) -> ProjectConfig:
    # resolve_tier_aliases=False keeps these opaque example model names out of
    # the tier-alias resolution these tests are not about (see #1033).
    return ProjectConfig(
        llm=LLMConfig(resolve_tier_aliases=False),
        agent=AgentConfig(model="primary", fallback_models=list(fallbacks)),
    )


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


class _ErrorResultRuntime(_StubRuntime):
    """Runtime whose terminal result event reports a failed turn.

    A runtime signals a failed turn in-band (``stop_reason="error"``, which is
    what ``CodexStreamParser`` and ``ClaudeCodeRuntime.parse_output()`` both
    set from the CLI's own ``is_error``), not only via the exit code.
    """

    def parse_output(self, line: str) -> StreamEvent | None:
        if line == "__RESULT__":
            failed = LLMResult(text="rate_limit exceeded", model="stub-model", stop_reason="error")
            return StreamEvent(type="result", text=failed.text, result=failed)
        return super().parse_output(line)


class _ExplodingRuntime(_StubRuntime):
    """Runtime whose parser raises mid-stream, the way a malformed line would."""

    def parse_output(self, line: str) -> StreamEvent | None:
        raise ValueError("malformed stream line")


class TestRuntimeFailureHandling:
    """A failed turn must not be returned as a success, and must leave no orphan."""

    async def test_error_result_event_is_raised_not_returned(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        runtime = _ErrorResultRuntime(_StubProcess(stdout=["__RESULT__"]))
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            # Classified from the reported detail, like the LLMProvider path.
            with pytest.raises(RateLimitError, match="rate_limit"):
                await dispatch_command(step, "/develop", args="42", cwd=tmp_path)

    async def test_parser_error_stops_the_spawned_process(self, tmp_path: Path) -> None:
        """Only timeout and cancellation used to kill the agent, so any other
        failure left it running and still editing the worktree."""
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        process = _StubProcess(stdout=["anything"])
        runtime = _ExplodingRuntime(process)
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            with pytest.raises(ValueError, match="malformed stream line"):
                await dispatch_command(step, "/develop", args="42", cwd=tmp_path)

        assert process.stop_calls
        assert process.stop_calls[0]["cause"] == "agent_dispatch_error"

    async def test_stop_failure_does_not_mask_the_real_error(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        process = _StubProcess(stdout=["anything"])

        async def _failing_stop(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003 (test double)
            raise OSError("no such process")

        process.stop = _failing_stop  # type: ignore[method-assign]
        runtime = _ExplodingRuntime(process)
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            with pytest.raises(ValueError, match="malformed stream line"):
                await dispatch_command(step, "/develop", args="42", cwd=tmp_path)


class TestFailureDetailPrefersStdoutOverStderr:
    """Matches the documented 'Failure capture must prefer stdout's structured
    error over stderr' decision: the CLI writes its real error to stdout,
    while stderr often carries only an incidental warning."""

    async def test_no_result_event_prefers_structured_stdout_over_stderr_warning(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        stdout_payload = '{"is_error": true, "result": "Auth failed: invalid API key"}'
        process = _StubProcess(stdout=[stdout_payload], stderr=["Warning: model deprecated"], exit_code=1)
        runtime = _StubRuntime(process)
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            with pytest.raises(Exception, match="Auth failed: invalid API key") as excinfo:
                await dispatch_command(step, "/develop", args="42", cwd=tmp_path)
        assert "Warning: model deprecated" not in str(excinfo.value)

    async def test_no_result_event_falls_back_to_stderr_without_leading_warnings(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        process = _StubProcess(stdout=[], stderr=["Warning: deprecated flag", "real error: bad config"], exit_code=1)
        runtime = _StubRuntime(process)
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            with pytest.raises(Exception, match="real error: bad config") as excinfo:
                await dispatch_command(step, "/develop", args="42", cwd=tmp_path)
        assert "Warning: deprecated flag" not in str(excinfo.value)


class TestRuntimeBudgetCap:
    """Every step passes ``max_budget - ctx.cost_usd``, so a non-positive value
    means the run is already over budget: a runtime that cannot cap itself must
    not be spawned at all."""

    async def test_exhausted_budget_blocks_a_runtime_that_cannot_cap(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        runtime = _StubRuntime(_StubProcess(stdout=["__RESULT__"]))
        assert runtime.enforces_budget_cap is False
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            with pytest.raises(BillingError):
                await dispatch_command(step, "/develop", args="42", cwd=tmp_path, max_budget_usd=Decimal("0"))
        assert not runtime.spawn_calls

    async def test_exhausted_budget_allowed_when_runtime_enforces_it(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        runtime = _CappingRuntime(_StubProcess(stdout=["__RESULT__"]))
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            await dispatch_command(step, "/develop", args="42", cwd=tmp_path, max_budget_usd=Decimal("0"))
        assert runtime.spawn_calls

    async def test_positive_budget_is_passed_through_unchanged(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        runtime = _StubRuntime(_StubProcess(stdout=["__RESULT__"]))
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            await dispatch_command(step, "/develop", args="42", cwd=tmp_path, max_budget_usd=Decimal("3"))
        assert runtime.spawn_calls[0]["max_budget_usd"] == Decimal("3")

    async def test_prompt_path_checks_the_budget_too(self, tmp_path: Path) -> None:
        step = _ToolStep()
        runtime = _StubRuntime(_StubProcess(stdout=["__RESULT__"]))
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            with pytest.raises(BillingError):
                await dispatch_prompt(step, "fix it", cwd=tmp_path, max_budget_usd=Decimal("-1"))
        assert not runtime.spawn_calls


class TestRuntimeModelFallback:
    """``agent.fallback_models`` has to apply to the tool path as well: without
    the shared chain walk, a rate-limited primary failed the whole step."""

    async def test_chain_advances_to_the_next_candidate(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        runtime = _FlakyModelRuntime(_StubProcess(stdout=["__RESULT__"]), failing_model="primary")
        cfg = _config_with_fallbacks("backup")
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            patch(
                "sova.core.agent_dispatch.client.prepare_invocation",
                new_callable=AsyncMock,
                return_value=(cfg, "primary", 900.0),
            ),
        ):
            result = await dispatch_command(step, "/develop", args="42", cwd=tmp_path)

        assert result.text == "done"
        assert [call["model"] for call in runtime.spawn_calls] == ["primary", "backup"]
        # Each hop is handed the next candidate as its own runtime-level fallback.
        assert runtime.spawn_calls[0]["fallback_model"] == "backup"

    async def test_single_candidate_chain_keeps_the_caller_fallback(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        runtime = _StubRuntime(_StubProcess(stdout=["__RESULT__"]))
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            patch(
                "sova.core.agent_dispatch.client.prepare_invocation",
                new_callable=AsyncMock,
                return_value=(_config_with_fallbacks(), "primary", 900.0),
            ),
        ):
            await dispatch_command(step, "/develop", args="42", fallback_model="caller-hop", cwd=tmp_path)

        assert runtime.spawn_calls[0]["fallback_model"] == "caller-hop"

    async def test_chain_walk_never_constructs_an_llm_provider(self, tmp_path: Path) -> None:
        """The executor here is runtime.spawn(), not the configured LLMProvider.

        Reaching for one just to de-duplicate/availability-filter the chain
        would pay for a provider construction the runtime path has no other
        use for, and a provider that fails to construct (e.g. a misconfigured
        optional dependency) would then fail a tool-using step whose runtime
        is perfectly healthy.
        """
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        runtime = _FlakyModelRuntime(_StubProcess(stdout=["__RESULT__"]), failing_model="primary")
        cfg = _config_with_fallbacks("backup")
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            patch(
                "sova.core.agent_dispatch.client.prepare_invocation",
                new_callable=AsyncMock,
                return_value=(cfg, "primary", 900.0),
            ),
            patch("sova.llm.client.get_provider", side_effect=AssertionError("must not construct a provider")),
        ):
            result = await dispatch_command(step, "/develop", args="42", cwd=tmp_path)

        assert result.text == "done"
        assert [call["model"] for call in runtime.spawn_calls] == ["primary", "backup"]


class TestAssemblePromptArgumentsPlaceholder:
    """``$ARGUMENTS`` substitution must hit only the real slot, not a
    backtick-quoted prose reference back to it (develop.md has both)."""

    def test_real_develop_command_substitutes_args_exactly_once(self) -> None:
        from sova.core.agent_dispatch import _assemble_prompt

        repo_root = Path(__file__).resolve().parent.parent
        body = (repo_root / ".claude" / "commands" / "develop.md").read_text(encoding="utf-8")
        # Strip YAML frontmatter the way _resolve_command_body() does.
        if body.startswith("---"):
            _, _, body = body.partition("---\n")
            _, _, body = body.partition("---\n")
        args = "42\n\n## Spec Context\nSome spec body text."

        prompt = _assemble_prompt(body, args)

        assert prompt.count(args) == 1
        assert "`$ARGUMENTS`" in prompt  # the backtick-quoted prose references survive untouched


class TestRequiresToolsOptIn:
    """Locks the per-step opt-in the whole dispatch boundary depends on.

    Each migrated step's own test patches ``dispatch_command``/
    ``dispatch_prompt`` to assert the call happens, but none of those asserted
    ``requires_tools`` itself: a future change that quietly set it back to
    ``False`` (or dropped it) would keep every one of those tests green while
    silently returning the step to the text-only LLMProvider, which is the
    exact regression this dispatch boundary exists to prevent.
    """

    @pytest.mark.parametrize(
        "step_path",
        [
            "sova.core.steps.develop.DevelopStep",
            "sova.core.steps.validate.ValidateStep",
            "sova.core.steps.monitor_ci.MonitorCIStep",
            "sova.core.steps.self_review.SelfReviewStep",
            "sova.core.steps.simplify.SimplifyStep",
            "sova.core.steps.address_review.AddressReviewStep",
            "sova.core.steps.address_external_findings.AddressExternalFindingsStep",
            "sova.core.steps.rearrange_commits.RearrangeCommitsStep",
        ],
    )
    def test_tool_using_step_requires_tools(self, step_path: str) -> None:
        import importlib

        module_path, _, class_name = step_path.rpartition(".")
        step_cls = getattr(importlib.import_module(module_path), class_name)
        assert step_cls.requires_tools is True

    def test_research_step_holdout_stays_text_only(self) -> None:
        """ResearchStep deliberately stays on invoke_command()/LLMProvider.

        /research never edits the worktree, only appends a '## Research'
        section to the issue/ticket body via gh/jira CLI calls, and its own
        validate_output() already gates on that section existing.
        """
        from sova.core.steps.research import ResearchStep

        assert ResearchStep.requires_tools is False


class TestSanitizeModelForClaudeCli:
    """llm.provider and agent.runtime are independently configurable, so a
    litellm-dialect model id (vertex_ai/, ollama/, ...) can reach the tool
    path. A bare ``claude --model`` CLI cannot parse that dialect."""

    def test_none_model_passes_through(self) -> None:
        from sova.core.agent_dispatch import _sanitize_model_for_claude_cli

        assert _sanitize_model_for_claude_cli(None) is None

    def test_plain_model_passes_through_unchanged(self) -> None:
        from sova.core.agent_dispatch import _sanitize_model_for_claude_cli

        assert _sanitize_model_for_claude_cli("claude-sonnet-4-5") == "claude-sonnet-4-5"

    def test_vertex_prefix_is_stripped(self) -> None:
        """vertex_ai/ is the one litellm prefix whose bare id is still a real Claude model id."""
        from sova.core.agent_dispatch import _sanitize_model_for_claude_cli

        assert _sanitize_model_for_claude_cli("vertex_ai/claude-sonnet-4-5@20250929") == "claude-sonnet-4-5@20250929"

    @pytest.mark.parametrize("model", ["ollama/qwen3:32b", "openai/gpt-4", "gemini/gemini-pro", "mistral/large"])
    def test_foreign_vendor_prefix_raises(self, model: str) -> None:
        from sova.core.agent_dispatch import AgentRuntimeUnavailableError, _sanitize_model_for_claude_cli

        with pytest.raises(AgentRuntimeUnavailableError, match="cannot serve"):
            _sanitize_model_for_claude_cli(model)

    def test_sanitize_for_runtime_only_applies_to_claude_code(self) -> None:
        """A non-claude-code runtime (e.g. Codex) accepts a different dialect."""
        from sova.core.agent_dispatch import _sanitize_model_for_runtime

        runtime = _StubRuntime(_StubProcess())
        assert runtime.name != "claude-code"
        assert _sanitize_model_for_runtime(runtime, "ollama/qwen3:32b") == "ollama/qwen3:32b"


class _ClaudeCodeNamedRuntime(_StubRuntime):
    """Runtime double whose ``name`` matches the real ClaudeCodeRuntime's.

    ``_sanitize_model_for_runtime()`` only applies to a runtime actually
    named "claude-code" (the one that spawns a bare ``claude --model`` CLI).
    """

    @property
    def name(self) -> str:
        return "claude-code"


class TestDispatchSanitizesVendorPrefixedModel:
    """The tool path must fail fast on a litellm-dialect model, not hand it
    to the Claude CLI and let the spawned process fail opaquely."""

    async def test_vertex_prefixed_model_is_stripped_before_spawn(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        runtime = _ClaudeCodeNamedRuntime(_StubProcess(stdout=["__RESULT__"]))
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(model="vertex_ai/claude-sonnet-4-5@20250929"),
        ):
            await dispatch_command(step, "/develop", args="42", cwd=tmp_path)

        assert runtime.spawn_calls[0]["model"] == "claude-sonnet-4-5@20250929"

    async def test_foreign_vendor_model_raises_before_spawn(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        runtime = _ClaudeCodeNamedRuntime(_StubProcess(stdout=["__RESULT__"]))
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(model="ollama/qwen3:32b"),
        ):
            with pytest.raises(AgentRuntimeUnavailableError, match="cannot serve"):
                await dispatch_command(step, "/develop", args="42", cwd=tmp_path)
        assert not runtime.spawn_calls


class _ModelIgnoringRuntime(_StubRuntime):
    """Runtime double matching CodexRuntime: ignores spawn(model=...) entirely."""

    @property
    def name(self) -> str:
        return "codex"

    @property
    def honours_model_override(self) -> bool:
        return False


class TestHonoursModelOverrideSkipsChainWalk:
    """A runtime that ignores spawn(model=...) must not re-run the agent once
    per fallback candidate for a model that never actually changes."""

    async def test_fallback_models_configured_but_only_one_spawn_happens(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        runtime = _ModelIgnoringRuntime(_StubProcess(stdout=["__RESULT__"]))
        cfg = _config_with_fallbacks("backup-one", "backup-two")
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            patch(
                "sova.core.agent_dispatch.client.prepare_invocation",
                new_callable=AsyncMock,
                return_value=(cfg, "primary", 900.0),
            ),
        ):
            result = await dispatch_command(step, "/develop", args="42", cwd=tmp_path)

        assert result.text == "done"
        assert len(runtime.spawn_calls) == 1
        assert runtime.spawn_calls[0]["model"] == "primary"

    async def test_default_runtime_still_honours_model_override(self, tmp_path: Path) -> None:
        """Sanity check: _StubRuntime (matching ClaudeCodeRuntime) is unaffected."""
        runtime = _StubRuntime(_StubProcess())
        assert runtime.honours_model_override is True


class _WorktreeMutatingFailingRuntime(_StubRuntime):
    """Runtime whose attempt edits the worktree before raising a fallback-eligible error.

    Models a real coding agent that made partial progress (edited/staged a
    file) before dying mid-run, unlike the stateless LLMProvider path.
    """

    def __init__(self, process: _StubProcess, *, cwd: Path, failing_model: str) -> None:
        super().__init__(process)
        self._cwd = cwd
        self._failing_model = failing_model

    async def spawn(self, prompt, cwd, **kwargs):  # noqa: ANN001, ANN003 (test double)
        await super().spawn(prompt, cwd, **kwargs)
        if kwargs.get("model") == self._failing_model:
            (self._cwd / "edited_by_agent.txt").write_text("partial progress\n", encoding="utf-8")
            raise RateLimitError("rate limit exceeded, try again later")
        return self._process


class TestFallbackChainBlockedByWorktreeMutation:
    """Unlike the LLMProvider path, a failed attempt here may have already
    edited the worktree, so advancing to the next fallback model must not
    retry the identical prompt against a half-modified tree."""

    async def test_chain_does_not_advance_once_the_tree_has_moved(self, tmp_path: Path) -> None:
        from sova.utils.shell import run

        await run("git", "init", cwd=tmp_path)
        await run("git", "config", "user.email", "test@example.com", cwd=tmp_path)
        await run("git", "config", "user.name", "Test", cwd=tmp_path)
        (tmp_path / "README.md").write_text("initial\n", encoding="utf-8")
        await run("git", "add", "README.md", cwd=tmp_path)
        await run("git", "commit", "-m", "initial", cwd=tmp_path)

        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        runtime = _WorktreeMutatingFailingRuntime(
            _StubProcess(stdout=["__RESULT__"]), cwd=tmp_path, failing_model="primary"
        )
        cfg = _config_with_fallbacks("backup")
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            patch(
                "sova.core.agent_dispatch.client.prepare_invocation",
                new_callable=AsyncMock,
                return_value=(cfg, "primary", 900.0),
            ),
        ):
            with pytest.raises(RateLimitError):
                await dispatch_command(step, "/develop", args="42", cwd=tmp_path)

        # Only the first (worktree-mutating) attempt ran; the chain must not
        # have advanced to "backup" against the now-dirty tree.
        assert [call["model"] for call in runtime.spawn_calls] == ["primary"]
        assert (tmp_path / "edited_by_agent.txt").exists()

    async def test_chain_still_advances_when_the_tree_is_untouched(self, tmp_path: Path) -> None:
        """A clean-failure attempt (no edits) must still be allowed to fall back."""
        from sova.utils.shell import run

        await run("git", "init", cwd=tmp_path)
        await run("git", "config", "user.email", "test@example.com", cwd=tmp_path)
        await run("git", "config", "user.name", "Test", cwd=tmp_path)
        (tmp_path / "README.md").write_text("initial\n", encoding="utf-8")
        await run("git", "add", "README.md", cwd=tmp_path)
        await run("git", "commit", "-m", "initial", cwd=tmp_path)

        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        runtime = _FlakyModelRuntime(_StubProcess(stdout=["__RESULT__"]), failing_model="primary")
        cfg = _config_with_fallbacks("backup")
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            patch(
                "sova.core.agent_dispatch.client.prepare_invocation",
                new_callable=AsyncMock,
                return_value=(cfg, "primary", 900.0),
            ),
        ):
            result = await dispatch_command(step, "/develop", args="42", cwd=tmp_path)

        assert result.text == "done"
        assert [call["model"] for call in runtime.spawn_calls] == ["primary", "backup"]


class TestOutputSinkFeedsContentEvents:
    """A long tool-using step must be observable before it finishes, not
    silent until the terminal result event arrives."""

    async def test_content_events_are_written_to_ctx_output_writer(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        process = _StubProcess(stdout=["first line", "second line", "__RESULT__"])
        runtime = _StubRuntime(process)

        written: list[str] = []

        class _FakeOutputWriter:
            def write_line(self, text: str) -> None:
                written.append(text)

            def should_flush(self) -> bool:
                return False

            async def flush(self) -> None:
                raise AssertionError("flush should not be called when should_flush() is False")

        ctx = ExecutionContext(
            project_dir=tmp_path,
            config=ProjectConfig(),
            adapter=AsyncMock(),
            issue_number="1",
            role="developer",
            run_label="test",
        )
        ctx.output_writer = _FakeOutputWriter()  # type: ignore[assignment]

        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            await dispatch_command(step, "/develop", args="42", cwd=tmp_path, ctx=ctx)

        assert written == ["first line", "second line"]

    async def test_no_ctx_means_no_output_writer_call_and_no_crash(self, tmp_path: Path) -> None:
        _write_command(tmp_path, "develop", "Do the thing")
        step = _ToolStep()
        process = _StubProcess(stdout=["first line", "__RESULT__"])
        runtime = _StubRuntime(process)
        with (
            patch("sova.core.agent_dispatch.get_runtime", return_value=runtime),
            _stub_preflight(),
        ):
            result = await dispatch_command(step, "/develop", args="42", cwd=tmp_path)
        assert result.text == "done"


class TestStopQuietlyShieldsCleanupFromCancellation:
    """A second cancellation during cleanup must not skip the kill, or the
    spawned agent (started with start_new_session=True) keeps running and
    editing the worktree after this call has already failed the step."""

    async def test_stop_completes_despite_a_cancellation_during_cleanup(self) -> None:
        from sova.core.agent_dispatch import _stop_quietly

        class _SlowStoppingRuntime(_StubRuntime):
            pass

        stop_started = asyncio.Event()

        class _SlowProc:
            def __init__(self) -> None:
                self.stopped = False

            async def stop(self, timeout: float = 10.0, *, cause: str = "", requester: str = "") -> None:
                stop_started.set()
                await asyncio.sleep(0.2)
                self.stopped = True

        proc = _SlowProc()
        runtime = _SlowStoppingRuntime(_StubProcess())

        task = asyncio.ensure_future(_stop_quietly(proc, runtime, cause="agent_dispatch_cancelled"))
        await stop_started.wait()
        # Cancelling the outer task must not interrupt proc.stop() itself:
        # the shield keeps the stop running to completion in the background.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.3)
        assert proc.stopped is True

    async def test_stop_timeout_is_logged_and_suppressed_not_raised(self) -> None:
        from sova.core.agent_dispatch import _stop_quietly

        class _HangingProc:
            async def stop(self, timeout: float = 10.0, *, cause: str = "", requester: str = "") -> None:
                await asyncio.sleep(10)

        runtime = _StubRuntime(_StubProcess())
        with patch("sova.core.agent_dispatch._STOP_QUIETLY_TIMEOUT_SECONDS", 0.05):
            # Must return normally (the timeout is caught and logged), never
            # propagate as an unhandled TimeoutError out of _stop_quietly().
            await _stop_quietly(_HangingProc(), runtime, cause="agent_dispatch_timeout")
