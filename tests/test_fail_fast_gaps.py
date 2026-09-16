"""Tests for fail-fast gap closures in issue #689."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from sova.config.models import AgentConfig, DevelopConfig
from sova.core.context import ExecutionContext
from sova.core.steps.develop import DevelopStep
from sova.core.workflow import WorkflowEngine
from sova.llm.errors import LLMTimeoutError
from sova.utils.shell import ShellResult


@dataclass
class MockLLMResult:
    """Mock LLMResult for testing."""

    text: str
    cost_usd: Decimal
    session_id: str
    model: str = "opus"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    duration_ms: int = 0
    stop_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@pytest.fixture
def mock_config():
    """Mock configuration with fail-fast settings."""
    cfg = Mock()
    cfg.agent = AgentConfig(
        max_budget=Decimal("10.00"),
        max_issue_budget=Decimal("50.00"),
        step_timeout=1800,
    )
    cfg.develop = DevelopConfig(
        max_fix_cycles=3,
        check_timeout=300,
        guard_test_weakening=True,
        max_fix_time=600,
        fix_timeout=180,
        step_timeout=1200,
    )
    cfg.ci = Mock(max_wait=1500)
    cfg.check_cmd = ""
    cfg.notification = Mock()
    return cfg


@pytest.fixture
def mock_ctx(tmp_path, mock_config):
    """Mock execution context."""
    ctx = Mock(spec=ExecutionContext)
    ctx.project_dir = tmp_path
    ctx.working_dir = tmp_path
    ctx.issue_number = "689"
    ctx.config = mock_config
    ctx.cost_usd = Decimal("0.00")
    ctx.is_budget_exceeded = False
    ctx.budget_remaining_fraction = 1.0
    ctx.resource_remaining_fraction = 1.0
    ctx.resolved_model = "opus"
    ctx.base_branch = "main"
    ctx.display_label = "test-label"
    ctx.role = "developer"
    ctx.task_run_id = None
    ctx.run_label = "test-run"
    ctx.branch_name = "feat/test"
    ctx.resume_run_id = None
    ctx.notification_group = "test"
    ctx.session_id = None
    ctx.output_writer = None
    ctx.step_time_remaining = None
    ctx.step_deadline_is_runaway = False

    def add_cost(amount):
        ctx.cost_usd += amount

    def get_fallback():
        return None

    ctx.add_cost = add_cost
    ctx.get_cli_fallback_model = get_fallback
    return ctx


class TestDevelopStepTimeout:
    """Tests for develop-specific step timeout."""

    @pytest.mark.asyncio
    async def test_develop_uses_custom_timeout(self, mock_ctx, mock_config):
        """Develop step should use develop.step_timeout capped at agent.step_timeout."""
        engine = WorkflowEngine(steps=[], ctx=mock_ctx)

        assert engine._step_timeout("develop") == 1200
        assert engine._step_timeout("other_step") == 1800
        assert engine._step_timeout("monitor_ci") == 1620

    @pytest.mark.asyncio
    async def test_develop_timeout_capped_at_agent_timeout(self, mock_ctx, mock_config):
        """Develop step timeout must never exceed agent.step_timeout."""
        mock_config.develop.step_timeout = 2400
        engine = WorkflowEngine(steps=[], ctx=mock_ctx)

        assert engine._step_timeout("develop") == 1800


class TestInnerCheckLoopTimeControl:
    """Tests for inner check loop time controls."""

    @pytest.mark.asyncio
    async def test_max_fix_time_exceeded(self, mock_ctx, tmp_path):
        """Inner check loop should abort when max_fix_time is exceeded."""
        step = DevelopStep()

        (tmp_path / "Makefile").write_text("check:\n\tfalse\n")
        mock_ctx.config.check_cmd = "make check"

        with (
            patch("sova.core.steps.develop.invoke_command") as mock_invoke_cmd,
            patch("sova.core.steps.develop.invoke") as mock_invoke_fix,
            patch("sova.core.steps.develop.run") as mock_run,
            patch("time.monotonic", side_effect=[0, 650, 660]),
        ):
            mock_invoke_cmd.return_value = MockLLMResult(
                text="done",
                cost_usd=Decimal("0.60"),
                input_tokens=500,
                output_tokens=500,
                session_id="test-session",
            )

            # Mock run for multiple calls: command -v check, actual check runs
            mock_run.side_effect = [
                ShellResult(returncode=0, stdout="/usr/bin/make", stderr=""),  # command -v
                ShellResult(returncode=1, stdout="", stderr="check failed"),  # initial check
            ]

            result = await step.execute(mock_ctx)

            assert not result.success
            assert "time budget exceeded" in result.error
            assert mock_invoke_fix.call_count == 0, "Time gate should prevent LLM fix invocation"

    @pytest.mark.asyncio
    async def test_duplicate_failure_detection(self, mock_ctx, tmp_path):
        """Inner check loop should abort when duplicate failures are detected."""
        step = DevelopStep()

        (tmp_path / "Makefile").write_text("check:\n\tfalse\n")
        mock_ctx.config.check_cmd = "make check"

        check_call_count = 0
        fix_attempt_count = 0

        async def mock_run_check(*args, **kwargs):
            nonlocal check_call_count
            check_call_count += 1
            # Handle sh -c "command -v make"
            if len(args) >= 3 and args[0] == "sh" and args[1] == "-c" and "command -v" in args[2]:
                return ShellResult(returncode=0, stdout="/usr/bin/make", stderr="")
            # Handle sh -c "make check" - this should fail
            if len(args) >= 3 and args[0] == "sh" and args[1] == "-c" and "make check" in args[2]:
                return ShellResult(returncode=1, stdout="", stderr="same error every time")
            # git diff --stat HEAD after fix attempts shows changes
            if args and len(args) >= 3 and args[0] == "git" and args[1] == "diff" and fix_attempt_count > 0:
                return ShellResult(returncode=0, stdout="file.py | 1 +\n", stderr="")
            # Other git commands for change detection
            if args and ("git" in args[0] or (len(args) > 1 and args[0] == "git")):
                return ShellResult(returncode=0, stdout="", stderr="")
            # Default failure
            return ShellResult(returncode=1, stdout="", stderr="same error every time")

        def track_fix_attempts(*args, **kwargs):
            nonlocal fix_attempt_count
            fix_attempt_count += 1
            return MockLLMResult(
                text="attempted fix",
                cost_usd=Decimal("0.05"),
                input_tokens=25,
                output_tokens=25,
                session_id="test-session",
            )

        with (
            patch("sova.core.steps.develop.invoke_command") as mock_invoke_cmd,
            patch("sova.core.steps.develop.invoke", side_effect=track_fix_attempts),
            patch("sova.core.steps.develop.run", side_effect=mock_run_check),
            patch("sova.core.steps.develop._get_dirty_test_files", return_value=set()),
        ):
            mock_invoke_cmd.return_value = MockLLMResult(
                text="done",
                cost_usd=Decimal("0.60"),
                input_tokens=50,
                output_tokens=50,
                session_id="test-session",
            )

            result = await step.execute(mock_ctx)

            assert not result.success
            assert "duplicate failure" in result.error

    @pytest.mark.asyncio
    async def test_fix_timeout_applied(self, mock_ctx, tmp_path):
        """Fix LLM invocations should use fix_timeout."""
        step = DevelopStep()

        (tmp_path / "Makefile").write_text("check:\n\tfalse\n")
        mock_ctx.config.check_cmd = "make check"

        async def mock_run_func(*args, **kwargs):
            # Handle sh -c "command -v make"
            if len(args) >= 3 and args[0] == "sh" and args[1] == "-c" and "command -v" in args[2]:
                return ShellResult(returncode=0, stdout="/usr/bin/make", stderr="")
            # Handle initial check run
            if len(args) >= 3 and args[0] == "sh" and args[1] == "-c" and "make check" in args[2]:
                # First check fails, second check succeeds
                if not hasattr(mock_run_func, "check_count"):
                    mock_run_func.check_count = 0
                mock_run_func.check_count += 1
                if mock_run_func.check_count == 1:
                    return ShellResult(returncode=1, stdout="", stderr="error")
                else:
                    return ShellResult(returncode=0, stdout="", stderr="")
            # git diff after fix shows changes
            if args and len(args) >= 3 and args[0] == "git" and args[1] == "diff" and args[2] == "--stat":
                return ShellResult(returncode=0, stdout="1 file changed", stderr="")
            # Other git commands
            if args and len(args) > 0 and args[0] == "git":
                return ShellResult(returncode=0, stdout="", stderr="")
            # Default
            return ShellResult(returncode=0, stdout="", stderr="")

        with (
            patch("sova.core.steps.develop.invoke_command") as mock_invoke_cmd,
            patch("sova.core.steps.develop.invoke") as mock_invoke,
            patch("sova.core.steps.develop.run", side_effect=mock_run_func),
            patch("sova.core.steps.develop._get_dirty_test_files", return_value=set()),
        ):
            mock_invoke_cmd.return_value = MockLLMResult(
                text="done",
                cost_usd=Decimal("0.60"),
                input_tokens=500,
                output_tokens=500,
                session_id="test-session",
            )

            mock_invoke.return_value = MockLLMResult(
                text="fixed",
                cost_usd=Decimal("0.05"),
                input_tokens=25,
                output_tokens=25,
                session_id="test-session",
            )

            await step.execute(mock_ctx)

            assert mock_invoke.call_count == 1
            assert mock_invoke.call_args.kwargs["timeout"] == 180

    @pytest.mark.asyncio
    async def test_fix_llm_timeout_produces_distinct_marker(self, mock_ctx, tmp_path):
        """A fix-loop LLM timeout must be tagged distinctly from a generic fix-LLM
        failure and from the unrelated step_hard_timeout string (issue #977)."""
        step = DevelopStep()

        (tmp_path / "Makefile").write_text("check:\n\tfalse\n")
        mock_ctx.config.check_cmd = "make check"

        async def mock_run_func(*args, **kwargs):
            if len(args) >= 3 and args[0] == "sh" and args[1] == "-c" and "command -v" in args[2]:
                return ShellResult(returncode=0, stdout="/usr/bin/make", stderr="")
            if len(args) >= 3 and args[0] == "sh" and args[1] == "-c" and "make check" in args[2]:
                return ShellResult(returncode=1, stdout="", stderr="error")
            return ShellResult(returncode=0, stdout="", stderr="")

        with (
            patch("sova.core.steps.develop.invoke_command") as mock_invoke_cmd,
            patch(
                "sova.core.steps.develop.invoke",
                side_effect=LLMTimeoutError("Command timed out after 180s"),
            ),
            patch("sova.core.steps.develop.run", side_effect=mock_run_func),
        ):
            mock_invoke_cmd.return_value = MockLLMResult(
                text="done",
                cost_usd=Decimal("0.60"),
                input_tokens=500,
                output_tokens=500,
                session_id="test-session",
            )

            result = await step.execute(mock_ctx)

        assert not result.success
        assert result.error.startswith("fix_llm_timeout on cycle 1:")
        assert result.error != "step_hard_timeout"
        assert "check fix LLM failed" not in result.error

    @pytest.mark.asyncio
    async def test_fix_llm_generic_failure_produces_generic_marker(self, mock_ctx, tmp_path):
        """A non-timeout fix-LLM RuntimeError gets the generic marker, not the timeout one."""
        step = DevelopStep()

        (tmp_path / "Makefile").write_text("check:\n\tfalse\n")
        mock_ctx.config.check_cmd = "make check"

        async def mock_run_func(*args, **kwargs):
            if len(args) >= 3 and args[0] == "sh" and args[1] == "-c" and "command -v" in args[2]:
                return ShellResult(returncode=0, stdout="/usr/bin/make", stderr="")
            if len(args) >= 3 and args[0] == "sh" and args[1] == "-c" and "make check" in args[2]:
                return ShellResult(returncode=1, stdout="", stderr="error")
            return ShellResult(returncode=0, stdout="", stderr="")

        with (
            patch("sova.core.steps.develop.invoke_command") as mock_invoke_cmd,
            patch("sova.core.steps.develop.invoke", side_effect=RuntimeError("model unavailable")),
            patch("sova.core.steps.develop.run", side_effect=mock_run_func),
        ):
            mock_invoke_cmd.return_value = MockLLMResult(
                text="done",
                cost_usd=Decimal("0.60"),
                input_tokens=500,
                output_tokens=500,
                session_id="test-session",
            )

            result = await step.execute(mock_ctx)

        assert not result.success
        assert result.error.startswith("fix_llm_failed on cycle 1:")
        assert "fix_llm_timeout" not in result.error

    def test_check_loop_budget_stops_when_step_deadline_insufficient(self, mock_ctx):
        """A worst-case cycle (check_timeout + fix_timeout) that cannot fit inside
        the step's own remaining deadline must stop the loop before it starts,
        rather than being hard-killed mid-cycle by the outer step timeout."""
        step = DevelopStep()
        # develop.check_timeout=300 + develop.fix_timeout=180 + a fixed
        # overhead buffer = 540s worst case; 100s remaining cannot fit
        # another cycle.
        mock_ctx.step_time_remaining = 100

        result = step._check_loop_budget(mock_ctx, loop_start_time=time.monotonic(), max_fix_time=600, cycle=1)

        assert result is not None
        assert "step deadline approaching" in result

    def test_check_loop_budget_skips_bailout_when_runaway_capped(self, mock_ctx):
        """When the step's remaining deadline was capped by the run-wide runaway
        wall-clock guard (not the step's own configured timeout), the loop must
        NOT preemptively bail out with a plain failed result: doing so would
        return a StepResult with runaway_triggered unset, routing the failure
        through the generic FAILED path instead of WorkflowEngine's resumable
        PAUSED/"runaway" path. Letting the outer asyncio.timeout fire instead
        preserves that classification (issue #977)."""
        step = DevelopStep()
        mock_ctx.step_time_remaining = 100  # insufficient for a worst-case cycle
        mock_ctx.step_deadline_is_runaway = True

        result = step._check_loop_budget(mock_ctx, loop_start_time=time.monotonic(), max_fix_time=600, cycle=1)

        assert result is None

    def test_check_loop_budget_continues_when_step_deadline_ample(self, mock_ctx):
        """A step with plenty of remaining deadline must not be short-circuited."""
        step = DevelopStep()
        mock_ctx.step_time_remaining = 900  # comfortably above the 540s worst case

        result = step._check_loop_budget(mock_ctx, loop_start_time=time.monotonic(), max_fix_time=600, cycle=1)

        assert result is None

    def test_check_loop_budget_unconstrained_when_step_deadline_unknown(self, mock_ctx):
        """No WorkflowEngine driving the context (step_time_remaining=None) must not
        constrain the loop; only max_fix_time and budget still apply."""
        step = DevelopStep()
        mock_ctx.step_time_remaining = None

        result = step._check_loop_budget(mock_ctx, loop_start_time=time.monotonic(), max_fix_time=600, cycle=1)

        assert result is None

    @pytest.mark.asyncio
    async def test_step_deadline_insufficient_stops_loop_before_first_cycle(self, mock_ctx, tmp_path):
        """End-to-end: when the step's remaining deadline cannot fit a worst-case
        cycle, the loop must exit before invoking the fix LLM at all, leaving
        the step's remaining budget available for later pipeline steps."""
        step = DevelopStep()

        (tmp_path / "Makefile").write_text("check:\n\tfalse\n")
        mock_ctx.config.check_cmd = "make check"
        mock_ctx.step_time_remaining = 100

        async def mock_run_func(*args, **kwargs):
            if len(args) >= 3 and args[0] == "sh" and args[1] == "-c" and "command -v" in args[2]:
                return ShellResult(returncode=0, stdout="/usr/bin/make", stderr="")
            if len(args) >= 3 and args[0] == "sh" and args[1] == "-c" and "make check" in args[2]:
                return ShellResult(returncode=1, stdout="", stderr="error")
            return ShellResult(returncode=0, stdout="", stderr="")

        with (
            patch("sova.core.steps.develop.invoke_command") as mock_invoke_cmd,
            patch("sova.core.steps.develop.invoke") as mock_invoke_fix,
            patch("sova.core.steps.develop.run", side_effect=mock_run_func),
        ):
            mock_invoke_cmd.return_value = MockLLMResult(
                text="done",
                cost_usd=Decimal("0.60"),
                input_tokens=500,
                output_tokens=500,
                session_id="test-session",
            )

            result = await step.execute(mock_ctx)

        assert not result.success
        assert "step deadline approaching" in result.error
        assert mock_invoke_fix.call_count == 0, "No fix cycle should start when it cannot fit the step deadline"


class TestEarlyNoChangeDetection:
    """Tests for early no-change detection."""

    @pytest.mark.asyncio
    async def test_low_cost_no_change_aborts(self, mock_ctx, tmp_path):
        """Develop step should abort when cost is low and no changes were produced."""
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.invoke_command") as mock_invoke,
            patch("sova.core.steps.develop.run") as mock_run,
        ):
            mock_invoke.return_value = MockLLMResult(
                text="done",
                cost_usd=Decimal("0.30"),
                input_tokens=50,
                output_tokens=50,
                session_id="test-session",
            )

            mock_run.return_value = ShellResult(
                returncode=0,
                stdout="",
                stderr="",
            )

            result = await step.execute(mock_ctx)

            assert not result.success
            assert "$0.50 threshold" in result.error

    @pytest.mark.asyncio
    async def test_low_cost_with_changes_continues(self, mock_ctx, tmp_path):
        """Develop step should continue when cost is low but changes were produced."""
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.invoke_command") as mock_invoke,
            patch("sova.core.steps.develop.run") as mock_run,
        ):
            mock_invoke.return_value = MockLLMResult(
                text="done",
                cost_usd=Decimal("0.30"),
                input_tokens=50,
                output_tokens=50,
                session_id="test-session",
            )

            mock_run.return_value = ShellResult(
                returncode=0,
                stdout="1 file changed, 10 insertions(+)",
                stderr="",
            )

            result = await step.execute(mock_ctx)

            assert result.success or "$0.50" not in (result.error or "")
