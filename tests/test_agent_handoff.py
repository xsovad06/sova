"""Tests for sova.dashboard.services.agent_handoff."""

from __future__ import annotations

from pathlib import Path

import pytest

from sova.db.models import StepExecution, TaskRun
from sova.db.session import close_db, get_session, init_db

# agent_handoff.py resolves `get_session` via a function-local `from sova.db.session
# import get_session` on every call (not a module-level import), so patching
# "sova.db.session.get_session" at its definition module takes effect correctly here,
# unlike the general rule of patching at the importing module's namespace.


@pytest.fixture(autouse=True)
async def setup_db(monkeypatch):
    """Initialize an in-memory DB for agent_handoff tests."""
    monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
    await init_db(run_migrations=False)
    yield
    await close_db()


class TestAutoHandoffIssueMismatch:
    async def test_skips_when_handoff_issue_mismatches(self) -> None:
        """Auto-handoff should not execute if handoff is for a different issue."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 1, "issue": "114", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="113",
            summary="test",
            next_actions=[
                HandoffAction(id="review", label="Review", auto_execute=True, mode="agent"),
            ],
        )

        mock_lifecycle = AsyncMock()
        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_lifecycle.start_agent),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
        ):
            await _process_auto_handoff(agent)

        mock_lifecycle.start_agent.assert_not_awaited()
        mock_clear.assert_not_called()

    async def test_does_not_persist_when_issue_mismatches_with_nonempty_details(self) -> None:
        """When the handoff issue mismatches the agent issue, no persist must happen
        even if details is non-empty: writing another issue's verdict would corrupt
        the completing run's handoff_json."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        findings = [{"file": "b.py", "line": 2, "severity": 8, "category": "bug", "description": "Y", "suggestion": ""}]

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="200", role="reviewer", status="done", pr_number=60)
                session.add(run)
                await session.flush()
                run_id = run.id

        # Handoff is for issue 201 but agent is running issue 200: mismatch
        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="201",
            pr_number=60,
            summary="1 finding",
            details={"next_action": "address_review", "pending_findings": findings},
            next_actions=[
                HandoffAction(
                    id="address_review",
                    label="Address",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "201", "role": "developer", "pr": 60},
                ),
            ],
        )

        agent = type("AgentState", (), {"run_id": run_id, "issue": "200", "project_dir": Path("/tmp/test")})()

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock()
        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_not_awaited()
        mock_clear.assert_not_called()

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed is not None
                assert not refreshed.handoff_json, "mismatched handoff details must not be persisted"

    async def test_executes_when_handoff_issue_matches(self) -> None:
        """Auto-handoff should execute normally when issues match."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 1, "issue": "113", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="113",
            pr_number=130,
            summary="test",
            next_actions=[
                HandoffAction(
                    id="review",
                    label="Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "113", "role": "reviewer"},
                ),
            ],
        )

        mock_start = AsyncMock(return_value={"run_id": 2})
        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once()
        mock_clear.assert_called_once()
        assert mock_clear.call_args[1].get("issue") == "113"

    async def test_persists_handoff_details_to_completing_run(self) -> None:
        """_process_auto_handoff must persist reviewer handoff details to TaskRun.handoff_json
        before clearing the file, so get_sova_review_verdict() finds the real verdict later."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        findings = [{"file": "a.py", "line": 1, "severity": 9, "category": "bug", "description": "X", "suggestion": ""}]

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="120", role="reviewer", status="done", pr_number=50)
                session.add(run)
                await session.flush()
                run_id = run.id

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="120",
            pr_number=50,
            summary="1 finding",
            details={"next_action": "address_review", "pending_findings": findings, "cost_usd": "0.01"},
            next_actions=[
                HandoffAction(
                    id="address_review",
                    label="Address",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "120", "role": "developer", "pr": 50},
                ),
            ],
        )

        agent = type("AgentState", (), {"run_id": run_id, "issue": "120", "project_dir": Path("/tmp/test")})()

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock(return_value={"run_id": 99})
        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
            patch(
                "sova.config.loader.load_config",
                return_value=MagicMock(pipeline=MagicMock(max_address_review_cycles=0)),
            ),
        ):
            await _process_auto_handoff(agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed is not None
                assert refreshed.handoff_json is not None, "handoff_json must be persisted before file is cleared"
                assert refreshed.handoff_json.get("next_action") == "address_review"
                persisted_findings = refreshed.handoff_json.get("pending_findings", [])
                assert len(persisted_findings) == 1
                assert persisted_findings[0]["severity"] == 9

    async def test_skips_persist_when_handoff_json_already_set(self) -> None:
        """_process_auto_handoff must not overwrite handoff_json if already populated
        (subprocess wrote it correctly to the right DB)."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        existing_handoff = {"next_action": "approve", "pending_findings": [], "role": "reviewer"}

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="121",
                    role="reviewer",
                    status="done",
                    pr_number=51,
                    handoff_json=existing_handoff,
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="121",
            pr_number=51,
            summary="No findings",
            details={"next_action": "approve", "pending_findings": [], "cost_usd": "0.01"},
            next_actions=[
                HandoffAction(
                    id="integrate",
                    label="Integrate PR",
                    auto_execute=False,
                    mode="claude-command",
                    command="/integrate-pr 51",
                ),
            ],
        )

        agent = type("AgentState", (), {"run_id": run_id, "issue": "121", "project_dir": Path("/tmp/test")})()

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.handoff_json == existing_handoff, "pre-existing handoff_json must not be overwritten"


class TestAutoHandoffCircuitBreaker:
    async def test_blocks_after_max_cycles(self) -> None:
        """Circuit breaker should block auto address-review after max cycles."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        # Seed 2 completed address-review runs for issue 115, PR 130
        # Each needs an "address_review" StepExecution to be counted
        async with await get_session() as session:
            async with session.begin():
                r1 = TaskRun(issue_number="115", role="developer", status="done", pr_number=130)
                r2 = TaskRun(issue_number="115", role="developer", status="done", pr_number=130)
                session.add_all([r1, r2])
            await session.flush()
            async with session.begin():
                session.add(StepExecution(task_run_id=r1.id, step_name="address_review", status="done"))
                session.add(StepExecution(task_run_id=r2.id, step_name="address_review", status="done"))

        agent = type(
            "AgentState",
            (),
            {"run_id": 10, "issue": "115", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="115",
            pr_number=130,
            summary="Findings to address",
            next_actions=[
                HandoffAction(
                    id="address",
                    label="Address Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "115", "role": "developer", "pr": 130},
                ),
            ],
        )

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock()
        mock_clear = MagicMock()
        mock_write = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 2
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.ipc.handoff.write_handoff_file", mock_write),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        # Agent should NOT be spawned
        mock_start.assert_not_awaited()
        # Blocked handoff should be written with manual-only actions
        mock_write.assert_called_once()
        blocked = mock_write.call_args[0][1]
        assert blocked.source == "circuit_breaker"
        assert all(not a.auto_execute for a in blocked.next_actions)
        assert "Circuit breaker" in blocked.summary

    async def test_allows_under_limit(self) -> None:
        """Circuit breaker should allow address-review when under the limit."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        # Seed only 1 completed address-review run (limit is 2)
        async with await get_session() as session:
            async with session.begin():
                r1 = TaskRun(issue_number="116", role="developer", status="done", pr_number=131)
                session.add(r1)
            await session.flush()
            async with session.begin():
                session.add(StepExecution(task_run_id=r1.id, step_name="address_review", status="done"))

        agent = type(
            "AgentState",
            (),
            {"run_id": 11, "issue": "116", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="116",
            pr_number=131,
            summary="Findings to address",
            next_actions=[
                HandoffAction(
                    id="address",
                    label="Address Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "116", "role": "developer", "pr": 131},
                ),
            ],
        )

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock(return_value={"run_id": 12})
        mock_clear = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 2
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once()

    async def test_skips_when_pr_number_is_none(self) -> None:
        """Circuit breaker should not fire when pr_number is None."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 13, "issue": "117", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="117",
            summary="Findings",
            next_actions=[
                HandoffAction(
                    id="address",
                    label="Address Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "117", "role": "developer"},
                ),
            ],
        )

        mock_start = AsyncMock(return_value={"run_id": 14})
        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
        ):
            await _process_auto_handoff(agent)

        # Should proceed without checking circuit breaker
        mock_start.assert_awaited_once()

    async def test_skips_for_non_developer_role(self) -> None:
        """Circuit breaker should not fire for non-developer roles (e.g., reviewer)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 15, "issue": "118", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="118",
            pr_number=132,
            summary="Ready for review",
            next_actions=[
                HandoffAction(
                    id="review",
                    label="Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "118", "role": "reviewer", "pr": 132},
                ),
            ],
        )

        mock_start = AsyncMock(return_value={"run_id": 16})
        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once()

    async def test_zero_limit_disables_breaker(self) -> None:
        """Setting max_address_review_cycles=0 should disable the circuit breaker."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        # Seed many completed address-review runs
        async with await get_session() as session:
            runs = []
            async with session.begin():
                for _ in range(10):
                    r = TaskRun(issue_number="119", role="developer", status="done", pr_number=133)
                    session.add(r)
                    runs.append(r)
            await session.flush()
            async with session.begin():
                for r in runs:
                    session.add(StepExecution(task_run_id=r.id, step_name="address_review", status="done"))

        agent = type(
            "AgentState",
            (),
            {"run_id": 17, "issue": "119", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="119",
            pr_number=133,
            summary="Findings",
            next_actions=[
                HandoffAction(
                    id="address",
                    label="Address Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "119", "role": "developer", "pr": 133},
                ),
            ],
        )

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock(return_value={"run_id": 18})
        mock_clear = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 0
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once()

    async def test_initial_dev_run_not_counted(self) -> None:
        """Initial developer run (with pr_number but no address_review step) should not count."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        # Seed: 1 initial dev run (no address_review step) + 1 address-review run
        async with await get_session() as session:
            async with session.begin():
                initial = TaskRun(issue_number="120", role="developer", status="done", pr_number=134)
                ar1 = TaskRun(issue_number="120", role="developer", status="done", pr_number=134)
                session.add_all([initial, ar1])
            await session.flush()
            async with session.begin():
                # Only the address-review run has the step record
                session.add(StepExecution(task_run_id=ar1.id, step_name="address_review", status="done"))

        agent = type(
            "AgentState",
            (),
            {"run_id": 19, "issue": "120", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="120",
            pr_number=134,
            summary="Findings to address",
            next_actions=[
                HandoffAction(
                    id="address",
                    label="Address Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "120", "role": "developer", "pr": 134},
                ),
            ],
        )

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock(return_value={"run_id": 20})
        mock_clear = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 2
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        # Should proceed: only 1 address-review run counted (not 2)
        mock_start.assert_awaited_once()

    async def test_circuit_breaker_isolates_by_issue(self) -> None:
        """Runs for issue 115 should not block address-review for issue 121.

        Seeded runs share PR 141 with the issue-121 handoff (not a distinct PR
        number) so this test actually exercises issue-based isolation: a filter
        that only matched on pr_number would incorrectly count these runs too.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sqlalchemy import select

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        # Seed 3 completed address-review runs for issue 115 (over limit)
        async with await get_session() as session:
            async with session.begin():
                for _ in range(3):
                    r = TaskRun(issue_number="115", role="developer", status="done", pr_number=141)
                    session.add(r)
            await session.flush()
            async with session.begin():
                for r in (await session.execute(select(TaskRun).where(TaskRun.pr_number == 141))).scalars():
                    session.add(StepExecution(task_run_id=r.id, step_name="address_review", status="done"))

        # Handoff is for issue 121 (no prior runs)
        agent = type(
            "AgentState",
            (),
            {"run_id": 21, "issue": "121", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="121",
            pr_number=141,
            summary="Findings to address",
            next_actions=[
                HandoffAction(
                    id="address",
                    label="Address Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "121", "role": "developer", "pr": 141},
                ),
            ],
        )

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock(return_value={"run_id": 22})
        mock_clear = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 2
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        # Issue 121 should NOT be blocked by issue 115's runs
        mock_start.assert_awaited_once()


class TestAutoHandoff:
    async def test_auto_handoff_spawns_agent(self) -> None:
        """_process_auto_handoff should auto-spawn an agent for auto_execute actions."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = AgentState(
            run_id=1,
            issue="42",
            role="developer",
            process=MagicMock(),
        )

        handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="42",
            pr_number=10,
            branch="feat/test",
            summary="Ready for review",
            next_actions=[
                HandoffAction(
                    id="review",
                    label="Review",
                    mode="agent",
                    args={"issue": "42", "pr": 10, "role": "reviewer"},
                    auto_execute=True,
                ),
            ],
        )

        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch.object(
                agent_lifecycle, "start_agent", new_callable=AsyncMock, return_value={"status": "started"}
            ) as mock_start,
            patch("sova.dashboard.services.handoff_service.clear_handoff") as mock_clear,
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("42", role="reviewer", pr_number=10, slug=None)
        mock_clear.assert_called_once()

    async def test_auto_handoff_skips_non_auto_actions(self) -> None:
        """_process_auto_handoff should not trigger actions without auto_execute."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = AgentState(
            run_id=1,
            issue="42",
            role="reviewer",
            process=MagicMock(),
        )

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="42",
            pr_number=10,
            summary="Clean review",
            next_actions=[
                HandoffAction(
                    id="integrate",
                    label="Integrate PR",
                    mode="claude-command",
                    command="/integrate-pr 10",
                    auto_execute=False,
                ),
            ],
        )

        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch.object(agent_lifecycle, "start_agent", new_callable=AsyncMock) as mock_agent,
            patch.object(agent_lifecycle, "start_command", new_callable=AsyncMock) as mock_cmd,
        ):
            await _process_auto_handoff(agent)

        mock_agent.assert_not_awaited()
        mock_cmd.assert_not_awaited()

    async def test_auto_handoff_claude_command(self) -> None:
        """_process_auto_handoff should run claude-command actions with auto_execute."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = AgentState(
            run_id=1,
            issue="42",
            role="reviewer",
            process=MagicMock(),
        )

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="42",
            pr_number=10,
            summary="Clean review",
            next_actions=[
                HandoffAction(
                    id="integrate",
                    label="Integrate PR",
                    mode="claude-command",
                    command="/integrate-pr 10",
                    args={"issue": "42", "pr": 10},
                    auto_execute=True,
                ),
            ],
        )

        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch.object(
                agent_lifecycle, "start_command", new_callable=AsyncMock, return_value={"status": "started"}
            ) as mock_cmd,
            patch("sova.dashboard.services.handoff_service.clear_handoff") as mock_clear,
        ):
            await _process_auto_handoff(agent)

        mock_cmd.assert_awaited_once_with("integrate-pr", {"issue": "42", "pr": 10}, slug=None)
        mock_clear.assert_called_once()

    async def test_auto_handoff_invalid_pr_number_falls_back_to_none(self) -> None:
        """When args['pr'] is non-numeric, int() raises ValueError.
        _process_auto_handoff must catch it, log a warning, and continue with pr_num=None."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = AgentState(run_id=5, issue="55", role="developer", process=MagicMock())

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="55",
            pr_number=None,
            summary="findings",
            next_actions=[
                HandoffAction(
                    id="address_review",
                    label="Address",
                    mode="agent",
                    args={"issue": "55", "role": "developer", "pr": "pr-not-a-number"},
                    auto_execute=True,
                ),
            ],
        )

        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch.object(
                agent_lifecycle, "start_agent", new_callable=AsyncMock, return_value={"status": "started"}
            ) as mock_start,
            patch("sova.dashboard.services.handoff_service.clear_handoff"),
            patch(
                "sova.config.loader.load_config",
                return_value=MagicMock(pipeline=MagicMock(max_address_review_cycles=0)),
            ),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("55", role="developer", pr_number=None, slug=None)

    async def test_auto_handoff_no_handoff_file(self) -> None:
        """_process_auto_handoff should handle missing handoff gracefully."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff

        agent = AgentState(
            run_id=1,
            issue="42",
            role="developer",
            process=MagicMock(),
        )

        with patch("sova.ipc.handoff.read_handoff_file", return_value=None):
            await _process_auto_handoff(agent)  # Should not raise

    async def test_auto_handoff_skips_stale_review_when_already_reviewed(self) -> None:
        """When a review action's PR already has a completed SOVA review (timing race:
        the developer wrote a "please review" handoff after the reviewer finished),
        _process_auto_handoff must not spawn a duplicate reviewer: it should clear
        the stale handoff and return instead. Regression coverage for PR #514."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = AgentState(run_id=9, issue="90", role="developer", process=MagicMock())

        handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="90",
            pr_number=15,
            branch="feat/test",
            summary="Ready for review",
            next_actions=[
                HandoffAction(
                    id="review",
                    label="Review",
                    mode="agent",
                    args={"issue": "90", "pr": 15, "role": "reviewer"},
                    auto_execute=True,
                ),
            ],
        )

        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                return_value={"has_sova_review": True, "verdict": "approve"},
            ),
            patch.object(agent_lifecycle, "start_agent", new_callable=AsyncMock) as mock_start,
            patch("sova.dashboard.services.handoff_service.clear_handoff") as mock_clear,
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_not_awaited()
        mock_clear.assert_called_once_with(agent.project_dir, issue=agent.issue)


