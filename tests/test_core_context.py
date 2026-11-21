"""TaskStatus, ExecutionContext, step result types, BaseStep and the step registry.

Split from tests/test_core.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.config.models import ProjectConfig
from sova.core.state import TaskStatus
from sova.core.steps.base import GateCheckResult, StepResult
from tests.core_helpers import DummyStep, _make_ctx

# Every test in this module runs against the shared per-worker in-memory DB
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


# ---------------------------------------------------------------------------
# TaskStatus (state machine)
# ---------------------------------------------------------------------------


class TestTaskStatus:
    def test_all_states_exist(self) -> None:
        expected = {
            "RUNNING",
            "PENDING",
            "ASSESSING",
            "RESEARCHED",
            "IN_PROGRESS",
            "DEVELOPING",
            "SIMPLIFYING",
            "REVIEWING",
            "COMMITTING",
            "PUSHING",
            "PR_CREATED",
            "CI_MONITORING",
            "AUTOMATED_REVIEW",
            "ADDRESSING_REVIEW",
            "AWAITING_APPROVAL",
            "DONE",
            "PAUSED",
            "FAILED",
            "REJECTED",
        }
        actual = {s.name for s in TaskStatus}
        assert expected == actual

    def test_awaiting_approval_in_task_run_terminal(self) -> None:
        from sova.core.state import TASK_RUN_TERMINAL

        assert "awaiting_approval" in TASK_RUN_TERMINAL

    def test_paused_in_task_run_terminal(self) -> None:
        """Paused runs are terminal for DB queries (process has exited)."""
        from sova.core.state import TASK_RUN_TERMINAL

        assert "paused" in TASK_RUN_TERMINAL


# ---------------------------------------------------------------------------
# ExecutionContext
# ---------------------------------------------------------------------------


class TestExecutionContext:
    def test_create_minimal(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        assert ctx.project_dir == tmp_path
        assert ctx.issue_number == "42"
        assert ctx.role == "developer"
        assert ctx.cost_usd == Decimal("0")

    def test_add_cost(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        ctx.add_cost(Decimal("1.50"))
        ctx.add_cost(Decimal("0.75"))
        assert ctx.cost_usd == Decimal("2.25")

    def test_budget_exceeded(self, tmp_path: Path) -> None:
        config = ProjectConfig(agent={"max_budget": Decimal("5.00")})
        ctx = _make_ctx(tmp_path, config=config)
        ctx.add_cost(Decimal("5.01"))
        assert ctx.is_budget_exceeded

    def test_budget_not_exceeded(self, tmp_path: Path) -> None:
        config = ProjectConfig(agent={"max_budget": Decimal("5.00")})
        ctx = _make_ctx(tmp_path, config=config)
        ctx.add_cost(Decimal("4.99"))
        assert not ctx.is_budget_exceeded

    def test_budget_remaining_fraction_full_at_zero_cost(self, tmp_path: Path) -> None:
        config = ProjectConfig(agent={"max_budget": Decimal("10.00")})
        ctx = _make_ctx(tmp_path, config=config)
        assert ctx.budget_remaining_fraction == 1.0

    def test_budget_remaining_fraction_half(self, tmp_path: Path) -> None:
        config = ProjectConfig(agent={"max_budget": Decimal("10.00")})
        ctx = _make_ctx(tmp_path, config=config)
        ctx.add_cost(Decimal("5.00"))
        assert ctx.budget_remaining_fraction == 0.5

    def test_budget_remaining_fraction_clamped_when_over_budget(self, tmp_path: Path) -> None:
        config = ProjectConfig(agent={"max_budget": Decimal("5.00")})
        ctx = _make_ctx(tmp_path, config=config)
        ctx.add_cost(Decimal("7.00"))
        assert ctx.budget_remaining_fraction == 0.0

    def test_budget_remaining_fraction_zero_at_exact_max_budget(self, tmp_path: Path) -> None:
        config = ProjectConfig(agent={"max_budget": Decimal("5.00")})
        ctx = _make_ctx(tmp_path, config=config)
        ctx.add_cost(Decimal("5.00"))
        assert ctx.budget_remaining_fraction == 0.0

    def test_resource_remaining_fraction_matches_budget_when_others_disabled(self, tmp_path: Path) -> None:
        config = ProjectConfig(
            agent={"max_budget": Decimal("10.00")},
            runaway={"max_run_wall_clock_seconds": 0, "max_llm_calls": 0},
        )
        ctx = _make_ctx(tmp_path, config=config)
        ctx.add_cost(Decimal("5.00"))
        assert ctx.resource_remaining_fraction == 0.5

    def test_resource_remaining_fraction_constrained_by_call_count(self, tmp_path: Path) -> None:
        """The composite fraction reacts to LLM call pressure even with $0 spent,
        the exact R6 gap budget_remaining_fraction alone cannot see."""
        from sova.llm.client import _increment_call_counter, reset_call_counter, start_call_counter

        config = ProjectConfig(
            agent={"max_budget": Decimal("10.00")},
            runaway={"max_run_wall_clock_seconds": 0, "max_llm_calls": 10},
        )
        ctx = _make_ctx(tmp_path, config=config)
        start_call_counter()
        try:
            _increment_call_counter(9)
            assert ctx.budget_remaining_fraction == 1.0
            assert ctx.resource_remaining_fraction == pytest.approx(0.1)
        finally:
            reset_call_counter()

    def test_resource_remaining_fraction_constrained_by_wall_clock(self, tmp_path: Path) -> None:
        import time

        config = ProjectConfig(
            agent={"max_budget": Decimal("10.00")},
            runaway={"max_run_wall_clock_seconds": 100, "max_llm_calls": 0},
        )
        ctx = _make_ctx(tmp_path, config=config)
        ctx.run_started_at = time.monotonic() - 90.0

        assert ctx.budget_remaining_fraction == 1.0
        assert ctx.resource_remaining_fraction == pytest.approx(0.1, abs=0.02)

    def test_resource_remaining_fraction_wall_clock_scaled_by_complexity(self, tmp_path: Path) -> None:
        import time

        from sova.llm.complexity import ComplexityTier

        config = ProjectConfig(
            agent={"max_budget": Decimal("10.00")},
            runaway={"max_run_wall_clock_seconds": 100, "max_llm_calls": 0},
        )
        ctx = _make_ctx(tmp_path, config=config)
        ctx.complexity = ComplexityTier.EPIC
        # 90s elapsed against a flat 100s ceiling would read as nearly exhausted,
        # but the EPIC 2.0x multiplier scales the ceiling to 200s.
        ctx.run_started_at = time.monotonic() - 90.0

        assert ctx.resource_remaining_fraction == pytest.approx(0.55, abs=0.02)

    def test_resource_remaining_fraction_full_when_run_not_started(self, tmp_path: Path) -> None:
        config = ProjectConfig(runaway={"max_run_wall_clock_seconds": 100})
        ctx = _make_ctx(tmp_path, config=config)
        assert ctx.run_started_at == 0.0
        assert ctx.resource_remaining_fraction == 1.0

    def test_routing_task_type_passes_tag_through(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        assert ctx.routing_task_type("develop") == "develop"

    def test_routing_task_type_treats_untagged_step_as_unset(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        assert ctx.routing_task_type("") is None

    def test_routing_task_type_suppressed_while_falling_back(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, fallback_model_index=1)
        assert ctx.routing_task_type("develop") is None

    def test_working_dir_defaults_to_project(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        assert ctx.working_dir == tmp_path

    def test_working_dir_uses_worktree(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "wt")
        assert ctx.working_dir == tmp_path / "wt"

    def test_repo_property(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, config=ProjectConfig(github_repo="owner/repo"))
        assert ctx.repo == "owner/repo"

    def test_issueless_context(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, issue_number="", run_label="planner-1234")
        assert not ctx.has_issue
        assert ctx.display_label == "planner-1234"
        assert ctx.issue_number == ""

    def test_issueless_context_no_label(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, issue_number="", run_label="", task_run_id=99)
        assert not ctx.has_issue
        assert ctx.display_label == "run-99"

    def test_issueless_context_fallback(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, issue_number="", run_label="")
        assert ctx.display_label == "issue-less"

    def test_issue_context_display(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path, issue_number="42")
        assert ctx.has_issue
        assert ctx.display_label == "#42"


class TestStepResult:
    def test_success(self) -> None:
        r = StepResult(success=True, summary="ok")
        assert r.success
        assert r.error is None

    def test_failure(self) -> None:
        r = StepResult(success=False, summary="bad", error="something broke")
        assert not r.success
        assert r.error == "something broke"


class TestGateCheckResult:
    def test_pass(self) -> None:
        r = GateCheckResult(passed=True)
        assert r.passed
        assert r.reason is None

    def test_fail(self) -> None:
        r = GateCheckResult(passed=False, reason="No code changes")
        assert not r.passed
        assert r.reason == "No code changes"


class TestBaseStep:
    async def test_execute_success(self, tmp_path: Path) -> None:
        step = DummyStep(should_pass=True)
        ctx = _make_ctx(tmp_path)
        result = await step.execute(ctx)
        assert result.success

    async def test_execute_failure(self, tmp_path: Path) -> None:
        step = DummyStep(should_pass=False)
        ctx = _make_ctx(tmp_path)
        result = await step.execute(ctx)
        assert not result.success

    async def test_gate_check(self, tmp_path: Path) -> None:
        step = DummyStep(gate_pass=True)
        ctx = _make_ctx(tmp_path)
        gate = await step.validate_output(ctx)
        assert gate.passed

    async def test_can_skip(self, tmp_path: Path) -> None:
        step = DummyStep(skip=True)
        ctx = _make_ctx(tmp_path)
        assert await step.can_skip(ctx)


class TestStepRegistry:
    def test_get_developer_steps_returns_all(self) -> None:
        from sova.core.steps import get_developer_steps

        steps = get_developer_steps()
        names = [s.name for s in steps]
        assert names == [
            "sync",
            "assess",
            "create_worktree",
            "capture_baseline",
            "develop",
            "simplify",
            "self_review",
            "commit",
            "validate",
            "push",
            "create_pr",
            "wait_for_external_reviews",
            "address_external_findings",
            "monitor_ci",
            "address_sonarcloud_findings",
            "confidence_score",
            "extract_memory",
            "handoff_to_reviewer",
        ]

    def test_get_address_review_steps(self) -> None:
        from sova.core.steps import get_address_review_steps

        steps = get_address_review_steps()
        names = [s.name for s in steps]
        assert names == [
            "ensure_worktree",
            "rebase",
            "address_review",
            "rearrange_commits",
            "validate",
            "push",
            "resolve_review_threads",
            "refresh_pr_body",
            "monitor_ci",
            "address_sonarcloud_findings",
            "resolve_external_reviews",
            "extract_memory",
            "handoff_to_reviewer",
        ]


# ---------------------------------------------------------------------------
# get_active_gh_user (sova/utils/gh.py)
# ---------------------------------------------------------------------------


class TestGetActiveGhUser:
    async def test_returns_active_login(self) -> None:
        from sova.utils.gh import get_active_gh_user

        auth_json = json.dumps(
            {
                "hosts": {
                    "github.com": [
                        {"active": True, "login": "dsova06"},
                        {"active": False, "login": "xsovad06"},
                    ]
                }
            }
        )
        with patch("sova.utils.gh.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=auth_json, stderr="")
            result = await get_active_gh_user()

        assert result == "dsova06"

    async def test_returns_none_on_command_failure(self) -> None:
        from sova.utils.gh import get_active_gh_user

        with patch("sova.utils.gh.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stdout="", stderr="gh not found")
            result = await get_active_gh_user()

        assert result is None

    async def test_returns_none_on_bad_json(self) -> None:
        from sova.utils.gh import get_active_gh_user

        with patch("sova.utils.gh.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="not json", stderr="")
            result = await get_active_gh_user()

        assert result is None

    async def test_returns_none_when_no_active_account(self) -> None:
        from sova.utils.gh import get_active_gh_user

        auth_json = json.dumps({"hosts": {"github.com": [{"active": False, "login": "xsovad06"}]}})
        with patch("sova.utils.gh.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=auth_json, stderr="")
            result = await get_active_gh_user()

        assert result is None

    async def test_returns_none_when_hosts_empty(self) -> None:
        from sova.utils.gh import get_active_gh_user

        auth_json = json.dumps({"hosts": {}})
        with patch("sova.utils.gh.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=auth_json, stderr="")
            result = await get_active_gh_user()

        assert result is None

    async def test_returns_none_on_unexpected_structure(self) -> None:
        from sova.utils.gh import get_active_gh_user

        auth_json = json.dumps({"hosts": "not-a-dict"})
        with patch("sova.utils.gh.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=auth_json, stderr="")
            result = await get_active_gh_user()

        assert result is None
