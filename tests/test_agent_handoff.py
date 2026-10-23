"""Tests for sova.dashboard.services.agent_handoff.

The handoff file functions (``write_handoff_file``, ``read_handoff_file``,
``clear_handoff``) run for real against each test's ``tmp_path`` project
directory, so every handoff side effect is asserted on the file itself: whether
``.claude/agent-control/handoff-<issue>.json`` exists, what it contains, and
what ``clear_handoff()`` copied into ``handoff-archive/``. Only genuinely
external collaborators (agent spawning, GitHub verdict lookups, the host's
memory reading, feed and desktop notifications) are replaced.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from sova.dashboard.services.work_verdict import _NO_REVIEW
from sova.db.models import TaskRun
from sova.db.session import close_db, get_session, init_db
from sova.ipc.handoff import DashboardHandoff, handoff_filename, write_handoff_file
from tests.conftest import drain_feed_persist

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
    await drain_feed_persist()
    await close_db()


@pytest.fixture(autouse=True)
def _isolate_host_and_github() -> Iterator[None]:
    """Keep the host's free memory and GitHub out of every auto-handoff decision.

    The memory gate reads the real machine's available memory, and a "review"
    action looks up the PR's verdict on GitHub. Neither belongs in a unit test:
    a busy machine would block the handoff, and a reachable GitHub would answer
    for an unrelated PR. A test that exercises either path patches it again
    inside its own ``with`` block, which takes precedence over these defaults.
    """
    with (
        patch("sova.dashboard.services.agent_handoff.check_memory_pressure", return_value=(None, None)),
        patch("sova.dashboard.services.work_verdict.resolve_sova_verdict", return_value=dict(_NO_REVIEW)),
    ):
        yield


def _handoff_path(project_dir: Path, issue: str) -> Path:
    """Return the real per-issue handoff file path the production code reads and clears."""
    return project_dir / ".claude" / "agent-control" / handoff_filename(issue)


def _read_handoff_json(project_dir: Path, issue: str) -> dict | None:
    """Parse the per-issue handoff file, or None when it does not exist."""
    path = _handoff_path(project_dir, issue)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _archived_handoffs(project_dir: Path) -> list[dict]:
    """Parse every handoff ``clear_handoff()`` archived before removing it."""
    archive = project_dir / ".claude" / "agent-control" / "handoff-archive"
    if not archive.exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(archive.glob("*.json"))]


def _seed_misfiled(project_dir: Path, handoff: DashboardHandoff, filed_under_issue: str) -> Path:
    """Write ``handoff`` with the real writer, then move it to another issue's file name.

    The only way the real reader returns a handoff whose issue differs from the
    agent's own is a file whose content disagrees with its name, which is the
    case the issue-mismatch guard exists for.
    """
    written = write_handoff_file(project_dir, handoff)
    target = _handoff_path(project_dir, filed_under_issue)
    written.rename(target)
    return target


def _seed_bystander(project_dir: Path) -> DashboardHandoff:
    """Write another issue's handoff, which an issue-scoped clear must leave alone."""
    bystander = DashboardHandoff(source="developer", status="awaiting_action", issue="999", summary="other issue")
    write_handoff_file(project_dir, bystander)
    return bystander


def _assert_cleared(project_dir: Path, handoff: DashboardHandoff) -> None:
    """Assert ``handoff``'s file is gone and that exactly it was archived on the way out."""
    assert not _handoff_path(project_dir, handoff.issue).exists(), "handoff file must be cleared"
    assert [a["id"] for a in _archived_handoffs(project_dir)] == [handoff.id], "cleared handoff must be archived"


def _assert_untouched(project_dir: Path, handoff: DashboardHandoff, filed_under_issue: str | None = None) -> None:
    """Assert ``handoff``'s file is still on disk, byte-for-byte as written, and was never archived."""
    issue = filed_under_issue if filed_under_issue is not None else handoff.issue
    assert _read_handoff_json(project_dir, issue) == handoff.model_dump(), "handoff file must be left as written"
    assert _archived_handoffs(project_dir) == [], "handoff must not be archived"


