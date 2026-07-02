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
    await init_db(run_migrations=False)
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

        h = DashboardHandoff(source="integrate-pr", status="awaiting_action", summary="Rebased and pushed.")
        assert h.source == "integrate-pr"
        assert h.status == "awaiting_action"
        assert h.id  # auto-generated UUID
        assert h.created_at  # auto-generated timestamp
        assert h.next_actions == []

    def test_create_with_actions(self) -> None:
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        h = DashboardHandoff(
            source="integrate-pr",
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

    def test_addressed_findings_round_trip(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, read_handoff_file, write_handoff_file

        findings = [
            {"source": "sonarcloud", "severity": "MAJOR", "file_path": "a.py", "tool_id": "S1", "message": "Issue"},
            {"source": "coderabbit", "severity": "HIGH", "file_path": "b.py", "tool_id": "", "message": "Bug"},
        ]
        h = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            summary="Done",
            issue="42",
            details={"addressed_findings": findings},
        )
        write_handoff_file(tmp_path, h)

        restored = read_handoff_file(tmp_path, issue="42")
        assert restored is not None
        assert len(restored.details["addressed_findings"]) == 2
        assert restored.details["addressed_findings"][0]["source"] == "sonarcloud"
        assert restored.details["addressed_findings"][1]["source"] == "coderabbit"

    def test_addressed_findings_round_trip_empty(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, read_handoff_file, write_handoff_file

        h = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            summary="Done",
            issue="43",
            details={"addressed_findings": []},
        )
        write_handoff_file(tmp_path, h)

        restored = read_handoff_file(tmp_path, issue="43")
        assert restored is not None
        assert restored.details["addressed_findings"] == []


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

    async def test_addressed_findings_round_trip(self) -> None:
        from sova.ipc.handoff import AgentHandoff, read_handoff, write_handoff

        session = await get_session()
        async with session.begin():
            tr = TaskRun(issue_number="42", role="developer", status="developing")
            session.add(tr)
            await session.flush()
            run_id = tr.id

        findings = [
            {"source": "sonarcloud", "severity": "MAJOR", "file_path": "a.py", "tool_id": "S1", "message": "Issue"},
        ]
        handoff = AgentHandoff(
            role="developer",
            phase="develop",
            summary="Done",
            next_action="review",
            branch_name="feat/42",
            addressed_findings=findings,
        )
        await write_handoff(run_id, handoff)

        restored = await read_handoff(run_id)
        assert restored is not None
        assert len(restored.addressed_findings) == 1
        assert restored.addressed_findings[0]["source"] == "sonarcloud"

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
    async def test_init_wraps_process(self) -> None:
        from sova.ipc.control import AgentProcess

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.returncode = None

        ap = AgentProcess(mock_proc)

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

        ap = AgentProcess(mock_proc)

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

        ap = AgentProcess(mock_proc)
        assert not ap.is_running

    async def test_wait_returns_exit_code(self) -> None:
        from sova.ipc.control import AgentProcess

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        ap = AgentProcess(mock_proc)

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

        ap = AgentProcess(mock_proc)

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

        ap = AgentProcess(mock_proc)

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

        ap = AgentProcess(mock_proc)

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

        ap = AgentProcess(mock_proc)

        assert ap.classify_exit(0) == ExitClassification.SUCCESS

    async def test_classify_error(self) -> None:
        from sova.ipc.control import AgentProcess, ExitClassification

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        ap = AgentProcess(mock_proc)

        assert ap.classify_exit(1) == ExitClassification.ERROR
        assert ap.classify_exit(127) == ExitClassification.ERROR

    async def test_classify_crash(self) -> None:
        from sova.ipc.control import AgentProcess, ExitClassification

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        ap = AgentProcess(mock_proc)

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

        ap = AgentProcess(mock_proc)

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

        ap = AgentProcess(mock_proc)

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

        ap = AgentProcess(mock_proc)

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

        ap = AgentProcess(mock_proc)

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
    async def test_send_desktop_macos_terminal_notifier(self) -> None:
        from sova.ipc.notifications import send_desktop_notification

        with patch("sova.ipc.notifications.sys") as mock_sys:
            mock_sys.platform = "darwin"
            with patch("sova.ipc.notifications.shutil") as mock_shutil:
                mock_shutil.which.return_value = "/opt/homebrew/bin/terminal-notifier"
                with patch("sova.ipc.notifications.run") as mock_run:
                    mock_run.return_value = MagicMock(success=True)
                    await send_desktop_notification(
                        "SOVA",
                        "Test body",
                        subtitle="Developer finished #42",
                        group="sova-42",
                    )

                    mock_run.assert_awaited_once()
                    call_args = mock_run.call_args[0]
                    assert "terminal-notifier" in call_args
                    assert "-subtitle" in call_args
                    assert "Developer finished #42" in call_args
                    assert "-group" in call_args
                    assert "sova-42" in call_args

    async def test_send_desktop_macos_jxa_fallback(self) -> None:
        from sova.ipc.notifications import send_desktop_notification

        with patch("sova.ipc.notifications.sys") as mock_sys:
            mock_sys.platform = "darwin"
            with patch("sova.ipc.notifications.shutil") as mock_shutil:
                mock_shutil.which.return_value = None
                with patch("sova.ipc.notifications.run") as mock_run:
                    mock_run.return_value = MagicMock(success=True)
                    await send_desktop_notification("SOVA", "Test body")

                    mock_run.assert_awaited_once()
                    call_args = mock_run.call_args[0]
                    assert "osascript" in call_args
                    assert "JavaScript" in call_args

    async def test_send_desktop_macos_jxa_escapes_special_chars(self) -> None:
        from sova.ipc.notifications import send_desktop_notification

        with patch("sova.ipc.notifications.sys") as mock_sys:
            mock_sys.platform = "darwin"
            with patch("sova.ipc.notifications.shutil") as mock_shutil:
                mock_shutil.which.return_value = None
                with patch("sova.ipc.notifications.run") as mock_run:
                    mock_run.return_value = MagicMock(success=True)
                    await send_desktop_notification('Title with "quotes"', 'Body with \\ and "quotes"')

                    mock_run.assert_awaited_once()
                    script_arg = mock_run.call_args[0][-1]
                    assert "displayNotification" in script_arg
                    assert '\\"quotes\\"' in script_arg

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
            mock_desktop.assert_awaited_once_with("Title", "Body", subtitle="", group="")

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

        async def slow_desktop(title: str, message: str, **_: str) -> None:
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


# ---------------------------------------------------------------------------
# AgentRuntime
# ---------------------------------------------------------------------------


class TestAgentRuntimeABC:
    def test_claude_code_runtime_name(self) -> None:
        from sova.ipc.runtime import ClaudeCodeRuntime

        rt = ClaudeCodeRuntime()
        assert rt.name == "claude-code"

    def test_aider_runtime_name(self) -> None:
        from sova.ipc.runtime import AiderRuntime

        rt = AiderRuntime()
        assert rt.name == "aider"

    def test_create_runtime_claude_code(self) -> None:
        from sova.ipc.runtime import ClaudeCodeRuntime, create_runtime

        rt = create_runtime("claude-code")
        assert isinstance(rt, ClaudeCodeRuntime)

    def test_create_runtime_aider(self) -> None:
        from sova.ipc.runtime import AiderRuntime, create_runtime

        rt = create_runtime("aider")
        assert isinstance(rt, AiderRuntime)

    def test_create_runtime_unknown_raises(self) -> None:
        from sova.ipc.runtime import create_runtime

        with pytest.raises(ValueError, match="Unknown agent runtime"):
            create_runtime("nonexistent")

    def test_get_set_runtime(self) -> None:
        from sova.ipc.runtime import AiderRuntime, ClaudeCodeRuntime, get_runtime, set_runtime

        # Default is ClaudeCodeRuntime
        set_runtime(ClaudeCodeRuntime())
        assert isinstance(get_runtime(), ClaudeCodeRuntime)

        # Switch to Aider
        set_runtime(AiderRuntime())
        assert isinstance(get_runtime(), AiderRuntime)

        # Reset for other tests
        set_runtime(ClaudeCodeRuntime())


class TestClaudeCodeRuntime:
    async def test_spawn_delegates_to_agent_process(self) -> None:
        from sova.ipc.runtime import ClaudeCodeRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = ClaudeCodeRuntime()
        with patch("sova.ipc.control.asyncio.create_subprocess_exec", return_value=mock_proc):
            ap = await rt.spawn("test prompt", Path("/tmp"))

        assert ap.pid == 42

    def test_parse_output_empty(self) -> None:
        from sova.ipc.runtime import ClaudeCodeRuntime

        rt = ClaudeCodeRuntime()
        assert rt.parse_output("") is None
        assert rt.parse_output("   ") is None

    def test_parse_output_plain_text(self) -> None:
        from sova.ipc.runtime import ClaudeCodeRuntime

        rt = ClaudeCodeRuntime()
        event = rt.parse_output("not json")
        assert event is not None
        assert event.type == "content"
        assert event.text == "not json"

    def test_parse_output_assistant_event(self) -> None:
        import json

        from sova.ipc.runtime import ClaudeCodeRuntime

        rt = ClaudeCodeRuntime()
        line = json.dumps({"type": "assistant", "content": [{"type": "text", "text": "Hello"}]})
        event = rt.parse_output(line)
        assert event is not None
        assert event.type == "content"
        assert event.text == "Hello"

    def test_parse_output_result_event(self) -> None:
        import json

        from sova.ipc.runtime import ClaudeCodeRuntime

        rt = ClaudeCodeRuntime()
        line = json.dumps({"type": "result", "result": "done"})
        event = rt.parse_output(line)
        assert event is not None
        assert event.type == "result"

    async def test_check_available_found(self) -> None:
        from sova.ipc.runtime import ClaudeCodeRuntime

        rt = ClaudeCodeRuntime()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"1.0.0\n", b""))
        mock_proc.returncode = 0

        with (
            patch("sova.ipc.runtime.shutil.which", return_value="/usr/bin/claude"),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            ok, detail = await rt.check_available()

        assert ok is True
        assert "1.0.0" in detail

    async def test_check_available_not_found(self) -> None:
        from sova.ipc.runtime import ClaudeCodeRuntime

        rt = ClaudeCodeRuntime()
        with patch("sova.ipc.runtime.shutil.which", return_value=None):
            ok, detail = await rt.check_available()

        assert ok is False
        assert "not found" in detail

    async def test_spawn_with_model(self, tmp_path: Path) -> None:
        from sova.ipc.runtime import ClaudeCodeRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 100
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        runtime = ClaudeCodeRuntime()
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            ap = await runtime.spawn("test", tmp_path, model="sonnet")

        assert ap.pid == 100
        args = mock_exec.call_args[0]
        assert "--model" in args
        model_idx = args.index("--model")
        assert args[model_idx + 1] == "sonnet"

    async def test_spawn_with_max_budget(self, tmp_path: Path) -> None:
        from decimal import Decimal

        from sova.ipc.runtime import ClaudeCodeRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 101
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        runtime = ClaudeCodeRuntime()
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            ap = await runtime.spawn("test", tmp_path, max_budget_usd=Decimal("5.00"))

        assert ap.pid == 101
        args = mock_exec.call_args[0]
        assert "--max-budget-usd" in args
        budget_idx = args.index("--max-budget-usd")
        assert args[budget_idx + 1] == "5.00"

    async def test_spawn_prepends_headless_preamble(self, tmp_path: Path) -> None:
        from sova.ipc.runtime import _HEADLESS_PREAMBLE, ClaudeCodeRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 102
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        runtime = ClaudeCodeRuntime()
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await runtime.spawn("my prompt", tmp_path)

        args = mock_exec.call_args[0]
        # -p flag value should be preamble + prompt
        p_idx = args.index("-p")
        assert args[p_idx + 1] == _HEADLESS_PREAMBLE + "my prompt"

    async def test_spawn_includes_required_cli_flags(self, tmp_path: Path) -> None:
        from sova.ipc.runtime import ClaudeCodeRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 103
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        runtime = ClaudeCodeRuntime()
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await runtime.spawn("test", tmp_path)

        args = mock_exec.call_args[0]
        # Verify required flags for headless operation
        assert "--output-format" in args
        fmt_idx = args.index("--output-format")
        assert args[fmt_idx + 1] == "stream-json"
        assert "--verbose" in args
        assert "--permission-mode" in args
        pm_idx = args.index("--permission-mode")
        assert args[pm_idx + 1] == "auto"


class TestAiderRuntime:
    async def test_spawn_builds_correct_args(self) -> None:
        from sova.ipc.runtime import AiderRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 99
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = AiderRuntime()
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            ap = await rt.spawn("fix bug", Path("/tmp"), model="gpt-4o")

        assert ap.pid == 99
        call_args = mock_exec.call_args[0]
        assert call_args[0] == "aider"
        assert "--message" in call_args
        msg_idx = list(call_args).index("--message")
        assert call_args[msg_idx + 1] == "fix bug"
        assert "--model" in call_args
        model_idx = list(call_args).index("--model")
        assert call_args[model_idx + 1] == "gpt-4o"

    def test_parse_output_plain_text(self) -> None:
        from sova.ipc.runtime import AiderRuntime

        rt = AiderRuntime()
        event = rt.parse_output("editing file.py")
        assert event is not None
        assert event.type == "content"
        assert event.text == "editing file.py"

    def test_parse_output_empty(self) -> None:
        from sova.ipc.runtime import AiderRuntime

        rt = AiderRuntime()
        assert rt.parse_output("") is None

    async def test_check_available_found(self) -> None:
        from sova.ipc.runtime import AiderRuntime

        rt = AiderRuntime()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"aider v0.50.0\n", b""))
        mock_proc.returncode = 0

        with (
            patch("sova.ipc.runtime.shutil.which", return_value="/usr/bin/aider"),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            ok, detail = await rt.check_available()

        assert ok is True
        assert "0.50.0" in detail

    async def test_check_available_not_found(self) -> None:
        from sova.ipc.runtime import AiderRuntime

        rt = AiderRuntime()
        with patch("sova.ipc.runtime.shutil.which", return_value=None):
            ok, detail = await rt.check_available()

        assert ok is False
        assert "not found" in detail

    def test_transform_prompt_plain_text(self) -> None:
        """Plain task descriptions pass through unchanged."""
        from sova.ipc.runtime import AiderRuntime

        rt = AiderRuntime()
        assert rt.transform_prompt("fix the bug in file.py") == "fix the bug in file.py"

    def test_transform_prompt_shell_command(self) -> None:
        """Shell-command-formatted prompts are extracted to the sova command."""
        from sova.ipc.runtime import AiderRuntime

        rt = AiderRuntime()
        prompt = "Run the following command:\n```bash\nsova run 28 --run-id 161\n```"
        result = rt.transform_prompt(prompt)
        assert result == "sova run 28 --run-id 161"

    def test_transform_prompt_non_sova_shell(self) -> None:
        """Non-sova shell commands pass through unchanged."""
        from sova.ipc.runtime import AiderRuntime

        rt = AiderRuntime()
        prompt = "Run:\n```bash\nls -la\n```"
        assert rt.transform_prompt(prompt) == prompt

    async def test_spawn_shell_prompt_executes_directly(self) -> None:
        """Shell-command prompts with sova should be executed as subprocess."""
        from sova.ipc.runtime import AiderRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = AiderRuntime()
        prompt = "Run the following command:\n```bash\nsova run 28\n```"
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            ap = await rt.spawn(prompt, Path("/tmp"))

        assert ap.pid == 42
        call_args = mock_exec.call_args[0]
        assert call_args[0] == "sova"
        assert "run" in call_args
        assert "28" in call_args

    async def test_spawn_with_budget_logs_warning(self) -> None:
        from decimal import Decimal

        from sova.ipc.runtime import AiderRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 99
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = AiderRuntime()
        with (
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sova.ipc.runtime.log") as mock_log,
        ):
            ap = await rt.spawn("fix bug", Path("/tmp"), max_budget_usd=Decimal("5.00"))

        assert ap.pid == 99
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args[0][0] == "aider.budget_not_enforced"


class TestCheckCliAvailable:
    async def test_version_check_exception(self) -> None:
        from sova.ipc.runtime import _check_cli_available

        with (
            patch("sova.ipc.runtime.shutil.which", return_value="/usr/bin/tool"),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", side_effect=OSError("boom")),
        ):
            ok, detail = await _check_cli_available("tool", "install hint")

        assert ok is False
        assert "error checking version" in detail

    async def test_version_check_nonzero_exit(self) -> None:
        """Non-zero exit from --version should report unavailable."""
        from sova.ipc.runtime import _check_cli_available

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error\n"))
        mock_proc.returncode = 1

        with (
            patch("sova.ipc.runtime.shutil.which", return_value="/usr/bin/tool"),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            ok, detail = await _check_cli_available("tool", "install hint")

        assert ok is False
        assert "exited with code 1" in detail

    async def test_version_check_timeout(self) -> None:
        """Hanging --version check should timeout gracefully."""
        from sova.ipc.runtime import _check_cli_available

        with (
            patch("sova.ipc.runtime.shutil.which", return_value="/usr/bin/tool"),
            patch(
                "sova.ipc.runtime.asyncio.wait_for",
                side_effect=asyncio.TimeoutError,
            ),
        ):
            ok, detail = await _check_cli_available("tool", "install hint")

        assert ok is False
        assert "timed out" in detail


class TestClaudeCodeParseEdgeCases:
    def test_parse_output_assistant_string_content(self) -> None:
        import json

        from sova.ipc.runtime import ClaudeCodeRuntime

        rt = ClaudeCodeRuntime()
        line = json.dumps({"type": "assistant", "content": "plain string"})
        event = rt.parse_output(line)
        assert event is not None
        assert event.type == "content"
        assert event.text == "plain string"

    def test_parse_output_assistant_other_content_type(self) -> None:
        import json

        from sova.ipc.runtime import ClaudeCodeRuntime

        rt = ClaudeCodeRuntime()
        line = json.dumps({"type": "assistant", "content": 42})
        event = rt.parse_output(line)
        assert event is not None
        assert event.type == "content"
        assert event.text == "42"

    def test_parse_output_unknown_event_type(self) -> None:
        import json

        from sova.ipc.runtime import ClaudeCodeRuntime

        rt = ClaudeCodeRuntime()
        line = json.dumps({"type": "system", "data": "info"})
        assert rt.parse_output(line) is None

    def test_parse_output_non_dict_json(self) -> None:
        """JSON array should not crash parse_output (AttributeError on .get())."""
        import json

        from sova.ipc.runtime import ClaudeCodeRuntime

        rt = ClaudeCodeRuntime()
        line = json.dumps([1, 2, 3])
        event = rt.parse_output(line)
        assert event is not None
        assert event.type == "content"
        assert event.text == line

    def test_parse_output_result_populates_llm_result(self) -> None:
        """Result events should populate StreamEvent.result with LLMResult."""
        import json

        from sova.ipc.runtime import ClaudeCodeRuntime

        rt = ClaudeCodeRuntime()
        line = json.dumps(
            {
                "type": "result",
                "result": "done",
                "total_cost_usd": 0.05,
                "model": "opus",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "session_id": "abc123",
            }
        )
        event = rt.parse_output(line)
        assert event is not None
        assert event.type == "result"
        assert event.text == "done"
        assert event.result is not None
        assert event.result.model == "opus"
        assert event.result.input_tokens == 100
        assert event.result.output_tokens == 50
        assert event.result.session_id == "abc123"

    def test_get_runtime_default(self) -> None:
        import sova.ipc.runtime as rt_mod
        from sova.ipc.runtime import ClaudeCodeRuntime, get_runtime

        # Force the default path by clearing the singleton
        original = rt_mod._runtime
        try:
            rt_mod._runtime = None
            result = get_runtime()
            assert isinstance(result, ClaudeCodeRuntime)
        finally:
            rt_mod._runtime = original


# ---------------------------------------------------------------------------
# MockAgentProcess
# ---------------------------------------------------------------------------


class TestMockAgentProcess:
    from sova.ipc.control import ExitClassification
    from sova.ipc.testing import MockAgentProcess

    @pytest.mark.asyncio
    async def test_immediate_success(self) -> None:
        proc = self.MockAgentProcess(exit_code=0)
        assert proc.is_running
        assert proc.returncode is None
        assert proc.pid == 99999

        code = await proc.wait()
        assert code == 0
        assert not proc.is_running
        assert proc.returncode == 0

    @pytest.mark.asyncio
    async def test_error_exit(self) -> None:
        proc = self.MockAgentProcess(exit_code=1, stderr_lines_data=["error: boom"])
        code, classification = await proc.wait_classified()
        assert code == 1
        assert classification == self.ExitClassification.ERROR

    @pytest.mark.asyncio
    async def test_crash_exit(self) -> None:
        proc = self.MockAgentProcess(exit_code=130)
        code, classification = await proc.wait_classified()
        assert code == 130
        assert classification == self.ExitClassification.CRASH

    @pytest.mark.asyncio
    async def test_stdout_lines(self) -> None:
        proc = self.MockAgentProcess(stdout_lines_data=["line1", "line2", "line3"])
        lines = [line async for line in proc.stdout_lines()]
        assert lines == ["line1", "line2", "line3"]

    @pytest.mark.asyncio
    async def test_stderr_lines(self) -> None:
        proc = self.MockAgentProcess(stderr_lines_data=["err1"])
        lines = [line async for line in proc.stderr_lines()]
        assert lines == ["err1"]

    @pytest.mark.asyncio
    async def test_empty_stdout(self) -> None:
        proc = self.MockAgentProcess()
        lines = [line async for line in proc.stdout_lines()]
        assert lines == []

    @pytest.mark.asyncio
    async def test_hang_and_stop(self) -> None:
        proc = self.MockAgentProcess(should_hang=True, exit_code=42)
        assert proc.is_running

        # wait() should block, so we stop from another task
        async def stopper() -> None:
            await asyncio.sleep(0.01)
            await proc.stop()

        stopper_task = asyncio.create_task(stopper())
        try:
            code = await proc.wait()
        finally:
            await stopper_task
        assert code == 42
        assert not proc.is_running

    @pytest.mark.asyncio
    async def test_wait_after_stop(self) -> None:
        proc = self.MockAgentProcess(exit_code=0)
        await proc.stop()
        # wait after stop should return immediately
        code = await proc.wait()
        assert code == 0

    @pytest.mark.asyncio
    async def test_stop_interrupts_delayed_wait(self) -> None:
        proc = self.MockAgentProcess(duration_seconds=10.0, exit_code=7)

        async def stopper() -> None:
            await asyncio.sleep(0.02)
            await proc.stop()

        stopper_task = asyncio.create_task(stopper())
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=2.0)
        finally:
            await stopper_task
        assert code == 7
        assert not proc.is_running

    @pytest.mark.asyncio
    async def test_delayed_completion(self) -> None:
        proc = self.MockAgentProcess(duration_seconds=0.01, exit_code=0)
        code = await proc.wait()
        assert code == 0

    def test_classify_exit_static(self) -> None:
        assert self.MockAgentProcess.classify_exit(0) == self.ExitClassification.SUCCESS
        assert self.MockAgentProcess.classify_exit(1) == self.ExitClassification.ERROR
        assert self.MockAgentProcess.classify_exit(127) == self.ExitClassification.ERROR
        assert self.MockAgentProcess.classify_exit(128) == self.ExitClassification.CRASH
        assert self.MockAgentProcess.classify_exit(137) == self.ExitClassification.CRASH

    @pytest.mark.asyncio
    async def test_custom_pid(self) -> None:
        proc = self.MockAgentProcess(pid=12345)
        assert proc.pid == 12345


# ---------------------------------------------------------------------------
# MockRuntime
# ---------------------------------------------------------------------------


class TestMockRuntime:
    from sova.ipc.runtime import create_runtime
    from sova.ipc.testing import MockRuntime

    @pytest.mark.asyncio
    async def test_spawn_and_track(self) -> None:
        rt = self.MockRuntime(stdout_lines=["hello"], exit_code=0)
        assert rt.name == "mock"
        assert rt.last_prompt is None
        assert rt.spawned_processes == []

        proc = await rt.spawn("do stuff", "/tmp")
        assert rt.last_prompt == "do stuff"
        assert len(rt.spawned_processes) == 1
        assert rt.spawned_processes[0] is proc

        lines = [line async for line in proc.stdout_lines()]
        assert lines == ["hello"]
        code = await proc.wait()
        assert code == 0

    @pytest.mark.asyncio
    async def test_multiple_spawns(self) -> None:
        rt = self.MockRuntime()
        await rt.spawn("first", "/tmp")
        await rt.spawn("second", "/tmp")
        assert len(rt.spawned_processes) == 2
        assert rt.last_prompt == "second"

    def test_parse_output(self) -> None:
        rt = self.MockRuntime()
        event = rt.parse_output("hello world")
        assert event is not None
        assert event.type == "content"
        assert event.text == "hello world"

    def test_parse_output_empty(self) -> None:
        rt = self.MockRuntime()
        assert rt.parse_output("") is None
        assert rt.parse_output("   ") is None

    @pytest.mark.asyncio
    async def test_check_available(self) -> None:
        rt = self.MockRuntime()
        available, detail = await rt.check_available()
        assert available is True
        assert "mock-runtime" in detail

    @pytest.mark.asyncio
    async def test_factory_create(self) -> None:
        rt = TestMockRuntime.create_runtime("mock")
        assert isinstance(rt, self.MockRuntime)
        assert rt.name == "mock"

    @pytest.mark.asyncio
    async def test_spawn_copies_stdout_lines(self) -> None:
        """Each spawn gets its own copy of stdout lines."""
        rt = self.MockRuntime(stdout_lines=["a", "b"])
        p1 = await rt.spawn("first", "/tmp")
        p2 = await rt.spawn("second", "/tmp")

        lines1 = [line async for line in p1.stdout_lines()]
        lines2 = [line async for line in p2.stdout_lines()]
        assert lines1 == ["a", "b"]
        assert lines2 == ["a", "b"]
