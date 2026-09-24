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
from sova.utils.shell import ShellResult


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


class TestEmailNotification:
    async def test_send_email_notification_success(self) -> None:
        from sova.ipc.notifications import send_email_notification

        config = NotificationConfig(
            email_enabled=True,
            email_to="dev@example.com",
            email_from="sova@example.com",
            email_smtp_host="smtp.example.com",
            email_smtp_port=587,
            email_smtp_starttls=True,
            email_smtp_user="user@example.com",
            email_smtp_password="secret",
        )

        with patch("sova.ipc.notifications.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

            await send_email_notification(config, "SOVA Alert", "Developer finished #42")

            mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=30)
            mock_smtp.starttls.assert_called_once()
            mock_smtp.login.assert_called_once_with("user@example.com", "secret")
            mock_smtp.send_message.assert_called_once()

            msg = mock_smtp.send_message.call_args[0][0]
            assert msg["Subject"] == "SOVA Alert"
            assert msg["From"] == "sova@example.com"
            assert msg["To"] == "dev@example.com"
            assert "Developer finished #42" in msg.get_content()

    async def test_send_email_notification_no_starttls(self) -> None:
        from sova.ipc.notifications import send_email_notification

        config = NotificationConfig(
            email_enabled=True,
            email_to="dev@example.com",
            email_from="sova@example.com",
            email_smtp_host="smtp.example.com",
            email_smtp_port=465,
            email_smtp_starttls=False,
        )

        with patch("sova.ipc.notifications.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

            await send_email_notification(config, "Title", "Body")

            mock_smtp.starttls.assert_not_called()

    async def test_send_email_notification_error_logged(self) -> None:
        from sova.ipc.notifications import _safe_notify, send_email_notification

        config = NotificationConfig(
            email_enabled=True,
            email_to="dev@example.com",
            email_from="sova@example.com",
            email_smtp_host="smtp.example.com",
        )

        with patch("sova.ipc.notifications.send_email_notification", side_effect=Exception("SMTP error")):
            coro = send_email_notification(config, "Title", "Body")
            await _safe_notify("notify.email_failed", coro, "Title")


class TestWebhookNotification:
    async def test_send_webhook_notification_success(self) -> None:
        from unittest.mock import AsyncMock

        from sova.ipc.notifications import send_webhook_notification

        with patch("sova.ipc.notifications.httpx.AsyncClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.post = AsyncMock(return_value=mock_response)

            await send_webhook_notification(
                "https://hooks.example.com/sova",
                '{"Authorization": "Bearer token"}',
                "SOVA Alert",
                "Developer finished #42",
            )

            mock_client.post.assert_awaited_once()
            call_args = mock_client.post.call_args
            assert call_args[0][0] == "https://hooks.example.com/sova"
            assert call_args[1]["json"]["title"] == "SOVA Alert"
            assert call_args[1]["json"]["message"] == "Developer finished #42"
            assert call_args[1]["headers"]["Authorization"] == "Bearer token"

    async def test_send_webhook_notification_error_logged(self) -> None:
        from sova.ipc.notifications import _safe_notify, send_webhook_notification

        with patch("sova.ipc.notifications.send_webhook_notification", side_effect=Exception("HTTP error")):
            coro = send_webhook_notification("https://example.com", "", "Title", "Body")
            await _safe_notify("notify.webhook_failed", coro, "Title")


class TestNotifyDispatcher:
    async def test_notify_dispatches_to_email(self) -> None:
        from sova.ipc.notifications import notify

        config = NotificationConfig(
            desktop=False,
            email_enabled=True,
            email_to="dev@example.com",
            email_from="sova@example.com",
            email_smtp_host="smtp.example.com",
        )

        with (
            patch("sova.ipc.notifications.send_desktop_notification") as mock_desktop,
            patch("sova.ipc.notifications.send_email_notification") as mock_email,
        ):
            notify(config, "Title", "Body", subtitle="Sub")
            await asyncio.sleep(0.01)

            mock_desktop.assert_not_awaited()
            mock_email.assert_awaited_once()
            assert mock_email.call_args[0][0] == config
            assert mock_email.call_args[0][1] == "Title"
            assert "Sub" in mock_email.call_args[0][2]
            assert "Body" in mock_email.call_args[0][2]

    async def test_notify_dispatches_to_webhook(self) -> None:
        from sova.ipc.notifications import notify

        config = NotificationConfig(
            desktop=False,
            webhook_url="https://hooks.example.com/sova",
            webhook_headers='{"Authorization": "Bearer token"}',
        )

        with (
            patch("sova.ipc.notifications.send_desktop_notification") as mock_desktop,
            patch("sova.ipc.notifications.send_webhook_notification") as mock_webhook,
        ):
            notify(config, "Title", "Body")
            await asyncio.sleep(0.01)

            mock_desktop.assert_not_awaited()
            mock_webhook.assert_awaited_once()
            assert mock_webhook.call_args[0][0] == "https://hooks.example.com/sova"
            assert mock_webhook.call_args[0][2] == "Title"
            assert mock_webhook.call_args[0][3] == "Body"


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

    def test_codex_runtime_name(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        rt = CodexRuntime()
        assert rt.name == "codex"

    def test_create_runtime_codex(self) -> None:
        from sova.ipc.runtime import CodexRuntime, create_runtime

        rt = create_runtime("codex")
        assert isinstance(rt, CodexRuntime)
        assert rt._config.model == ""
        assert rt._config.sandbox == "workspace-write"

    def test_create_runtime_codex_forwards_config(self) -> None:
        from sova.config.models import CodexConfig
        from sova.ipc.runtime import CodexRuntime, create_runtime

        cfg = CodexConfig(model="gpt-5-codex", sandbox="read-only")
        rt = create_runtime("codex", codex=cfg)
        assert isinstance(rt, CodexRuntime)
        assert rt._config is cfg

    def test_create_runtime_codex_config_ignored_for_other_runtimes(self) -> None:
        from sova.config.models import CodexConfig
        from sova.ipc.runtime import ClaudeCodeRuntime, create_runtime

        rt = create_runtime("claude-code", codex=CodexConfig(model="gpt-5-codex"))
        assert isinstance(rt, ClaudeCodeRuntime)

    def test_create_runtime_unknown_raises(self) -> None:
        from sova.ipc.runtime import create_runtime

        with pytest.raises(ValueError, match="Unknown agent runtime"):
            create_runtime("nonexistent")

    def test_runtime_literal_matches_registry_and_settings_meta(self) -> None:
        """Config Literal, runtime registry, and settings-meta options must agree.

        A runtime added to one but not the others silently diverges: e.g. a
        runtime selectable in sova.toml but absent from the dashboard select,
        or vice versa.
        """
        from typing import get_args

        from sova.config.models import AgentConfig
        from sova.dashboard.settings_meta import get_meta
        from sova.ipc.runtime import _RUNTIMES

        literal_values = set(get_args(AgentConfig.model_fields["runtime"].annotation))
        assert literal_values == set(_RUNTIMES)

        meta = get_meta("agent.runtime")
        assert meta is not None
        assert set(meta.options) == literal_values

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

    def test_reload_runtime_swaps_global_only(self) -> None:
        """reload_runtime() swaps the singleton without mutating held references.

        Simulates an in-flight agent spawned before the reload: its own
        runtime object must be untouched.
        """
        from sova.config.models import ProjectConfig
        from sova.ipc.runtime import ClaudeCodeRuntime, get_runtime, reload_runtime, set_runtime

        set_runtime(ClaudeCodeRuntime())
        in_flight_runtime = get_runtime()

        reload_runtime(ProjectConfig(agent={"runtime": "codex"}, codex={"model": "gpt-5-codex"}))

        assert isinstance(in_flight_runtime, ClaudeCodeRuntime)
        new_runtime = get_runtime()
        assert new_runtime is not in_flight_runtime
        assert new_runtime.name == "codex"
        assert new_runtime._config.model == "gpt-5-codex"

        # Reset for other tests
        set_runtime(ClaudeCodeRuntime())

    def test_reload_runtime_passes_codex_config(self) -> None:
        from sova.config.models import ProjectConfig
        from sova.ipc.runtime import ClaudeCodeRuntime, get_runtime, reload_runtime, set_runtime

        reload_runtime(ProjectConfig(agent={"runtime": "codex"}, codex={"sandbox": "read-only"}))
        assert get_runtime()._config.sandbox == "read-only"

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
        assert args[pm_idx + 1] == "bypassPermissions"

    async def test_spawn_uses_shared_cli_args_builder(self, tmp_path: Path) -> None:
        """The runtime spawn path shares its argv construction with the LLM
        provider path via sova.llm.cli_args, rather than building it inline."""
        from decimal import Decimal

        from sova.ipc.runtime import ClaudeCodeRuntime
        from sova.llm.cli_args import build_claude_cli_args

        mock_proc = AsyncMock()
        mock_proc.pid = 104
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        runtime = ClaudeCodeRuntime()
        with (
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
            patch("sova.ipc.runtime.build_claude_cli_args", wraps=build_claude_cli_args) as mock_builder,
        ):
            await runtime.spawn(
                "test", tmp_path, model="sonnet", fallback_model="haiku", max_budget_usd=Decimal("2.50")
            )

        mock_builder.assert_called_once()
        assert mock_builder.call_args.kwargs["model"] == "sonnet"
        assert mock_builder.call_args.kwargs["fallback_model"] == "haiku"
        assert mock_builder.call_args.kwargs["max_budget_usd"] == Decimal("2.50")
        assert mock_builder.call_args.kwargs["output_format"] == "stream-json"

        args = mock_exec.call_args[0]
        assert "--max-budget-usd" in args
        assert "2.50" in args

    def test_headless_preamble_forbids_pipeline_actions(self) -> None:
        from sova.ipc.runtime import _HEADLESS_PREAMBLE

        preamble_lower = _HEADLESS_PREAMBLE.lower()
        assert "do not create pull requests" in preamble_lower or "never create pull request" in preamble_lower
        assert "do not push" in preamble_lower or "never push" in preamble_lower
        assert "do not commit" in preamble_lower or "never commit" in preamble_lower

    def test_headless_preamble_includes_context_management(self) -> None:
        from sova.ipc.runtime import _HEADLESS_PREAMBLE

        preamble_lower = _HEADLESS_PREAMBLE.lower()
        assert "/compact" in preamble_lower
        assert "context" in preamble_lower


class TestSpawnDirect:
    async def test_spawn_direct_creates_subprocess(self) -> None:
        from sova.ipc.runtime import spawn_direct

        mock_proc = AsyncMock()
        mock_proc.pid = 77
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            ap = await spawn_direct(["sova", "run", "42", "--run-id", "7"], Path("/tmp"))

        assert ap.pid == 77
        call_args = mock_exec.call_args
        assert call_args[0] == ("sova", "run", "42", "--run-id", "7")

    async def test_spawn_direct_with_file_output(self, tmp_path: Path) -> None:
        from sova.ipc.runtime import spawn_direct

        mock_proc = AsyncMock()
        mock_proc.pid = 78
        mock_proc.returncode = None

        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc):
            fp = await spawn_direct(
                ["sova", "run", "42"],
                tmp_path,
                output_dir=tmp_path,
                run_label="100",
            )

        from sova.ipc.control import FileAgentProcess

        assert isinstance(fp, FileAgentProcess)
        assert fp.pid == 78

    def test_pipeline_roles_set(self) -> None:
        from sova.ipc.runtime import _PIPELINE_ROLES

        assert "developer" in _PIPELINE_ROLES
        assert "researcher" in _PIPELINE_ROLES
        assert "planner" in _PIPELINE_ROLES
        assert "reviewer" not in _PIPELINE_ROLES


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


class TestCodexRuntime:
    async def test_spawn_builds_correct_args(self) -> None:
        from sova.config.models import CodexConfig
        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 55
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = CodexRuntime(config=CodexConfig(model="gpt-5-codex"))
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            ap = await rt.spawn("fix the bug", Path("/tmp"))

        assert ap.pid == 55
        args = mock_exec.call_args[0]
        assert args[0] == "codex"
        assert args[1] == "exec"
        assert "--json" in args
        assert "--sandbox" in args
        sandbox_idx = args.index("--sandbox")
        assert args[sandbox_idx + 1] == "workspace-write"
        assert "--model" in args
        model_idx = args.index("--model")
        assert args[model_idx + 1] == "gpt-5-codex"
        assert args[-1] == "fix the bug"
        assert "--full-auto" not in args

    async def test_spawn_without_model_omits_flag(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 56
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = CodexRuntime()
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            ap = await rt.spawn("do something", Path("/tmp"))

        assert ap.pid == 56
        args = mock_exec.call_args[0]
        assert "--model" not in args
        assert args[-1] == "do something"

    async def test_spawn_ignores_caller_supplied_model(self) -> None:
        """CodexRuntime uses ``codex.model`` exclusively, never the caller's model.

        The caller's ``model`` argument is a Claude model id resolved from
        ``agent.model``, which Codex cannot serve.
        """
        from sova.config.models import CodexConfig
        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 58
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = CodexRuntime(config=CodexConfig(model="gpt-5-codex"))
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await rt.spawn("fix the bug", Path("/tmp"), model="claude-opus-4-6")

        args = mock_exec.call_args[0]
        model_idx = args.index("--model")
        assert args[model_idx + 1] == "gpt-5-codex"
        assert "claude-opus-4-6" not in args

    async def test_spawn_uses_configured_sandbox(self) -> None:
        from sova.config.models import CodexConfig
        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 59
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = CodexRuntime(config=CodexConfig(sandbox="read-only"))
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await rt.spawn("do something", Path("/tmp"))

        args = mock_exec.call_args[0]
        sandbox_idx = args.index("--sandbox")
        assert args[sandbox_idx + 1] == "read-only"

    async def test_spawn_passes_prompt_verbatim_without_shell(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 57
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        prompt = 'do `whoami` && rm -rf $(echo /); echo "done"'
        rt = CodexRuntime()
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await rt.spawn(prompt, Path("/tmp"))

        args = mock_exec.call_args[0]
        assert args[-1] == prompt

    async def test_spawn_prompt_with_leading_dash_uses_separator(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 61
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        prompt = "--rm -rf / ignore this flag-looking prompt"
        rt = CodexRuntime()
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await rt.spawn(prompt, Path("/tmp"))

        args = mock_exec.call_args[0]
        assert args[-1] == prompt
        assert args[-2] == "--"

    def test_parse_output_plain_text(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        rt = CodexRuntime()
        event = rt.parse_output("plain text line")
        assert event is not None
        assert event.type == "content"
        assert event.text == "plain text line"

    def test_parse_output_delegates_to_stateful_parser(self) -> None:
        """The runtime owns one CodexStreamParser instance, so thread state persists across calls."""
        import json

        from sova.ipc.runtime import CodexRuntime

        rt = CodexRuntime()
        rt.parse_output(json.dumps({"type": "thread.started", "thread_id": "thread-abc"}))
        event = rt.parse_output(json.dumps({"type": "turn.completed", "usage": {}}))

        assert event is not None
        assert event.type == "result"
        assert event.result is not None
        assert event.result.session_id == "thread-abc"

    def test_parse_output_empty(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        rt = CodexRuntime()
        assert rt.parse_output("") is None
        assert rt.parse_output("   ") is None

    async def test_check_available_found(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        rt = CodexRuntime()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"codex-cli 0.20.0\n", b""))
        mock_proc.returncode = 0

        with (
            patch("sova.ipc.runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            ok, detail = await rt.check_available()

        assert ok is True
        assert "0.20.0" in detail

    async def test_check_available_not_found(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        rt = CodexRuntime()
        with patch("sova.ipc.runtime.shutil.which", return_value=None):
            ok, detail = await rt.check_available()

        assert ok is False
        assert "not found" in detail

    @staticmethod
    def _version_proc() -> AsyncMock:
        """Mock the ``codex --version`` process that precedes every auth probe."""
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"codex-cli 0.20.0\n", b""))
        proc.returncode = 0
        return proc

    async def test_check_available_missing_cli_never_probes_auth(self) -> None:
        """A missing CLI must return the install hint without attempting the login probe."""
        from sova.ipc.runtime import CodexRuntime

        rt = CodexRuntime()
        with (
            patch("sova.ipc.runtime.shutil.which", return_value=None),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec") as mock_exec,
            patch("sova.ipc.runtime.run") as mock_run,
        ):
            ok, detail = await rt.check_available()

        assert ok is False
        assert "install" in detail
        mock_exec.assert_not_called()
        mock_run.assert_not_called()

    async def test_check_available_logged_out(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        rt = CodexRuntime()
        probe = ShellResult(returncode=1, stdout="Not logged in. Run codex login.\n", stderr="")

        with (
            patch("sova.ipc.runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=self._version_proc()),
            patch("sova.ipc.runtime.run", return_value=probe),
        ):
            ok, detail = await rt.check_available()

        assert ok is False
        assert "codex login" in detail

    async def test_check_available_authenticated(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        rt = CodexRuntime()
        probe = ShellResult(returncode=0, stdout="Logged in via ChatGPT session.\n", stderr="")

        with (
            patch("sova.ipc.runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=self._version_proc()),
            patch("sova.ipc.runtime.run", return_value=probe),
        ):
            ok, detail = await rt.check_available()

        assert ok is True
        assert "authenticated" in detail

    async def test_check_available_auth_probe_timeout_fails_open(self) -> None:
        """An unparseable/timed-out auth probe must not block an otherwise working CLI."""
        from sova.ipc.runtime import CodexRuntime

        rt = CodexRuntime()
        probe = ShellResult(returncode=-1, stdout="", stderr="Command timed out after 5.0s", timed_out=True)

        with (
            patch("sova.ipc.runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=self._version_proc()),
            patch("sova.ipc.runtime.run", return_value=probe),
        ):
            ok, detail = await rt.check_available()

        assert ok is True
        assert "unknown" in detail

    async def test_check_available_reports_env_key_presence_without_value(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        rt = CodexRuntime()
        probe = ShellResult(returncode=0, stdout="", stderr="")

        with (
            patch("sova.ipc.runtime.shutil.which", return_value="/usr/bin/codex"),
            patch.dict(os.environ, {"CODEX_API_KEY": "sk-codex-sentinel"}, clear=False),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=self._version_proc()),
            patch("sova.ipc.runtime.run", return_value=probe),
        ):
            ok, detail = await rt.check_available()

        assert ok is True
        assert "CODEX_API_KEY set" in detail
        assert "sk-codex-sentinel" not in detail

    async def test_spawn_injects_codex_api_key_scoped_to_this_process(self) -> None:
        """CODEX_API_KEY must reach the Codex child even though SCRUBBED_VARS strips it by default."""
        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 62
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = CodexRuntime()
        with (
            patch.dict(os.environ, {"CODEX_API_KEY": "sk-codex-sentinel"}, clear=False),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
        ):
            await rt.spawn("do the task", Path("/tmp"))

        child_env = mock_exec.call_args.kwargs["env"]
        assert child_env["CODEX_API_KEY"] == "sk-codex-sentinel"
        args = mock_exec.call_args[0]
        assert "sk-codex-sentinel" not in args

    async def test_spawn_strips_anthropic_api_key_from_codex_child(self) -> None:
        """A key meant for Claude Code must not leak into an unrelated Codex child."""
        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 63
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = CodexRuntime()
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-sentinel"}, clear=False),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
        ):
            await rt.spawn("do the task", Path("/tmp"))

        child_env = mock_exec.call_args.kwargs["env"]
        assert "ANTHROPIC_API_KEY" not in child_env

    async def test_spawn_prefers_caller_supplied_api_key(self) -> None:
        """A curated env's own key wins over whatever is in the server's process env."""
        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 67
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = CodexRuntime()
        with (
            patch.dict(os.environ, {"CODEX_API_KEY": "sk-server-key"}, clear=False),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
        ):
            await rt.spawn("do the task", Path("/tmp"), env={"PATH": "/bin", "CODEX_API_KEY": "sk-project-key"})

        child_env = mock_exec.call_args.kwargs["env"]
        assert child_env["CODEX_API_KEY"] == "sk-project-key"

    async def test_spawn_omits_key_when_caller_env_excludes_it(self) -> None:
        """A caller that deliberately built an env without the key must not get the server's."""
        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 68
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = CodexRuntime()
        with (
            patch.dict(os.environ, {"CODEX_API_KEY": "sk-server-key"}, clear=False),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
        ):
            await rt.spawn("do the task", Path("/tmp"), env={"PATH": "/bin"})

        child_env = mock_exec.call_args.kwargs["env"]
        assert "CODEX_API_KEY" not in child_env

    async def test_spawn_treats_blank_api_key_as_absent(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 69
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = CodexRuntime()
        with (
            patch.dict(os.environ, {"CODEX_API_KEY": "   "}, clear=False),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
        ):
            await rt.spawn("do the task", Path("/tmp"))

        child_env = mock_exec.call_args.kwargs["env"]
        assert "CODEX_API_KEY" not in child_env

    async def test_spawn_without_codex_api_key_omits_it(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 64
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = CodexRuntime()
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
        ):
            os.environ.pop("CODEX_API_KEY", None)
            await rt.spawn("do the task", Path("/tmp"))

        child_env = mock_exec.call_args.kwargs["env"]
        assert "CODEX_API_KEY" not in child_env

    async def test_claude_code_and_aider_never_receive_codex_api_key(self) -> None:
        """CODEX_API_KEY is Codex-only; other runtimes must never see it."""
        from sova.ipc.runtime import AiderRuntime, ClaudeCodeRuntime

        for runtime_cls in (ClaudeCodeRuntime, AiderRuntime):
            mock_proc = AsyncMock()
            mock_proc.pid = 65
            mock_proc.returncode = None
            mock_proc.stdout = AsyncMock()
            mock_proc.stderr = AsyncMock()

            rt = runtime_cls()
            with (
                patch.dict(os.environ, {"CODEX_API_KEY": "sk-codex-sentinel"}, clear=False),
                patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
            ):
                await rt.spawn("do the task", Path("/tmp"))

            child_env = mock_exec.call_args.kwargs["env"]
            assert "CODEX_API_KEY" not in child_env

    async def test_spawn_never_logs_codex_api_key(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 66
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = CodexRuntime()
        with (
            patch.dict(os.environ, {"CODEX_API_KEY": "sk-codex-sentinel"}, clear=False),
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sova.ipc.runtime.log") as mock_log,
        ):
            await rt.spawn("do the task", Path("/tmp"))

        for call in mock_log.mock_calls:
            assert "sk-codex-sentinel" not in str(call)

    async def test_spawn_with_fallback_model_logs_warning_without_prompt(self) -> None:
        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 58
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = CodexRuntime()
        with (
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sova.ipc.runtime.log") as mock_log,
        ):
            ap = await rt.spawn("secret prompt text", Path("/tmp"), fallback_model="gpt-5-mini")

        assert ap.pid == 58
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args[0][0] == "codex.fallback_model_not_supported"
        assert "secret prompt text" not in str(mock_log.warning.call_args)

    async def test_spawn_with_budget_logs_warning_without_prompt(self) -> None:
        from decimal import Decimal

        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 59
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = CodexRuntime()
        with (
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sova.ipc.runtime.log") as mock_log,
        ):
            ap = await rt.spawn("secret prompt text", Path("/tmp"), max_budget_usd=Decimal("5.00"))

        assert ap.pid == 59
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args[0][0] == "codex.budget_not_enforced"
        assert "secret prompt text" not in str(mock_log.warning.call_args)

    async def test_spawn_with_both_unsupported_inputs_warns_twice(self) -> None:
        from decimal import Decimal

        from sova.ipc.runtime import CodexRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 60
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()

        rt = CodexRuntime()
        with (
            patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sova.ipc.runtime.log") as mock_log,
        ):
            await rt.spawn(
                "test",
                Path("/tmp"),
                fallback_model="gpt-5-mini",
                max_budget_usd=Decimal("5.00"),
            )

        assert mock_log.warning.call_count == 2


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


class TestAgentEnvScrubbing:
    """Spawned agents must never inherit provider-routing or session vars.

    Regression guard for the silent-failure class where an inherited
    CLAUDE_CODE_USE_VERTEX redirected every agent away from llm.provider.
    """

    def test_marker_scrubs_hostile_env(self) -> None:
        from sova.ipc.runtime import _inject_agent_marker

        hostile = {
            "PATH": "/usr/bin",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_VERTEX_PROJECT_ID": "leaked-project",
            "CLAUDE_CODE_SESSION_ID": "parent-session",
        }
        with patch("sova.ipc.runtime.configured_passthrough", return_value=()):
            result = _inject_agent_marker(hostile)

        assert result["SOVA_AGENT_RUN"] == "1"
        assert result["PATH"] == "/usr/bin"
        for leaked in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "CLAUDE_CODE_SESSION_ID"):
            assert leaked not in result

    def test_marker_scrubs_process_env_when_none(self) -> None:
        from sova.ipc.runtime import _inject_agent_marker

        with (
            patch.dict(os.environ, {"CLAUDE_CODE_USE_VERTEX": "1"}, clear=False),
            patch("sova.ipc.runtime.configured_passthrough", return_value=()),
        ):
            result = _inject_agent_marker(None)

        assert "CLAUDE_CODE_USE_VERTEX" not in result
        assert result["SOVA_AGENT_RUN"] == "1"

    def test_marker_honors_configured_passthrough(self) -> None:
        from sova.ipc.runtime import _inject_agent_marker

        with patch("sova.ipc.runtime.configured_passthrough", return_value=("CLAUDE_CODE_USE_VERTEX",)):
            result = _inject_agent_marker({"CLAUDE_CODE_USE_VERTEX": "1", "CLOUD_ML_REGION": "global"})

        assert result["CLAUDE_CODE_USE_VERTEX"] == "1"
        assert "CLOUD_ML_REGION" not in result

    def test_passthrough_degrades_to_empty_when_config_unavailable(self) -> None:
        """Spawning must survive a missing or broken project config."""
        from sova.utils.env import configured_passthrough

        with patch("sova.config.loader.load_config", side_effect=RuntimeError("no project")):
            assert configured_passthrough() == ()


class TestInterpretCodexAuthProbe:
    """The pure interpreter for ``codex login status`` output."""

    def test_empty_or_missing_output_is_unknown(self) -> None:
        from sova.ipc.runtime import _interpret_codex_auth_probe

        assert _interpret_codex_auth_probe(None) == (None, "auth state unknown")
        assert _interpret_codex_auth_probe("") == (None, "auth state unknown")

    def test_json_object_reports_logged_in(self) -> None:
        from sova.ipc.runtime import _interpret_codex_auth_probe

        assert _interpret_codex_auth_probe('{"loggedIn": true}') == (True, "authenticated")

    def test_json_object_reports_logged_out(self) -> None:
        from sova.ipc.runtime import _interpret_codex_auth_probe

        authenticated, detail = _interpret_codex_auth_probe('{"logged_in": false}')
        assert authenticated is False
        assert "codex login" in detail

    def test_json_null_value_falls_through_to_unknown(self) -> None:
        """A null field is a shape we do not understand, never a definite logout."""
        from sova.ipc.runtime import _interpret_codex_auth_probe

        assert _interpret_codex_auth_probe('{"loggedIn": null}') == (None, "auth state unknown")

    def test_json_dict_without_a_readable_flag_is_unknown(self) -> None:
        """A key name in the JSON source must not be phrase-matched as a verdict."""
        from sova.ipc.runtime import _interpret_codex_auth_probe

        assert _interpret_codex_auth_probe('{"authenticated": "false"}') == (None, "auth state unknown")
        assert _interpret_codex_auth_probe('{"status": "unknown"}') == (None, "auth state unknown")

    def test_bare_json_scalar_falls_through_to_text_scan(self) -> None:
        """Valid JSON is not necessarily an object; a quoted scalar has no fields to read."""
        from sova.ipc.runtime import _interpret_codex_auth_probe

        assert _interpret_codex_auth_probe('"not logged in"')[0] is False
        assert _interpret_codex_auth_probe('"42"') == (None, "auth state unknown")

    def test_negative_phrase_wins_over_substring_of_positive(self) -> None:
        """The phrase "not logged in" contains "logged in", so the negative scan must run first."""
        from sova.ipc.runtime import _interpret_codex_auth_probe

        assert _interpret_codex_auth_probe("Not logged in. Run codex login.")[0] is False

    def test_unrecognized_output_is_unknown(self) -> None:
        from sova.ipc.runtime import _interpret_codex_auth_probe

        assert _interpret_codex_auth_probe("error: unrecognized subcommand 'status'") == (
            None,
            "auth state unknown",
        )