class TestAutoHandoffMemoryGate:
    async def test_memory_block_stops_auto_handoff(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 1, "issue": "42", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="42",
            pr_number=10,
            summary="test",
            next_actions=[
                HandoffAction(
                    id="review",
                    label="Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "42", "role": "reviewer"},
                ),
            ],
        )

        block_error = {"error": "Insufficient memory: 0.5 GB available"}
        mock_start = AsyncMock()
        mock_clear = MagicMock()
        mock_write = MagicMock()

        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch(
                "sova.dashboard.services.agent_handoff.check_memory_pressure",
                return_value=(block_error, None),
            ),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.ipc.handoff.write_handoff_file", mock_write),
        ):
            await _process_auto_handoff(agent)

        # Should NOT have spawned the next agent
        mock_start.assert_not_awaited()
        # Should have cleared old handoff and written a blocked one
        mock_clear.assert_called_once()
        mock_write.assert_called_once()
        written_handoff = mock_write.call_args[0][1]
        assert written_handoff.source == "memory_guard"
        assert written_handoff.next_actions[0].auto_execute is False
        assert "(manual)" in written_handoff.next_actions[0].label


class TestPersistCompletingAgentHandoff:
    async def test_persists_when_handoff_json_empty(self) -> None:
        from sova.dashboard.services.agent_handoff import _persist_completing_agent_handoff
        from sova.ipc.handoff import DashboardHandoff

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="90", role="reviewer", status="done")
            session.add(run)
            await session.flush()
            run_id = run.id

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="90",
            summary="1 finding",
            details={"next_action": "address_review", "pending_findings": [{"severity": 9}]},
        )

        await _persist_completing_agent_handoff(run_id, handoff, Path("/tmp"))

        session2 = await get_session()
        async with session2.begin():
            refreshed = await session2.get(TaskRun, run_id)
            assert refreshed.handoff_json == handoff.details

    async def test_does_not_overwrite_existing_handoff_json(self) -> None:
        from sova.dashboard.services.agent_handoff import _persist_completing_agent_handoff
        from sova.ipc.handoff import DashboardHandoff

        existing = {"next_action": "approve", "pending_findings": []}
        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="91", role="reviewer", status="done", handoff_json=existing)
            session.add(run)
            await session.flush()
            run_id = run.id

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="91",
            summary="different",
            details={"next_action": "address_review", "pending_findings": [{"severity": 9}]},
        )

        await _persist_completing_agent_handoff(run_id, handoff, Path("/tmp"))

        session2 = await get_session()
        async with session2.begin():
            refreshed = await session2.get(TaskRun, run_id)
            assert refreshed.handoff_json == existing

    async def test_skips_when_details_empty(self) -> None:
        from sova.dashboard.services.agent_handoff import _persist_completing_agent_handoff
        from sova.ipc.handoff import DashboardHandoff

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="92", role="reviewer", status="done")
            session.add(run)
            await session.flush()
            run_id = run.id

        handoff = DashboardHandoff(source="reviewer", status="completed", issue="92", summary="clean")

        await _persist_completing_agent_handoff(run_id, handoff, Path("/tmp"))

        session2 = await get_session()
        async with session2.begin():
            refreshed = await session2.get(TaskRun, run_id)
            assert refreshed.handoff_json is None

    async def test_missing_run_does_not_raise(self) -> None:
        from sova.dashboard.services.agent_handoff import _persist_completing_agent_handoff
        from sova.ipc.handoff import DashboardHandoff

        handoff = DashboardHandoff(
            source="reviewer", status="awaiting_action", issue="93", summary="x", details={"a": 1}
        )

        await _persist_completing_agent_handoff(999999, handoff, Path("/tmp"))  # should not raise

    async def test_exception_is_swallowed(self, monkeypatch) -> None:
        from sova.dashboard.services.agent_handoff import _persist_completing_agent_handoff
        from sova.ipc.handoff import DashboardHandoff

        async def _boom(**kwargs):
            raise RuntimeError("db unavailable")

        monkeypatch.setattr("sova.db.session.get_session", _boom)

        handoff = DashboardHandoff(
            source="reviewer", status="awaiting_action", issue="94", summary="x", details={"a": 1}
        )

        await _persist_completing_agent_handoff(1, handoff, Path("/tmp"))  # should not raise
