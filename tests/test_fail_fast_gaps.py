"""Tests for fail-fast gap closures in issue #689."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from sova.config.models import AgentConfig, DevelopConfig
from sova.core.context import ExecutionContext
from sova.core.steps.develop import DevelopStep
from sova.core.workflow import WorkflowEngine
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
