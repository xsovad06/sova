"""Tests for sova.ipc -- handoff protocol, process management, notifications."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.config.models import NotificationConfig
from sova.db.models import TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for IPC tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db()
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


# ---------------------------------------------------------------------------
# AgentHandoff model
# ---------------------------------------------------------------------------


class TestHandoffAction:
    def test_create_action(self) -> None:
        from sova.ipc.handoff import HandoffAction

        a = HandoffAction(
            id="merge",
            label="Merge PR",
            description="Squash-merge into main",
            style="approve",
            mode="claude-command",
            command="approve-merge",
            args={"pr": 15},
        )
        assert a.id == "merge"
        assert a.style == "approve"
        assert a.mode == "claude-command"
        assert a.args == {"pr": 15}

    def test_defaults(self) -> None:
        from sova.ipc.handoff import HandoffAction

        a = HandoffAction(id="wait", label="Wait")
        assert a.style == "neutral"
        assert a.mode == "claude-command"
        assert a.command == ""
        assert a.args == {}

    def test_serialize_roundtrip(self) -> None:
        from sova.ipc.handoff import HandoffAction

        a = HandoffAction(id="abort", label="Abort", style="danger", mode="shell", command="rm handoff.json")
        data = a.model_dump()
        restored = HandoffAction.model_validate(data)
        assert restored == a


class TestDashboardHandoff:
    def test_create_minimal(self) -> None:
        from sova.ipc.handoff import DashboardHandoff

        h = DashboardHandoff(source="ship-pr", status="awaiting_action", summary="Rebased and pushed.")
        assert h.source == "ship-pr"
        assert h.status == "awaiting_action"
        assert h.id  # auto-generated UUID
        assert h.created_at  # auto-generated timestamp
        assert h.next_actions == []

    def test_create_with_actions(self) -> None:
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        h = DashboardHandoff(
            source="ship-pr",
            status="awaiting_action",
            issue="#42",
            pr_number=15,
            branch="feat/my-feature",
            summary="Rebased and pushed. CI pending.",
            details={"actions_taken": ["Rebased onto main"], "ci_status": "pending"},
            next_actions=[
                HandoffAction(id="merge", label="Merge PR", style="approve", command="approve-merge"),
                HandoffAction(id="abort", label="Abort", style="danger", mode="shell", command="rm handoff.json"),
            ],
        )
        assert h.pr_number == 15
        assert len(h.next_actions) == 2
        assert h.next_actions[0].style == "approve"

    def test_serialize_roundtrip(self) -> None:
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        h = DashboardHandoff(
            source="test",
            status="completed",
            summary="All done",
            next_actions=[HandoffAction(id="ok", label="OK")],
        )
        json_str = h.model_dump_json()
        restored = DashboardHandoff.model_validate_json(json_str)
        assert restored.source == h.source
        assert len(restored.next_actions) == 1


class TestHandoffFile:
    def test_write_and_read(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, read_handoff_file, write_handoff_file

        h = DashboardHandoff(source="test", status="awaiting_action", summary="Test handoff")
        path = write_handoff_file(tmp_path, h)
        assert path.exists()

        restored = read_handoff_file(tmp_path)
        assert restored is not None
        assert restored.source == "test"
        assert restored.summary == "Test handoff"

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import read_handoff_file

        assert read_handoff_file(tmp_path) is None

    def test_read_invalid_json_returns_none(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import read_handoff_file

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff.json").write_text("not valid json{{{")

        assert read_handoff_file(tmp_path) is None

    def test_write_creates_directory(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        h = DashboardHandoff(source="test", status="completed", summary="Done")
        path = write_handoff_file(tmp_path, h)
        assert path.parent.exists()


class TestAgentHandoff:
    def test_create_minimal(self) -> None:
        from sova.ipc.handoff import AgentHandoff

        h = AgentHandoff(
            role="developer",
            phase="development",
            summary="Implemented feature X",
            next_action="await_review",
            branch_name="feat/x",
        )
        assert h.role == "developer"
        assert h.needs_human is False
        assert h.pending_findings == []

    def test_create_full(self) -> None:
        from sova.ipc.handoff import AgentHandoff

        h = AgentHandoff(
            role="reviewer",
            phase="review",
            summary="Found 3 issues",
            key_decisions=["Used adapter pattern"],
            files_changed=["src/main.py"],
            tests_added=["tests/test_main.py"],
            next_action="address_findings",
            pending_findings=[{"file": "main.py", "line": 10, "issue": "missing null check"}],
            blockers=[],
            needs_human=True,
            human_message="Please clarify requirement #3",
            pr_number=42,
            branch_name="feat/x",
            commit_shas=["abc123"],
        )
        assert h.needs_human is True
        assert h.pr_number == 42
        assert len(h.pending_findings) == 1

    def test_serialize_to_dict(self) -> None:
        from sova.ipc.handoff import AgentHandoff

        h = AgentHandoff(
            role="developer",
            phase="development",
            summary="Done",
            next_action="await_review",
            branch_name="feat/x",
        )
        d = h.model_dump()
        assert isinstance(d, dict)
        assert d["role"] == "developer"

    def test_roundtrip_json(self) -> None:
        from sova.ipc.handoff import AgentHandoff

        h = AgentHandoff(
            role="developer",
            phase="development",
            summary="Done",
            next_action="await_review",
            branch_name="feat/x",
            commit_shas=["abc", "def"],
        )
        json_str = h.model_dump_json()
        restored = AgentHandoff.model_validate_json(json_str)
        assert restored == h

    def test_from_dict(self) -> None:
        from sova.ipc.handoff import AgentHandoff

        data = {
            "role": "reviewer",
            "phase": "review",
            "summary": "Reviewed",
            "next_action": "address_findings",
            "branch_name": "feat/y",
        }
        h = AgentHandoff.model_validate(data)
        assert h.role == "reviewer"


class TestHandoffDB:
    async def test_write_handoff_to_task_run(self) -> None:
        from sova.ipc.handoff import AgentHandoff, write_handoff

        # Create a TaskRun first
        session = await get_session()
        async with session.begin():
            tr = TaskRun(issue_number="42", role="developer", status="developing")
            session.add(tr)
            await session.flush()
            run_id = tr.id

        handoff = AgentHandoff(
            role="developer",
            phase="development",
            summary="Built the feature",
            next_action="await_review",
            branch_name="feat/42",
        )

        await write_handoff(run_id, handoff)

        # Verify it was persisted
        session = await get_session()
        async with session.begin():
            tr = await session.get(TaskRun, run_id)
            assert tr.handoff_json is not None
            assert tr.handoff_json["role"] == "developer"
            assert tr.handoff_json["summary"] == "Built the feature"

    async def test_read_handoff_from_task_run(self) -> None:
        from sova.ipc.handoff import AgentHandoff, read_handoff, write_handoff

        session = await get_session()
        async with session.begin():
            tr = TaskRun(issue_number="42", role="developer", status="developing")
            session.add(tr)
            await session.flush()
            run_id = tr.id

        handoff = AgentHandoff(
            role="developer",
            phase="development",
            summary="Built the feature",
            next_action="await_review",
            branch_name="feat/42",
            commit_shas=["abc123"],
        )
        await write_handoff(run_id, handoff)

        restored = await read_handoff(run_id)
        assert restored is not None
        assert restored.role == "developer"
        assert restored.commit_shas == ["abc123"]

    async def test_read_handoff_returns_none_when_empty(self) -> None:
        from sova.ipc.handoff import read_handoff

        session = await get_session()
        async with session.begin():
            tr = TaskRun(issue_number="42", role="developer", status="developing")
            session.add(tr)
            await session.flush()
            run_id = tr.id

        result = await read_handoff(run_id)
        assert result is None

    async def test_read_handoff_returns_none_for_missing_run(self) -> None:
        from sova.ipc.handoff import read_handoff

        result = await read_handoff(99999)
        assert result is None

    async def test_write_overwrites_previous_handoff(self) -> None:
        from sova.ipc.handoff import AgentHandoff, read_handoff, write_handoff

        session = await get_session()
        async with session.begin():
            tr = TaskRun(issue_number="42", role="developer", status="developing")
            session.add(tr)
            await session.flush()
            run_id = tr.id

        h1 = AgentHandoff(
            role="developer", phase="development", summary="First", next_action="await_review", branch_name="feat/42"
        )
        await write_handoff(run_id, h1)

        h2 = AgentHandoff(
            role="reviewer", phase="review", summary="Second", next_action="address_findings", branch_name="feat/42"
        )
        await write_handoff(run_id, h2)

        restored = await read_handoff(run_id)
        assert restored.role == "reviewer"
        assert restored.summary == "Second"


# ---------------------------------------------------------------------------
# Process Manager
# ---------------------------------------------------------------------------


class TestAgentProcess:
    async def test_spawn_agent_creates_process(self) -> None:
        from sova.ipc.control import AgentProcess

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.returncode = None

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await AgentProcess.spawn(
                prompt="Write tests",
                cwd=Path("/tmp/test"),
            )

        assert ap.pid == 12345
        assert ap.is_running

    async def test_stop_kills_process(self) -> None:
        from sova.ipc.control import AgentProcess

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await AgentProcess.spawn(prompt="test", cwd=Path("/tmp"))

        mock_proc.returncode = None
        await ap.stop()

        mock_proc.terminate.assert_called_once()

    async def test_is_running_false_when_exited(self) -> None:
        from sova.ipc.control import AgentProcess

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.returncode = 0
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await AgentProcess.spawn(prompt="test", cwd=Path("/tmp"))

        mock_proc.returncode = 0
        assert not ap.is_running

    async def test_spawn_with_model(self) -> None:
        from sova.ipc.control import AgentProcess

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await AgentProcess.spawn(
                prompt="test",
                cwd=Path("/tmp"),
                model="sonnet",
            )

        call_args = mock_exec.call_args
        args = call_args[0]
        assert "--model" in args
        model_idx = args.index("--model")
        assert args[model_idx + 1] == "sonnet"

    async def test_wait_returns_exit_code(self) -> None:
        from sova.ipc.control import AgentProcess

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await AgentProcess.spawn(prompt="test", cwd=Path("/tmp"))

        mock_proc.returncode = 0
        code = await ap.wait()
        assert code == 0

    async def test_read_stdout_line(self) -> None:
        from sova.ipc.control import AgentProcess

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=[b"line 1\n", b"line 2\n", b""])
        mock_proc.stderr = AsyncMock()

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await AgentProcess.spawn(prompt="test", cwd=Path("/tmp"))

        lines = []
        async for line in ap.stdout_lines():
            lines.append(line)

        assert lines == ["line 1", "line 2"]

    async def test_read_stderr_lines(self) -> None:
        from sova.ipc.control import AgentProcess

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.readline = AsyncMock(side_effect=[b"err 1\n", b"err 2\n", b""])

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await AgentProcess.spawn(prompt="test", cwd=Path("/tmp"))

        lines = []
        async for line in ap.stderr_lines():
            lines.append(line)

        assert lines == ["err 1", "err 2"]

    async def test_stderr_lines_none_stream(self) -> None:
        from sova.ipc.control import AgentProcess

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = None

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await AgentProcess.spawn(prompt="test", cwd=Path("/tmp"))

        lines = []
        async for line in ap.stderr_lines():
            lines.append(line)

        assert lines == []


class TestExitClassification:
    async def test_classify_success(self) -> None:
        from sova.ipc.control import AgentProcess, ExitClassification

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await AgentProcess.spawn(prompt="test", cwd=Path("/tmp"))

        assert ap.classify_exit(0) == ExitClassification.SUCCESS

    async def test_classify_error(self) -> None:
        from sova.ipc.control import AgentProcess, ExitClassification

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await AgentProcess.spawn(prompt="test", cwd=Path("/tmp"))

        assert ap.classify_exit(1) == ExitClassification.ERROR
        assert ap.classify_exit(127) == ExitClassification.ERROR

    async def test_classify_crash(self) -> None:
        from sova.ipc.control import AgentProcess, ExitClassification

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await AgentProcess.spawn(prompt="test", cwd=Path("/tmp"))

        assert ap.classify_exit(128) == ExitClassification.CRASH
        assert ap.classify_exit(137) == ExitClassification.CRASH  # SIGKILL
        assert ap.classify_exit(139) == ExitClassification.CRASH  # SIGSEGV

    async def test_wait_classified(self) -> None:
        from sova.ipc.control import AgentProcess, ExitClassification

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await AgentProcess.spawn(prompt="test", cwd=Path("/tmp"))

        mock_proc.returncode = 0
        code, classification = await ap.wait_classified()
        assert code == 0
        assert classification == ExitClassification.SUCCESS

    async def test_wait_classified_crash(self) -> None:
        from sova.ipc.control import AgentProcess, ExitClassification

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=137)

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await AgentProcess.spawn(prompt="test", cwd=Path("/tmp"))

        mock_proc.returncode = 137
        code, classification = await ap.wait_classified()
        assert code == 137
        assert classification == ExitClassification.CRASH


class TestMarkCrashed:
    async def test_mark_crashed_updates_task_run(self) -> None:
        from sova.ipc.control import mark_crashed

        session = await get_session()
        async with session.begin():
            tr = TaskRun(issue_number="99", role="developer", status="developing")
            session.add(tr)
            await session.flush()
            run_id = tr.id

        session = await get_session()
        await mark_crashed(run_id, "Process killed by SIGKILL (exit 137)", session)

        session = await get_session()
        async with session.begin():
            tr = await session.get(TaskRun, run_id)
            assert tr.status == "failed"
            assert tr.error_message == "Process killed by SIGKILL (exit 137)"
            assert tr.ended_at is not None

    async def test_mark_crashed_missing_run(self) -> None:
        from sova.ipc.control import mark_crashed

        session = await get_session()
        # Should not raise
        await mark_crashed(99999, "crash", session)


class TestProcessTracker:
    async def test_track_and_get(self) -> None:
        from sova.ipc.control import AgentProcess, ProcessTracker

        tracker = ProcessTracker()

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await AgentProcess.spawn(prompt="test", cwd=Path("/tmp"))

        tracker.register(task_run_id=1, process=ap)
        assert tracker.get(1) is ap
        assert tracker.get(999) is None

    async def test_unregister(self) -> None:
        from sova.ipc.control import AgentProcess, ProcessTracker

        tracker = ProcessTracker()

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await AgentProcess.spawn(prompt="test", cwd=Path("/tmp"))

        tracker.register(task_run_id=1, process=ap)
        tracker.unregister(1)
        assert tracker.get(1) is None

    def test_list_active(self) -> None:
        from sova.ipc.control import ProcessTracker

        tracker = ProcessTracker()

        proc1 = MagicMock()
        proc1.is_running = True
        proc1.pid = 100

        proc2 = MagicMock()
        proc2.is_running = False
        proc2.pid = 200

        tracker.register(1, proc1)
        tracker.register(2, proc2)

        active = tracker.list_active()
        assert len(active) == 1
        assert active[0][0] == 1


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class TestDesktopNotification:
    async def test_send_desktop_macos(self) -> None:
        from sova.ipc.notifications import send_desktop_notification

        with patch("sova.ipc.notifications.sys") as mock_sys:
            mock_sys.platform = "darwin"
            with patch("sova.ipc.notifications.run") as mock_run:
                mock_run.return_value = MagicMock(success=True)
                await send_desktop_notification("Test Title", "Test body")

                mock_run.assert_awaited_once()
                call_args = mock_run.call_args[0]
                assert "osascript" in call_args

    async def test_send_desktop_linux(self) -> None:
        from sova.ipc.notifications import send_desktop_notification

        with patch("sova.ipc.notifications.sys") as mock_sys:
            mock_sys.platform = "linux"
            with patch("sova.ipc.notifications.run") as mock_run:
                mock_run.return_value = MagicMock(success=True)
                await send_desktop_notification("Test Title", "Test body")

                mock_run.assert_awaited_once()
                call_args = mock_run.call_args[0]
                assert "notify-send" in call_args

    async def test_unsupported_platform_no_error(self) -> None:
        from sova.ipc.notifications import send_desktop_notification

        with patch("sova.ipc.notifications.sys") as mock_sys:
            mock_sys.platform = "win32"
            with patch("sova.ipc.notifications.run") as mock_run:
                await send_desktop_notification("Title", "Body")
                mock_run.assert_not_awaited()


class TestSlackNotification:
    async def test_send_slack(self) -> None:
        from sova.ipc.notifications import send_slack_notification

        with patch("sova.ipc.notifications.run") as mock_run:
            mock_run.return_value = MagicMock(success=True)
            await send_slack_notification(
                webhook_url="https://hooks.slack.com/services/T/B/x",
                title="Agent needs help",
                message="Please review PR #42",
            )

            mock_run.assert_awaited_once()
            call_args = mock_run.call_args[0]
            assert "curl" in call_args

    async def test_send_slack_empty_url_skips(self) -> None:
        from sova.ipc.notifications import send_slack_notification

        with patch("sova.ipc.notifications.run") as mock_run:
            await send_slack_notification(webhook_url="", title="Test", message="Body")
            mock_run.assert_not_awaited()


class TestNotify:
    async def test_notify_sends_desktop_when_enabled(self) -> None:
        from sova.ipc.notifications import notify

        config = NotificationConfig(desktop=True, slack_webhook_url="")
        with patch("sova.ipc.notifications.send_desktop_notification") as mock_desktop:
            mock_desktop.return_value = None
            notify(config, "Title", "Body")
            # Let the background task run
            await asyncio.sleep(0)
            mock_desktop.assert_awaited_once_with("Title", "Body")

    async def test_notify_sends_slack_when_configured(self) -> None:
        from sova.ipc.notifications import notify

        config = NotificationConfig(desktop=False, slack_webhook_url="https://hooks.slack.com/x")
        with patch("sova.ipc.notifications.send_slack_notification") as mock_slack:
            mock_slack.return_value = None
            notify(config, "Title", "Body")
            await asyncio.sleep(0)
            mock_slack.assert_awaited_once()

    async def test_notify_skips_all_when_disabled(self) -> None:
        from sova.ipc.notifications import notify

        config = NotificationConfig(desktop=False, slack_webhook_url="")
        with (
            patch("sova.ipc.notifications.send_desktop_notification") as mock_desktop,
            patch("sova.ipc.notifications.send_slack_notification") as mock_slack,
        ):
            notify(config, "Title", "Body")
            await asyncio.sleep(0)
            mock_desktop.assert_not_awaited()
            mock_slack.assert_not_awaited()

    async def test_notify_error_does_not_raise(self) -> None:
        from sova.ipc.notifications import notify

        config = NotificationConfig(desktop=True)
        with patch("sova.ipc.notifications.send_desktop_notification", side_effect=Exception("boom")):
            # Should not raise -- fire-and-forget
            notify(config, "Title", "Body")
            await asyncio.sleep(0)

    async def test_notify_is_fire_and_forget(self) -> None:
        """Verify notify() returns immediately without awaiting delivery."""
        from sova.ipc.notifications import notify

        call_order: list[str] = []

        async def slow_desktop(title: str, message: str) -> None:
            await asyncio.sleep(0.05)
            call_order.append("desktop_done")

        config = NotificationConfig(desktop=True, slack_webhook_url="")
        with patch("sova.ipc.notifications.send_desktop_notification", side_effect=slow_desktop):
            notify(config, "Title", "Body")
            call_order.append("notify_returned")
            # notify returned before desktop finished
            assert call_order == ["notify_returned"]
            # Wait for background task to complete
            await asyncio.sleep(0.1)
            assert call_order == ["notify_returned", "desktop_done"]
