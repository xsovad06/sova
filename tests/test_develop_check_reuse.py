"""DevelopStep's inner check loop reuses the develop agent's own recorded full check.

The develop agent runs the full check command and records a pass in the check
cache (``develop.md``). The step then ran the same command again on the same
files. Its first run is now skipped when a pass is recorded for the identical
worktree content and command; a run after a fix cycle always happens, since
the fix changed what the recorded pass verified.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.config.models import DevelopConfig, ProjectConfig
from sova.core.check_cache import PIPELINE_STAGE_ENV_KEY, has_recorded_pass, record_result
from sova.core.context import ExecutionContext
from sova.core.steps.develop import DevelopStep
from sova.llm.models import LLMResult
from tests.conftest import InvariantRepo

CHECK_CMD = "make check"


@pytest.fixture(autouse=True)
def _outside_a_pipeline_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The step process never has SOVA_PIPELINE_STAGE, so the reuse must not depend on it."""
    monkeypatch.delenv(PIPELINE_STAGE_ENV_KEY, raising=False)


def _ctx(worktree: Path) -> ExecutionContext:
    adapter = MagicMock()
    adapter.repo = "test/repo"
    return ExecutionContext(
        project_dir=worktree,
        config=ProjectConfig(check_cmd=CHECK_CMD, develop=DevelopConfig(guard_test_weakening=False)),
        adapter=adapter,
        issue_number="42",
        role="developer",
        worktree_dir=worktree,
    )


def _check_runs(mock_run: AsyncMock) -> int:
    return sum(1 for call in mock_run.call_args_list if call.args[:3] == ("sh", "-c", CHECK_CMD))


class TestHasRecordedPass:
    async def test_true_for_a_matching_pass_outside_a_pipeline_dispatch(self, invariant_repo: InvariantRepo) -> None:
        await record_result(invariant_repo.dir, CHECK_CMD, passed=True)

        assert await has_recorded_pass(invariant_repo.dir, CHECK_CMD) is True

    async def test_false_once_the_worktree_changed(self, invariant_repo: InvariantRepo) -> None:
        await record_result(invariant_repo.dir, CHECK_CMD, passed=True)
        invariant_repo.write("README.md", "# edited\n")

        assert await has_recorded_pass(invariant_repo.dir, CHECK_CMD) is False

    async def test_false_when_the_worktree_hash_is_unavailable(self, invariant_repo: InvariantRepo) -> None:
        await record_result(invariant_repo.dir, CHECK_CMD, passed=True)

        with patch("sova.core.check_cache._compute_hash", AsyncMock(return_value=None)):
            assert await has_recorded_pass(invariant_repo.dir, CHECK_CMD) is False

    async def test_false_for_a_recorded_failure(self, invariant_repo: InvariantRepo) -> None:
        await record_result(invariant_repo.dir, CHECK_CMD, passed=False)

        assert await has_recorded_pass(invariant_repo.dir, CHECK_CMD) is False


class TestFirstCheckReusesTheAgentsPass:
    async def test_recorded_pass_for_the_same_state_skips_the_command(self, invariant_repo: InvariantRepo) -> None:
        await record_result(invariant_repo.dir, CHECK_CMD, passed=True)

        with patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="", stderr="")  # command -v probe
            passed, summary = await DevelopStep()._run_inner_check_loop(_ctx(invariant_repo.dir))

        assert passed is True
        assert summary == "checks passed (reused the recorded run)"
        assert _check_runs(mock_run) == 0

    async def test_an_unset_check_cmd_matches_the_command_the_agent_was_rendered(
        self, invariant_repo: InvariantRepo
    ) -> None:
        """With check_cmd unset the step resolves `make check`, the agent's template renders `lint && test`."""
        from sova.commands.templates import build_variables

        ctx = _ctx(invariant_repo.dir)
        ctx.config = ProjectConfig(develop=DevelopConfig(guard_test_weakening=False))
        invariant_repo.write("Makefile", "check:\n\ttrue\n")
        rendered = build_variables(ctx.config)["check_cmd"]
        assert rendered != CHECK_CMD, "the two spellings must differ for this test to mean anything"
        await record_result(invariant_repo.dir, rendered, passed=True)

        with patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="", stderr="")
            passed, summary = await DevelopStep()._run_inner_check_loop(ctx)

        assert (passed, summary) == (True, "checks passed (reused the recorded run)")
        assert _check_runs(mock_run) == 0

    async def test_a_change_after_the_recorded_pass_runs_the_command(self, invariant_repo: InvariantRepo) -> None:
        await record_result(invariant_repo.dir, CHECK_CMD, passed=True)
        invariant_repo.write("new_module.py", "VALUE = 1\n")

        with patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="ok", stderr="")
            passed, summary = await DevelopStep()._run_inner_check_loop(_ctx(invariant_repo.dir))

        assert (passed, summary) == (True, "checks passed")
        assert _check_runs(mock_run) == 1

    async def test_a_failed_lookup_runs_the_command(self, invariant_repo: InvariantRepo) -> None:
        with (
            patch("sova.core.steps.develop.has_recorded_pass", AsyncMock(side_effect=OSError("disk"))),
            patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = MagicMock(success=True, stdout="ok", stderr="")
            passed, _summary = await DevelopStep()._run_inner_check_loop(_ctx(invariant_repo.dir))

        assert passed is True
        assert _check_runs(mock_run) == 1

    async def test_the_run_after_a_fix_cycle_always_happens(self, invariant_repo: InvariantRepo) -> None:
        """A pass the fix agent records for its own edit never replaces the step's re-run."""
        worktree = invariant_repo.dir

        async def fix_and_record(*_args: object, **_kwargs: object) -> LLMResult:
            (worktree / "fixed.py").write_text("FIXED = True\n", encoding="utf-8")
            await record_result(worktree, CHECK_CMD, passed=True)
            return LLMResult(text="Fixed", model="m", cost_usd=Decimal("0"), input_tokens=1, output_tokens=1)

        responses = {CHECK_CMD: [MagicMock(success=False, stdout="FAILED", stderr=""), MagicMock(success=True)]}

        async def fake_run(*args: str, **_kwargs: object) -> MagicMock:
            if args[:3] == ("sh", "-c", CHECK_CMD):
                return responses[CHECK_CMD].pop(0)
            return MagicMock(success=True, stdout="changed\n", stderr="")

        with (
            patch("sova.core.steps.develop.run", AsyncMock(side_effect=fake_run)) as mock_run,
            patch("sova.core.steps.develop.dispatch_prompt", AsyncMock(side_effect=fix_and_record)),
        ):
            passed, summary = await DevelopStep()._run_inner_check_loop(_ctx(worktree))

        assert (passed, summary) == (True, "checks passed after 1 fix cycle(s)")
        assert _check_runs(mock_run) == 2