class TestAutoHandoffIssueMismatch:
    async def test_skips_when_handoff_issue_mismatches(self, tmp_path: Path) -> None:
        """Auto-handoff should not execute if handoff is for a different issue."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 1, "issue": "114", "project_dir": tmp_path},
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
        _seed_misfiled(tmp_path, handoff, filed_under_issue="114")

        mock_start = AsyncMock()
        with patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start):
            await _process_auto_handoff(agent)

        mock_start.assert_not_awaited()
        _assert_untouched(tmp_path, handoff, filed_under_issue="114")

    async def test_does_not_persist_when_issue_mismatches_with_nonempty_details(self, tmp_path: Path) -> None:
        """When the handoff issue mismatches the agent issue, no persist must happen
        even if details is non-empty: writing another issue's verdict would corrupt
        the completing run's handoff_json."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import HandoffAction

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
        _seed_misfiled(tmp_path, handoff, filed_under_issue="200")

        agent = type("AgentState", (), {"run_id": run_id, "issue": "200", "project_dir": tmp_path})()

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock()
        with (
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_not_awaited()
        _assert_untouched(tmp_path, handoff, filed_under_issue="200")

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed is not None
                assert not refreshed.handoff_json, "mismatched handoff details must not be persisted"

    async def test_executes_when_handoff_issue_matches(self, tmp_path: Path) -> None:
        """Auto-handoff should execute normally when issues match, clearing only that issue's handoff."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 1, "issue": "113", "project_dir": tmp_path},
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
        write_handoff_file(tmp_path, handoff)
        bystander = _seed_bystander(tmp_path)

        mock_start = AsyncMock(return_value={"run_id": 2})
        with patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("113", role="reviewer", pr_number=130, slug=None)
        _assert_cleared(tmp_path, handoff)
        assert _read_handoff_json(tmp_path, "999") == bystander.model_dump(), "the clear must be scoped to issue 113"

    async def test_persists_handoff_details_to_completing_run(self, tmp_path: Path) -> None:
        """_process_auto_handoff must persist reviewer handoff details to TaskRun.handoff_json
        before clearing the file, so every other consumer of it finds the real verdict later."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import HandoffAction

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
        write_handoff_file(tmp_path, handoff)

        agent = type("AgentState", (), {"run_id": run_id, "issue": "120", "project_dir": tmp_path})()

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock(return_value={"run_id": 99})
        with (
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
            patch(
                "sova.config.loader.load_config",
                return_value=MagicMock(pipeline=MagicMock(max_address_review_cycles=0)),
            ),
        ):
            await _process_auto_handoff(agent)

        _assert_cleared(tmp_path, handoff)
        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed is not None
                assert refreshed.handoff_json is not None, "handoff_json must be persisted before file is cleared"
                assert refreshed.handoff_json.get("next_action") == "address_review"
                persisted_findings = refreshed.handoff_json.get("pending_findings", [])
                assert len(persisted_findings) == 1
                assert persisted_findings[0]["severity"] == 9

    async def test_skips_persist_when_handoff_json_already_set(self, tmp_path: Path) -> None:
        """_process_auto_handoff must not overwrite handoff_json if already populated
        (subprocess wrote it correctly to the right DB)."""
        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import HandoffAction

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
        write_handoff_file(tmp_path, handoff)

        agent = type("AgentState", (), {"run_id": run_id, "issue": "121", "project_dir": tmp_path})()

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with patch("sova.db.session.get_session", side_effect=_ignore_project_dir):
            await _process_auto_handoff(agent)

        # No auto_execute action: the manual Integrate handoff stays for the dashboard to render.
        _assert_untouched(tmp_path, handoff)
        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.handoff_json == existing_handoff, "pre-existing handoff_json must not be overwritten"


class TestAutoHandoffCircuitBreaker:
    async def test_blocks_after_max_cycles(self, tmp_path: Path) -> None:
        """Circuit breaker should block auto address-review after max cycles."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 10, "issue": "115", "project_dir": tmp_path},
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
        write_handoff_file(tmp_path, handoff)
        bystander = _seed_bystander(tmp_path)

        mock_start = AsyncMock()
        mock_emit = MagicMock()
        mock_notify = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 2
        with (
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.dashboard.services.work_verdict.resolve_sova_verdict",
                new_callable=AsyncMock,
                return_value={"has_sova_review": True, "verdict": "revise", "address_cycles": 2},
            ),
            patch("sova.dashboard.services.agent_handoff.emit_safe", mock_emit),
            patch("sova.dashboard.services.agent_handoff._notify_budget_exhausted", mock_notify),
        ):
            await _process_auto_handoff(agent)

        # Agent should NOT be spawned
        mock_start.assert_not_awaited()
        # No manual-only handoff is written anymore: resolve_next_action()'s
        # review_budget_exhausted rule resolves the over-budget PR to
        # PR_REVIEW_EXHAUSTED with an Integrate action on its own, so the
        # breaker only clears the stale handoff and reports the exhaustion.
        _assert_cleared(tmp_path, handoff)
        control_dir = tmp_path / ".claude" / "agent-control"
        assert sorted(p.name for p in control_dir.glob("handoff*.json")) == ["handoff-999.json"], (
            "the breaker must not write a replacement handoff, and must leave other issues' handoffs alone"
        )
        assert _read_handoff_json(tmp_path, "999") == bystander.model_dump()
        mock_emit.assert_called_once()
        assert "budget exhausted" in mock_emit.call_args[0][0]
        mock_notify.assert_called_once()
        assert mock_notify.call_args[0][1] == "115"

    async def test_allows_under_limit(self, tmp_path: Path) -> None:
        """Circuit breaker should allow address-review when under the limit."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 11, "issue": "116", "project_dir": tmp_path},
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
        write_handoff_file(tmp_path, handoff)

        mock_start = AsyncMock(return_value={"run_id": 12})
        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 2
        with (
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.dashboard.services.work_verdict.resolve_sova_verdict",
                new_callable=AsyncMock,
                return_value={"has_sova_review": True, "verdict": "revise", "address_cycles": 1},
            ),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("116", role="developer", pr_number=131, slug=None)
        _assert_cleared(tmp_path, handoff)

    async def test_blocks_when_cycle_count_is_unverifiable(self, tmp_path: Path) -> None:
        """An unreadable review history means an unknown count, never zero cycles."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_handoff import _check_address_review_circuit_breaker
        from sova.dashboard.services.work_verdict import UNRESOLVED_KEY

        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 3
        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.dashboard.services.work_verdict.resolve_sova_verdict",
                new_callable=AsyncMock,
                return_value={**_NO_REVIEW, UNRESOLVED_KEY: True},
            ),
        ):
            reason = await _check_address_review_circuit_breaker("118", 132, "developer", tmp_path)

        assert reason is not None
        assert "unverifiable" in reason

    async def test_skips_when_pr_number_is_none(self, tmp_path: Path) -> None:
        """Circuit breaker should not fire when pr_number is None."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 13, "issue": "117", "project_dir": tmp_path},
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
        write_handoff_file(tmp_path, handoff)

        mock_start = AsyncMock(return_value={"run_id": 14})
        with patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start):
            await _process_auto_handoff(agent)

        # Should proceed without checking circuit breaker
        mock_start.assert_awaited_once_with("117", role="developer", pr_number=None, slug=None)
        _assert_cleared(tmp_path, handoff)

    async def test_skips_for_non_developer_role(self, tmp_path: Path) -> None:
        """Circuit breaker should not fire for non-developer roles (e.g., reviewer)."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 15, "issue": "118", "project_dir": tmp_path},
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
        write_handoff_file(tmp_path, handoff)

        mock_start = AsyncMock(return_value={"run_id": 16})
        with patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("118", role="reviewer", pr_number=132, slug=None)
        _assert_cleared(tmp_path, handoff)

    async def test_zero_limit_disables_breaker(self, tmp_path: Path) -> None:
        """Setting max_address_review_cycles=0 should disable the circuit breaker."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 17, "issue": "119", "project_dir": tmp_path},
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
        write_handoff_file(tmp_path, handoff)

        mock_start = AsyncMock(return_value={"run_id": 18})
        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 0
        with (
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("119", role="developer", pr_number=133, slug=None)
        _assert_cleared(tmp_path, handoff)


class TestAutoHandoff:
    async def test_auto_handoff_spawns_agent(self, tmp_path: Path) -> None:
        """_process_auto_handoff should auto-spawn an agent for auto_execute actions."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = AgentState(
            run_id=1,
            issue="42",
            role="developer",
            process=MagicMock(),
            project_dir=tmp_path,
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
        write_handoff_file(tmp_path, handoff)

        with patch.object(
            agent_lifecycle, "start_agent", new_callable=AsyncMock, return_value={"status": "started"}
        ) as mock_start:
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("42", role="reviewer", pr_number=10, slug=None)
        _assert_cleared(tmp_path, handoff)

    async def test_auto_handoff_skips_non_auto_actions(self, tmp_path: Path) -> None:
        """_process_auto_handoff should not trigger actions without auto_execute."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = AgentState(
            run_id=1,
            issue="42",
            role="reviewer",
            process=MagicMock(),
            project_dir=tmp_path,
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
        write_handoff_file(tmp_path, handoff)

        with (
            patch.object(agent_lifecycle, "start_agent", new_callable=AsyncMock) as mock_agent,
            patch.object(agent_lifecycle, "start_command", new_callable=AsyncMock) as mock_cmd,
        ):
            await _process_auto_handoff(agent)

        mock_agent.assert_not_awaited()
        mock_cmd.assert_not_awaited()
        _assert_untouched(tmp_path, handoff)

    async def test_auto_handoff_claude_command(self, tmp_path: Path) -> None:
        """_process_auto_handoff should run claude-command actions with auto_execute."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = AgentState(
            run_id=1,
            issue="42",
            role="reviewer",
            process=MagicMock(),
            project_dir=tmp_path,
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
        write_handoff_file(tmp_path, handoff)

        with patch.object(
            agent_lifecycle, "start_command", new_callable=AsyncMock, return_value={"status": "started"}
        ) as mock_cmd:
            await _process_auto_handoff(agent)

        mock_cmd.assert_awaited_once_with("integrate-pr", {"issue": "42", "pr": 10}, slug=None)
        _assert_cleared(tmp_path, handoff)

    async def test_auto_handoff_invalid_pr_number_falls_back_to_none(self, tmp_path: Path) -> None:
        """When args['pr'] is non-numeric, int() raises ValueError.
        _process_auto_handoff must catch it, log a warning, and continue with pr_num=None."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = AgentState(run_id=5, issue="55", role="developer", process=MagicMock(), project_dir=tmp_path)

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
        write_handoff_file(tmp_path, handoff)

        with (
            patch.object(
                agent_lifecycle, "start_agent", new_callable=AsyncMock, return_value={"status": "started"}
            ) as mock_start,
            patch(
                "sova.config.loader.load_config",
                return_value=MagicMock(pipeline=MagicMock(max_address_review_cycles=0)),
            ),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("55", role="developer", pr_number=None, slug=None)
        _assert_cleared(tmp_path, handoff)

    async def test_auto_handoff_no_handoff_file(self, tmp_path: Path) -> None:
        """_process_auto_handoff should handle missing handoff gracefully."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff

        agent = AgentState(
            run_id=1,
            issue="42",
            role="developer",
            process=MagicMock(),
            project_dir=tmp_path,
        )

        with (
            patch.object(agent_lifecycle, "start_agent", new_callable=AsyncMock) as mock_agent,
            patch.object(agent_lifecycle, "start_command", new_callable=AsyncMock) as mock_cmd,
        ):
            await _process_auto_handoff(agent)  # Should not raise

        mock_agent.assert_not_awaited()
        mock_cmd.assert_not_awaited()
        assert not (tmp_path / ".claude").exists(), "no handoff file means nothing is written or archived"

    async def test_auto_handoff_skips_stale_review_when_already_reviewed(self, tmp_path: Path) -> None:
        """When a review action's PR already has a completed SOVA review (timing race:
        the developer wrote a "please review" handoff after the reviewer finished),
        _process_auto_handoff must not spawn a duplicate reviewer: it should clear
        the stale handoff and return instead. Regression coverage for PR #514."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = AgentState(run_id=9, issue="90", role="developer", process=MagicMock(), project_dir=tmp_path)

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
        write_handoff_file(tmp_path, handoff)
        bystander = _seed_bystander(tmp_path)

        with (
            patch(
                "sova.dashboard.services.work_verdict.resolve_sova_verdict",
                new_callable=AsyncMock,
                return_value={"has_sova_review": True, "verdict": "approve"},
            ),
            patch.object(agent_lifecycle, "start_agent", new_callable=AsyncMock) as mock_start,
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_not_awaited()
        _assert_cleared(tmp_path, handoff)
        assert _read_handoff_json(tmp_path, "999") == bystander.model_dump(), "the clear must be scoped to issue 90"

    async def test_auto_handoff_spawns_rereview_when_verdict_is_addressed(self, tmp_path: Path) -> None:
        """The address-review pipeline now ends by handing off to the Reviewer. At that
        point resolve_sova_verdict() reports "addressed" (a review exists, but an
        address cycle completed after it), which the stale-review guard must read as
        "re-review the new head", not as a duplicate review to suppress."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = AgentState(run_id=9, issue="90", role="developer", process=MagicMock(), project_dir=tmp_path)

        handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="90",
            pr_number=15,
            branch="feat/test",
            summary="Findings addressed, re-review",
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
        write_handoff_file(tmp_path, handoff)

        with (
            patch(
                "sova.dashboard.services.work_verdict.resolve_sova_verdict",
                new_callable=AsyncMock,
                return_value={"has_sova_review": True, "verdict": "addressed"},
            ),
            patch.object(
                agent_lifecycle, "start_agent", new_callable=AsyncMock, return_value={"status": "started"}
            ) as mock_start,
            patch(
                "sova.config.loader.load_config",
                return_value=MagicMock(pipeline=MagicMock(max_address_review_cycles=0)),
            ),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("90", role="reviewer", pr_number=15, slug=None)
        _assert_cleared(tmp_path, handoff)


class TestAutoHandoffMemoryGate:
    async def test_memory_block_stops_auto_handoff(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 1, "issue": "42", "project_dir": tmp_path},
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
        write_handoff_file(tmp_path, handoff)
        bystander = _seed_bystander(tmp_path)

        block_error = {"error": "Insufficient memory: 0.5 GB available"}
        mock_start = AsyncMock()

        with (
            patch(
                "sova.dashboard.services.agent_handoff.check_memory_pressure",
                return_value=(block_error, None),
            ),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
        ):
            await _process_auto_handoff(agent)

        # Should NOT have spawned the next agent
        mock_start.assert_not_awaited()
        # Should have cleared (archived) the old handoff, then written a blocked one in its place
        assert [a["id"] for a in _archived_handoffs(tmp_path)] == [handoff.id]
        written = _read_handoff_json(tmp_path, "42")
        assert written is not None, "a manual-only blocked handoff must replace the cleared one"
        assert written["id"] != handoff.id
        assert written["source"] == "memory_guard"
        assert written["status"] == "awaiting_action"
        assert written["issue"] == "42"
        assert written["pr_number"] == 10
        assert written["summary"] == block_error["error"]
        assert len(written["next_actions"]) == 1
        blocked_action = written["next_actions"][0]
        assert blocked_action["id"] == "review"
        assert blocked_action["auto_execute"] is False
        assert blocked_action["label"] == "Review (manual)"
        assert blocked_action["mode"] == "agent"
        assert blocked_action["args"] == {"issue": "42", "role": "reviewer"}
        assert _read_handoff_json(tmp_path, "999") == bystander.model_dump(), "the clear must be scoped to issue 42"


class TestPersistCompletingAgentHandoff:
    async def test_persists_when_handoff_json_empty(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_handoff import _persist_completing_agent_handoff

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

        await _persist_completing_agent_handoff(run_id, handoff, tmp_path)

        session2 = await get_session()
        async with session2.begin():
            refreshed = await session2.get(TaskRun, run_id)
            assert refreshed.handoff_json == handoff.details

    async def test_does_not_overwrite_existing_handoff_json(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_handoff import _persist_completing_agent_handoff

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

        await _persist_completing_agent_handoff(run_id, handoff, tmp_path)

        session2 = await get_session()
        async with session2.begin():
            refreshed = await session2.get(TaskRun, run_id)
            assert refreshed.handoff_json == existing

    async def test_skips_when_details_empty(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_handoff import _persist_completing_agent_handoff

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="92", role="reviewer", status="done")
            session.add(run)
            await session.flush()
            run_id = run.id

        handoff = DashboardHandoff(source="reviewer", status="completed", issue="92", summary="clean")

        await _persist_completing_agent_handoff(run_id, handoff, tmp_path)

        session2 = await get_session()
        async with session2.begin():
            refreshed = await session2.get(TaskRun, run_id)
            assert refreshed.handoff_json is None

    async def test_missing_run_does_not_raise(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_handoff import _persist_completing_agent_handoff

        handoff = DashboardHandoff(
            source="reviewer", status="awaiting_action", issue="93", summary="x", details={"a": 1}
        )

        await _persist_completing_agent_handoff(999999, handoff, tmp_path)  # should not raise

    async def test_exception_is_swallowed(self, monkeypatch, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_handoff import _persist_completing_agent_handoff

        async def _boom(**kwargs):
            raise RuntimeError("db unavailable")

        monkeypatch.setattr("sova.db.session.get_session", _boom)

        handoff = DashboardHandoff(
            source="reviewer", status="awaiting_action", issue="94", summary="x", details={"a": 1}
        )

        await _persist_completing_agent_handoff(1, handoff, tmp_path)  # should not raise
