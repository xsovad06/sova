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
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from sova.dashboard.services.work_verdict import _NO_REVIEW, UNRESOLVED_KEY
from sova.db.models import TaskRun
from sova.db.session import get_session
from sova.ipc.handoff import DashboardHandoff, handoff_filename, write_handoff_file

# agent_handoff.py imports `get_session` and `load_config` at module scope, so a test
# patches them in its namespace ("sova.dashboard.services.agent_handoff.get_session"),
# never at their definition modules, where the patch would not reach this code.


# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


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
            {"run_id": 1, "issue": "114", "project_dir": tmp_path, "pr_number": None},
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

        agent = type("AgentState", (), {"run_id": run_id, "issue": "200", "project_dir": tmp_path, "pr_number": None})()

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock()
        with (
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.agent_handoff.get_session", side_effect=_ignore_project_dir),
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
            {"run_id": 1, "issue": "113", "project_dir": tmp_path, "pr_number": None},
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

        mock_start.assert_awaited_once_with("113", role="reviewer", pr_number=130, slug=None, _skip_handoff_clear=True)
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

        agent = type("AgentState", (), {"run_id": run_id, "issue": "120", "project_dir": tmp_path, "pr_number": None})()

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock(return_value={"run_id": 99})
        with (
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.agent_handoff.get_session", side_effect=_ignore_project_dir),
            patch(
                "sova.dashboard.services.agent_handoff.load_config",
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

        agent = type("AgentState", (), {"run_id": run_id, "issue": "121", "project_dir": tmp_path, "pr_number": None})()

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with patch("sova.dashboard.services.agent_handoff.get_session", side_effect=_ignore_project_dir):
            await _process_auto_handoff(agent)

        # No auto_execute action: the manual Integrate handoff stays for the dashboard to render.
        _assert_untouched(tmp_path, handoff)
        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.handoff_json == existing_handoff, "pre-existing handoff_json must not be overwritten"


class TestAutoHandoffCircuitBreaker:
    @pytest.mark.parametrize(
        ("verdict", "headline"),
        [
            ({"has_sova_review": True, "verdict": "revise", "address_cycles": 2}, "address-review budget exhausted"),
            # An unreadable history is a hold for the user, and the alert must not call it exhausted.
            ({**_NO_REVIEW, UNRESOLVED_KEY: True}, "address-review budget unverifiable"),
        ],
        ids=["exhausted", "unverifiable"],
    )
    async def test_blocks_after_max_cycles(self, tmp_path: Path, verdict: dict, headline: str) -> None:
        """Circuit breaker should block auto address-review after max cycles, or on an unknown count."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 10, "issue": "115", "project_dir": tmp_path, "pr_number": None},
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
            patch("sova.dashboard.services.agent_handoff.load_config", return_value=mock_cfg),
            patch(
                "sova.dashboard.services.work_verdict.resolve_sova_verdict",
                new_callable=AsyncMock,
                return_value=verdict,
            ),
            patch("sova.dashboard.services.agent_handoff.emit_safe", mock_emit),
            patch("sova.dashboard.services.agent_handoff._notify_budget_exhausted", mock_notify),
        ):
            await _process_auto_handoff(agent)

        # Agent should NOT be spawned
        mock_start.assert_not_awaited()
        # No manual-only handoff is written anymore: resolve_next_action() resolves
        # the over-budget PR to PR_REVIEW_EXHAUSTED with Integrate, and an unreadable
        # history to a user hold, on its own, so the breaker only clears the stale
        # handoff and reports why it stopped.
        _assert_cleared(tmp_path, handoff)
        control_dir = tmp_path / ".claude" / "agent-control"
        assert sorted(p.name for p in control_dir.glob("handoff*.json")) == ["handoff-999.json"], (
            "the breaker must not write a replacement handoff, and must leave other issues' handoffs alone"
        )
        assert _read_handoff_json(tmp_path, "999") == bystander.model_dump()
        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == f"#115: {headline}"
        mock_notify.assert_called_once()
        assert mock_notify.call_args[0][1] == "115"
        assert mock_notify.call_args.kwargs["headline"] == headline

    async def test_allows_under_limit(self, tmp_path: Path) -> None:
        """Circuit breaker should allow address-review when under the limit."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 11, "issue": "116", "project_dir": tmp_path, "pr_number": None},
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
            patch("sova.dashboard.services.agent_handoff.load_config", return_value=mock_cfg),
            patch(
                "sova.dashboard.services.work_verdict.resolve_sova_verdict",
                new_callable=AsyncMock,
                return_value={"has_sova_review": True, "verdict": "revise", "address_cycles": 1},
            ),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("116", role="developer", pr_number=131, slug=None, _skip_handoff_clear=True)
        _assert_cleared(tmp_path, handoff)

    async def test_blocks_when_cycle_count_is_unverifiable(self, tmp_path: Path) -> None:
        """An unreadable review history means an unknown count, never zero cycles."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_handoff import _check_address_review_circuit_breaker
        from sova.dashboard.services.work_verdict import UNRESOLVED_KEY

        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 3
        with (
            patch("sova.dashboard.services.agent_handoff.load_config", return_value=mock_cfg),
            patch(
                "sova.dashboard.services.work_verdict.resolve_sova_verdict",
                new_callable=AsyncMock,
                return_value={**_NO_REVIEW, UNRESOLVED_KEY: True},
            ),
        ):
            reason = await _check_address_review_circuit_breaker("118", 132, "developer", tmp_path)

        assert reason is not None
        assert "unverifiable" in reason

    async def test_truncated_history_reads_as_exhausted_not_an_outage(self, tmp_path: Path) -> None:
        """A history past the window never clears on a retry, so the reason must not blame GitHub."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_handoff import _check_address_review_circuit_breaker
        from sova.dashboard.services.work_verdict import TRUNCATED_KEY, UNRESOLVED_KEY

        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 3
        with (
            patch("sova.dashboard.services.agent_handoff.load_config", return_value=mock_cfg),
            patch(
                "sova.dashboard.services.work_verdict.resolve_sova_verdict",
                new_callable=AsyncMock,
                return_value={**_NO_REVIEW, UNRESOLVED_KEY: True, TRUNCATED_KEY: True},
            ),
        ):
            reason = await _check_address_review_circuit_breaker("118", 132, "developer", tmp_path)

        assert reason is not None
        assert "exhausted" in reason
        assert "unverifiable" not in reason
        assert "GitHub" not in reason

    async def test_skips_when_pr_number_is_none(self, tmp_path: Path) -> None:
        """Circuit breaker should not fire when pr_number is None."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 13, "issue": "117", "project_dir": tmp_path, "pr_number": None},
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
        mock_start.assert_awaited_once_with(
            "117", role="developer", pr_number=None, slug=None, _skip_handoff_clear=True
        )
        _assert_cleared(tmp_path, handoff)

    async def test_skips_for_non_developer_role(self, tmp_path: Path) -> None:
        """Circuit breaker should not fire for non-developer roles (e.g., reviewer)."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 15, "issue": "118", "project_dir": tmp_path, "pr_number": None},
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

        mock_start.assert_awaited_once_with("118", role="reviewer", pr_number=132, slug=None, _skip_handoff_clear=True)
        _assert_cleared(tmp_path, handoff)

    async def test_zero_limit_disables_breaker(self, tmp_path: Path) -> None:
        """Setting max_address_review_cycles=0 should disable the circuit breaker."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 17, "issue": "119", "project_dir": tmp_path, "pr_number": None},
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
            patch("sova.dashboard.services.agent_handoff.load_config", return_value=mock_cfg),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("119", role="developer", pr_number=133, slug=None, _skip_handoff_clear=True)
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

        mock_start.assert_awaited_once_with("42", role="reviewer", pr_number=10, slug=None, _skip_handoff_clear=True)
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

        mock_cmd.assert_awaited_once_with(
            "integrate-pr", {"issue": "42", "pr": 10}, slug=None, _skip_handoff_clear=True
        )
        _assert_cleared(tmp_path, handoff)

    async def test_auto_handoff_claude_command_without_command_is_skipped(self, tmp_path: Path) -> None:
        """An auto-execute claude-command action with no command starts nothing and raises nothing."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle, handoff_service
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        assert handoff_service.command_name(None) == ""
        agent = AgentState(run_id=1, issue="42", role="reviewer", process=MagicMock(), project_dir=tmp_path)
        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="42",
            pr_number=10,
            summary="Clean review",
            next_actions=[
                HandoffAction(
                    id="next",
                    label="Next step",
                    mode="claude-command",
                    command="",
                    args={"issue": "42", "pr": 10},
                    auto_execute=True,
                ),
            ],
        )
        write_handoff_file(tmp_path, handoff)

        with (
            patch.object(agent_lifecycle, "start_command", new_callable=AsyncMock) as mock_cmd,
            capture_logs() as logs,
        ):
            await _process_auto_handoff(agent)

        mock_cmd.assert_not_awaited()
        assert not any(e["log_level"] == "error" for e in logs)

    async def test_auto_handoff_unknown_command_is_refused(self, project_agents, fake_runtime) -> None:
        """The real start_command() refuses a command outside the catalog, and the refusal is logged as one."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        project_dir = project_agents.project_dir
        agent = AgentState(run_id=1, issue="42", role="reviewer", process=MagicMock(), project_dir=project_dir)
        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="42",
            pr_number=10,
            summary="Clean review",
            next_actions=[
                HandoffAction(
                    id="next",
                    label="Next step",
                    mode="claude-command",
                    command="/not-a-real-command 10",
                    args={"issue": "42", "pr": 10},
                    auto_execute=True,
                ),
            ],
        )
        write_handoff_file(project_dir, handoff)

        with capture_logs() as logs:
            await _process_auto_handoff(agent)

        assert fake_runtime.spawn_calls == []
        refused = [e for e in logs if e["event"] == "auto_handoff.start_refused"]
        assert [(e["log_level"], e["action_id"], e["result"]["code"]) for e in refused] == [
            ("warning", "next", "unknown_command")
        ]
        assert not any(e["event"] == "auto_handoff.command_started" for e in logs)

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
                "sova.dashboard.services.agent_handoff.load_config",
                return_value=MagicMock(pipeline=MagicMock(max_address_review_cycles=0)),
            ),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("55", role="developer", pr_number=None, slug=None, _skip_handoff_clear=True)
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
                "sova.dashboard.services.work_verdict.load_config",
                return_value=MagicMock(pipeline=MagicMock(max_address_review_cycles=0)),
            ),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("90", role="reviewer", pr_number=15, slug=None, _skip_handoff_clear=True)
        _assert_cleared(tmp_path, handoff)


class TestAutoHandoffMemoryGate:
    async def test_memory_block_stops_auto_handoff(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 1, "issue": "42", "project_dir": tmp_path, "pr_number": None},
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

        with capture_logs() as logs:
            await _persist_completing_agent_handoff(999999, handoff, tmp_path)

        assert [e["event"] for e in logs] == []

    async def test_exception_is_swallowed(self, monkeypatch, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_handoff import _persist_completing_agent_handoff

        async def _boom(**kwargs):
            raise RuntimeError("db unavailable")

        monkeypatch.setattr("sova.dashboard.services.agent_handoff.get_session", _boom)

        handoff = DashboardHandoff(
            source="reviewer", status="awaiting_action", issue="94", summary="x", details={"a": 1}
        )

        with capture_logs() as logs:
            await _persist_completing_agent_handoff(1, handoff, tmp_path)

        assert [e["event"] for e in logs if e["log_level"] == "warning"] == ["auto_handoff.handoff_json_persist_failed"]


class TestFollowUpSlug:
    """``_slug_for_project()`` picks the follow-up's pool from the finished agent's project (issue #1569)."""

    @pytest.mark.parametrize(
        ("pools", "registered", "expected"),
        [
            # A single-project server's default pool wins even when its directory is also registered.
            ({"__default__": "proj"}, {"sova": "proj"}, "__default__"),
            # No live pool: the registry names the project.
            ({}, {"other": "elsewhere", "sova": "proj"}, "sova"),
            # Neither: start_agent() keeps its default resolution.
            ({}, {}, None),
        ],
    )
    def test_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pools: dict, registered: dict, expected: str | None
    ) -> None:
        from sova.dashboard.services import agent_pool
        from sova.dashboard.services.agent_handoff import _slug_for_project
        from sova.dashboard.services.agent_pool import ProjectAgents

        (tmp_path / "proj").mkdir()
        monkeypatch.setattr(
            agent_pool, "_projects", {s: ProjectAgents(project_dir=tmp_path / d) for s, d in pools.items()}
        )
        monkeypatch.setattr("sova.config.registry._load", lambda: {s: str(tmp_path / d) for s, d in registered.items()})

        assert _slug_for_project(tmp_path / "proj") == expected

    def test_the_owning_pool_wins_over_a_pool_sharing_its_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On a hub, ``__default__`` and the primary project's slug share a directory: the run's own pool is used."""
        from sova.dashboard.services import agent_pool
        from sova.dashboard.services.agent_handoff import _slug_for_project
        from sova.dashboard.services.agent_pool import ProjectAgents

        first, owner = ProjectAgents(project_dir=tmp_path), ProjectAgents(project_dir=tmp_path)
        monkeypatch.setattr(agent_pool, "_projects", {"__default__": first, "sova": owner})

        assert _slug_for_project(tmp_path, owner) == "sova"
        assert _slug_for_project(tmp_path, first) == "__default__"

    def test_a_pool_no_longer_listed_falls_back_to_the_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.dashboard.services import agent_pool
        from sova.dashboard.services.agent_handoff import _slug_for_project
        from sova.dashboard.services.agent_pool import ProjectAgents

        monkeypatch.setattr(agent_pool, "_projects", {"sova": ProjectAgents(project_dir=tmp_path)})

        assert _slug_for_project(tmp_path, ProjectAgents(project_dir=tmp_path)) == "sova"

    async def test_single_project_follow_up_lands_in_the_pool_http_starts_use(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single-project mode (no slug in context): the follow-up joins the default pool even when the directory
        is also registered, so ``max_parallel`` is counted in one pool."""
        from unittest.mock import AsyncMock

        from sova.config.context import clear_project_context
        from sova.dashboard.services import agent_pool
        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.dashboard.services.agent_pool import AgentState, _get_project_agents

        monkeypatch.setattr(agent_pool, "_projects", {})
        monkeypatch.setattr(agent_pool, "_default_project_dir", tmp_path)
        monkeypatch.setattr("sova.config.registry._load", lambda: {"sova": str(tmp_path)})
        monkeypatch.setattr(agent_pool, "read_max_parallel", lambda _d: 2)
        clear_project_context()
        default_pool = _get_project_agents()  # what start_agent() resolves for an HTTP request in this mode
        write_handoff_file(tmp_path, _auto_review_handoff())
        agent = AgentState(run_id=5, issue="42", role="developer", process=MagicMock(), project_dir=tmp_path)

        start = AsyncMock(return_value={"status": "started"})
        with (
            patch("sova.dashboard.services.agent_lifecycle.start_agent", start),
            patch("sova.dashboard.services.agent_handoff.check_memory_pressure", return_value=(None, None)),
            patch(
                "sova.dashboard.services.agent_handoff._check_address_review_circuit_breaker",
                AsyncMock(return_value=None),
            ),
            patch(
                "sova.dashboard.services.work_verdict.resolve_verdict_for_pr",
                AsyncMock(return_value={"has_sova_review": False}),
            ),
        ):
            await _process_auto_handoff(agent, default_pool)

        start.assert_awaited_once()
        assert _get_project_agents(start.await_args.kwargs["slug"]) is default_pool

    def test_no_project_dir_is_none(self) -> None:
        from sova.dashboard.services.agent_handoff import _slug_for_project

        assert _slug_for_project(None) is None


class TestIssueLessAutoHandoff:
    async def test_a_pr_only_run_clears_only_its_own_handoff(self, tmp_path: Path) -> None:
        """A PR-only run has no issue; its clear must not take the other issues' pending handoffs with it."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services import handoff_service
        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type("AgentState", (), {"run_id": None, "issue": "", "pr_number": 7, "project_dir": tmp_path})()
        pr_only = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            pr_number=7,
            summary="pr only",
            next_actions=[HandoffAction(id="go", label="Go", mode="agent", args={"pr": 7}, auto_execute=True)],
        )
        write_handoff_file(tmp_path, pr_only)
        others = [_seed_bystander(tmp_path)]
        others.append(DashboardHandoff(source="developer", status="awaiting_action", issue="998", summary="b"))
        write_handoff_file(tmp_path, others[1])
        # The issue-less file must be the newest, the one an issue-less read picks.
        legacy = tmp_path / ".claude" / "agent-control" / "handoff.json"
        os.utime(legacy, (legacy.stat().st_atime, legacy.stat().st_mtime + 10))

        with patch("sova.dashboard.services.agent_lifecycle.start_agent", AsyncMock(return_value={})) as start:
            await _process_auto_handoff(agent)

        start.assert_awaited_once()
        assert not legacy.exists()
        assert sorted(h["issue"] for h in handoff_service.get_all_handoffs(tmp_path)) == ["998", "999"]

    async def test_a_pr_only_run_ignores_a_newer_issue_handoff(self, tmp_path: Path) -> None:
        """A newer issue-scoped handoff is neither run nor cleared by a PR-only run; its own older one still runs."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type("AgentState", (), {"run_id": None, "issue": "", "pr_number": 7, "project_dir": tmp_path})()
        pr_only = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            pr_number=7,
            summary="pr only",
            next_actions=[HandoffAction(id="go", label="Go", mode="agent", args={"pr": 7}, auto_execute=True)],
        )
        write_handoff_file(tmp_path, pr_only)
        other = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="999",
            summary="other issue",
            next_actions=[
                HandoffAction(id="other", label="Other", mode="agent", args={"issue": "999"}, auto_execute=True)
            ],
        )
        other_path = write_handoff_file(tmp_path, other)
        os.utime(other_path, (other_path.stat().st_atime, other_path.stat().st_mtime + 10))

        with patch("sova.dashboard.services.agent_lifecycle.start_agent", AsyncMock(return_value={})) as start:
            await _process_auto_handoff(agent)

        start.assert_awaited_once()
        assert start.await_args.kwargs["pr_number"] == 7
        assert not (tmp_path / ".claude" / "agent-control" / "handoff.json").exists()
        assert _read_handoff_json(tmp_path, "999") == other.model_dump()

    async def test_a_pr_only_run_ignores_another_prs_handoff(self, tmp_path: Path) -> None:
        """Every PR-only handoff shares handoff.json: PR 7's run must not run or clear the one PR 9's run wrote."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type("AgentState", (), {"run_id": None, "issue": "", "pr_number": 7, "project_dir": tmp_path})()
        other_pr = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            pr_number=9,
            summary="another PR",
            next_actions=[HandoffAction(id="go", label="Go", mode="agent", args={"pr": 9}, auto_execute=True)],
        )
        write_handoff_file(tmp_path, other_pr)

        with patch("sova.dashboard.services.agent_lifecycle.start_agent", AsyncMock(return_value={})) as start:
            await _process_auto_handoff(agent)

        start.assert_not_awaited()
        assert (tmp_path / ".claude" / "agent-control" / "handoff.json").exists()

    async def test_a_pr_only_run_with_only_an_issue_handoff_does_nothing(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        agent = type("AgentState", (), {"run_id": None, "issue": "", "pr_number": 7, "project_dir": tmp_path})()
        other = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="999",
            summary="other issue",
            next_actions=[
                HandoffAction(id="other", label="Other", mode="agent", args={"issue": "999"}, auto_execute=True)
            ],
        )
        write_handoff_file(tmp_path, other)

        with patch("sova.dashboard.services.agent_lifecycle.start_agent", AsyncMock(return_value={})) as start:
            await _process_auto_handoff(agent)

        start.assert_not_awaited()
        _assert_untouched(tmp_path, other)


def _auto_review_handoff(issue: str = "42") -> DashboardHandoff:
    from sova.ipc.handoff import HandoffAction

    return DashboardHandoff(
        source="developer",
        status="awaiting_action",
        issue=issue,
        pr_number=10,
        summary="ready for review",
        next_actions=[
            HandoffAction(
                id="handoff",
                label="Review",
                mode="agent",
                args={"issue": issue, "pr": 10, "role": "reviewer"},
                auto_execute=True,
            )
        ],
    )


class TestAutoHandoffClearsBeforeStart:
    """The auto path clears its trigger before the follow-up starts (spec decision 4, issue #1569)."""

    async def test_the_trigger_is_gone_when_the_follow_up_starts(self, tmp_path: Path) -> None:
        """The follow-up must not read the trigger, and the dashboard must not offer it as a button meanwhile."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff

        write_handoff_file(tmp_path, _auto_review_handoff())
        agent = type("AgentState", (), {"run_id": None, "issue": "42", "pr_number": None, "project_dir": tmp_path})()
        seen: list[bool] = []

        async def _start(*_args: object, **_kwargs: object) -> dict:
            seen.append((tmp_path / ".claude" / "agent-control" / "handoff-42.json").exists())
            return {"status": "started"}

        with patch("sova.dashboard.services.agent_lifecycle.start_agent", AsyncMock(side_effect=_start)):
            await _process_auto_handoff(agent)

        assert seen == [False]

    @pytest.mark.parametrize(
        "result",
        [
            {"error": "Maximum concurrent agents reached (2)", "code": "no_free_slot"},
            {"error": "Failed to spawn agent process: boom", "code": "spawn_failed"},
            {"status": "error", "detail": "refused"},
        ],
    )
    async def test_an_unsuccessful_start_is_logged(self, tmp_path: Path, result: dict) -> None:
        """A refused auto start leaves no pending handoff (the accepted trade-off), so it is logged as a warning."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff

        write_handoff_file(tmp_path, _auto_review_handoff())
        agent = type("AgentState", (), {"run_id": None, "issue": "42", "pr_number": None, "project_dir": tmp_path})()

        with (
            patch("sova.dashboard.services.agent_lifecycle.start_agent", AsyncMock(return_value=result)) as start,
            capture_logs() as logs,
        ):
            await _process_auto_handoff(agent)

        start.assert_awaited_once()
        assert not (tmp_path / ".claude" / "agent-control" / "handoff-42.json").exists()
        assert any(e["event"] == "auto_handoff.start_refused" for e in logs)

    async def test_a_replacement_written_during_the_start_stays_pending(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff

        handoff = _auto_review_handoff()
        write_handoff_file(tmp_path, handoff)
        replacement = DashboardHandoff(source="reviewer", status="awaiting_action", issue="42", summary="newer")
        agent = type("AgentState", (), {"run_id": None, "issue": "42", "pr_number": None, "project_dir": tmp_path})()

        async def _start(*_args: object, **_kwargs: object) -> dict:
            write_handoff_file(tmp_path, replacement)
            return {"status": "started"}

        with patch("sova.dashboard.services.agent_lifecycle.start_agent", AsyncMock(side_effect=_start)):
            await _process_auto_handoff(agent)

        # The read version was archived before the start; the replacement is left as written.
        assert _read_handoff_json(tmp_path, "42") == replacement.model_dump()

    async def test_an_empty_command_still_clears_the_handoff(self, tmp_path: Path) -> None:
        """A claude-command action naming no command starts nothing, and its handoff is cleared as before."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import HandoffAction

        handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="42",
            summary="nothing to run",
            next_actions=[HandoffAction(id="noop", label="Noop", mode="claude-command", command="", auto_execute=True)],
        )
        write_handoff_file(tmp_path, handoff)
        agent = type("AgentState", (), {"run_id": None, "issue": "42", "project_dir": tmp_path, "pr_number": None})()

        with patch("sova.dashboard.services.agent_lifecycle.start_command", AsyncMock()) as start:
            await _process_auto_handoff(agent)

        start.assert_not_awaited()
        _assert_cleared(tmp_path, handoff)
