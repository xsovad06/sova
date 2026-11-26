"""Handoff API route and the handoff, spec-approval and resume services behind it.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.app import create_app
from sova.db.models import TaskRun
from sova.db.session import get_session
from tests.conftest import agents_page_source
from tests.conftest import csrf_request_headers as _csrf_request_headers

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


# ---------------------------------------------------------------------------
# Per-issue handoff file I/O
# ---------------------------------------------------------------------------


class TestPerIssueHandoffFiles:
    def test_write_creates_per_issue_file(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        h = DashboardHandoff(source="developer", status="awaiting_action", issue="113", summary="test")
        write_handoff_file(tmp_path, h)

        assert (tmp_path / ".claude" / "agent-control" / "handoff-113.json").exists()
        assert not (tmp_path / ".claude" / "agent-control" / "handoff.json").exists()

    def test_write_falls_back_to_legacy_when_no_issue(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        h = DashboardHandoff(source="developer", status="completed", issue="", summary="test")
        write_handoff_file(tmp_path, h)

        assert (tmp_path / ".claude" / "agent-control" / "handoff.json").exists()

    def test_read_with_issue_reads_correct_file(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, read_handoff_file, write_handoff_file

        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="113", summary="a"))
        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="114", summary="b"))

        h = read_handoff_file(tmp_path, issue="113")
        assert h is not None
        assert h.issue == "113"
        assert h.summary == "a"

    def test_read_without_issue_returns_most_recent(self, tmp_path: Path) -> None:
        import os

        from sova.ipc.handoff import DashboardHandoff, read_handoff_file, write_handoff_file

        old_path = write_handoff_file(
            tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="113", summary="old")
        )
        new_path = write_handoff_file(
            tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="114", summary="new")
        )
        os.utime(old_path, (1_700_000_000, 1_700_000_000))
        os.utime(new_path, (1_700_000_100, 1_700_000_100))

        h = read_handoff_file(tmp_path)
        assert h is not None
        assert h.issue == "114"

    def test_read_falls_back_to_legacy(self, tmp_path: Path) -> None:
        import json

        cdir = tmp_path / ".claude" / "agent-control"
        cdir.mkdir(parents=True)
        (cdir / "handoff.json").write_text(
            json.dumps({"id": "x", "source": "dev", "status": "completed", "issue": "99", "summary": "legacy"})
        )

        from sova.ipc.handoff import read_handoff_file

        h = read_handoff_file(tmp_path, issue="99")
        assert h is not None
        assert h.issue == "99"

    def test_read_with_issue_ignores_legacy_for_different_issue(self, tmp_path: Path) -> None:
        import json

        cdir = tmp_path / ".claude" / "agent-control"
        cdir.mkdir(parents=True)
        (cdir / "handoff.json").write_text(
            json.dumps({"id": "x", "source": "dev", "status": "completed", "issue": "114", "summary": "other"})
        )

        from sova.ipc.handoff import read_handoff_file

        h = read_handoff_file(tmp_path, issue="113")
        assert h is None

    def test_read_all_returns_all_files(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, read_all_handoff_files, write_handoff_file

        for issue in ["113", "114", "115"]:
            write_handoff_file(
                tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue=issue, summary=f"#{issue}")
            )

        all_h = read_all_handoff_files(tmp_path)
        assert len(all_h) == 3
        issues = {h.issue for h in all_h}
        assert issues == {"113", "114", "115"}

    def test_parallel_writes_coexist(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        for issue in ["113", "114", "115"]:
            write_handoff_file(
                tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue=issue, summary=f"#{issue}")
            )

        cdir = tmp_path / ".claude" / "agent-control"
        assert (cdir / "handoff-113.json").exists()
        assert (cdir / "handoff-114.json").exists()
        assert (cdir / "handoff-115.json").exists()


class TestPerIssueHandoffService:
    def test_clear_specific_issue(self, tmp_path: Path, monkeypatch) -> None:
        from sova.dashboard.services import handoff_service
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="113", summary="a"))
        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="114", summary="b"))

        cleared = handoff_service.clear_handoff(issue="113")
        assert cleared is True

        cdir = tmp_path / ".claude" / "agent-control"
        assert not (cdir / "handoff-113.json").exists()
        assert (cdir / "handoff-114.json").exists()

    def test_clear_all(self, tmp_path: Path, monkeypatch) -> None:
        from sova.dashboard.services import handoff_service
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="113", summary="a"))
        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="114", summary="b"))

        cleared = handoff_service.clear_all_handoffs()
        assert cleared is True

        cdir = tmp_path / ".claude" / "agent-control"
        assert not (cdir / "handoff-113.json").exists()
        assert not (cdir / "handoff-114.json").exists()

    def test_get_all_handoffs(self, tmp_path: Path, monkeypatch) -> None:
        from sova.dashboard.services import handoff_service
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="113", summary="a"))
        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="114", summary="b"))

        all_h = handoff_service.get_all_handoffs()
        assert len(all_h) == 2
        issues = {h["issue"] for h in all_h}
        assert issues == {"113", "114"}

    def test_get_handoff_with_issue(self, tmp_path: Path, monkeypatch) -> None:
        from sova.dashboard.services import handoff_service
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="113", summary="a"))
        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="114", summary="b"))

        h = handoff_service.get_handoff(issue="113")
        assert h is not None
        assert h["issue"] == "113"


# ---------------------------------------------------------------------------
# Handoff API
# ---------------------------------------------------------------------------


class TestHandoffAPI:
    @pytest.fixture(autouse=True)
    def _isolate_handoff(self, tmp_path, monkeypatch):
        from sova.dashboard.services import handoff_service

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

    async def test_get_handoff_none(self, client: AsyncClient) -> None:
        resp = await client.get("/api/handoff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_handoff"] is False

    async def test_get_handoff_with_file(self, client: AsyncClient, tmp_path) -> None:
        import json

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        handoff_data = {
            "id": "test-123",
            "source": "integrate-pr",
            "status": "awaiting_action",
            "summary": "Test handoff",
            "created_at": "2026-04-20T10:00:00Z",
            "next_actions": [
                {
                    "id": "integrate",
                    "label": "Integrate PR",
                    "style": "approve",
                    "mode": "claude-command",
                    "command": "integrate-pr",
                },
            ],
        }
        (control_dir / "handoff.json").write_text(json.dumps(handoff_data))

        resp = await client.get("/api/handoff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_handoff"] is True
        assert data["handoff"]["source"] == "integrate-pr"
        assert len(data["handoff"]["next_actions"]) == 1

    async def test_unreadable_handoffs_are_listed_project_wide_without_an_issue(
        self, client: AsyncClient, tmp_path
    ) -> None:
        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff-5.json").write_text("{not json", encoding="utf-8")
        (control_dir / "handoff-6.json").write_text("[]", encoding="utf-8")

        resp = await client.get("/api/handoff")

        assert resp.status_code == 200
        assert sorted(resp.json()["unreadable_handoffs"]) == [
            str(control_dir / "handoff-5.json"),
            str(control_dir / "handoff-6.json"),
        ]

    async def test_non_object_handoff_is_reported_not_a_500(self, client: AsyncClient, tmp_path) -> None:
        """A handoff file holding valid JSON that is not an object used to raise in the created_at sort."""
        import json

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        good = {"id": "g", "source": "developer", "status": "completed", "summary": "ok", "issue": "1"}
        (control_dir / "handoff-1.json").write_text(json.dumps(good), encoding="utf-8")
        (control_dir / "handoff-2.json").write_text("[]", encoding="utf-8")

        resp = await client.get("/api/handoff")

        assert resp.status_code == 200
        data = resp.json()
        assert data["handoff"]["issue"] == "1"
        assert data["unreadable_handoffs"] == [str(control_dir / "handoff-2.json")]

    async def test_issue_filter_narrows_unreadable_handoffs_to_that_issue(self, client: AsyncClient, tmp_path) -> None:
        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff-5.json").write_text("{not json", encoding="utf-8")
        (control_dir / "handoff-6.json").write_text("{not json", encoding="utf-8")
        (control_dir / "handoff.json").write_text("{not json", encoding="utf-8")

        resp = await client.get("/api/handoff", params={"issue": "5"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["has_handoff"] is False
        assert sorted(data["unreadable_handoffs"]) == [
            str(control_dir / "handoff-5.json"),
            str(control_dir / "handoff.json"),
        ]

    async def test_schema_invalid_handoff_is_rendered_and_listed_separately(
        self, client: AsyncClient, tmp_path
    ) -> None:
        """The dashboard renders legacy shapes, but the pipeline skips them, so the response names them."""
        import json

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        good = {"id": "g", "source": "developer", "status": "completed", "summary": "ok", "issue": "1"}
        (control_dir / "handoff-1.json").write_text(json.dumps(good), encoding="utf-8")
        (control_dir / "handoff-15.json").write_text('{"issue": "15", "source": "reviewer"}', encoding="utf-8")

        resp = await client.get("/api/handoff")

        assert resp.status_code == 200
        data = resp.json()
        assert sorted(h["issue"] for h in data["handoffs"]) == ["1", "15"]
        assert data["unreadable_handoffs"] == []
        assert data["schema_invalid_handoffs"] == [str(control_dir / "handoff-15.json")]

    async def test_issue_filter_narrows_schema_invalid_handoffs_to_that_issue(
        self, client: AsyncClient, tmp_path
    ) -> None:
        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff-5.json").write_text('{"issue": "5"}', encoding="utf-8")
        (control_dir / "handoff-6.json").write_text('{"issue": "6"}', encoding="utf-8")

        resp = await client.get("/api/handoff", params={"issue": "5"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["has_handoff"] is True
        assert data["schema_invalid_handoffs"] == [str(control_dir / "handoff-5.json")]

    async def test_clear_handoff_no_file(self, client: AsyncClient) -> None:
        resp = await client.post("/api/handoff/clear")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleared"] is False

    async def test_clear_handoff_with_file(self, client: AsyncClient, tmp_path) -> None:
        import json

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff.json").write_text(
            json.dumps(
                {
                    "id": "test-456",
                    "source": "test",
                    "status": "completed",
                    "summary": "Done",
                    "created_at": "2026-04-20T10:00:00Z",
                }
            )
        )

        resp = await client.post("/api/handoff/clear")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleared"] is True

        # File should be gone
        assert not (control_dir / "handoff.json").exists()
        # Archive should exist
        assert (control_dir / "handoff-archive").exists()

    @pytest.mark.parametrize(
        ("body", "cleared"), [({"issue": ""}, False), ({"issue": "#"}, False), ({"issue": None}, False), ({}, True)]
    )
    async def test_only_a_missing_issue_clears_every_handoff(
        self, client: AsyncClient, tmp_path, body: dict, cleared: bool
    ) -> None:
        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        files = [control_dir / "handoff-1.json", control_dir / "handoff.json"]
        for f in files:
            f.write_text('{"source": "test", "status": "awaiting_action"}', encoding="utf-8")

        resp = await client.post("/api/handoff/clear", json=body)

        assert resp.json()["cleared"] is cleared
        assert [f.exists() for f in files] == [not cleared, not cleared]

    async def test_execute_no_handoff(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/handoff/execute",
            json={"action_id": "merge"},
        )
        assert resp.status_code == 404
        data = resp.json()
        assert "not found" in data["detail"]

    async def test_execute_action_not_found(self, client: AsyncClient, tmp_path) -> None:
        import json

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff.json").write_text(
            json.dumps(
                {
                    "id": "test-789",
                    "source": "test",
                    "status": "awaiting_action",
                    "summary": "Test",
                    "created_at": "2026-04-20T10:00:00Z",
                    "next_actions": [{"id": "merge", "label": "Merge"}],
                }
            )
        )

        resp = await client.post(
            "/api/handoff/execute",
            json={"action_id": "nonexistent"},
        )
        assert resp.status_code == 404
        data = resp.json()
        assert "not found" in data["detail"]

    async def test_execute_agent_action_passes_pr_number(self, client: AsyncClient, tmp_path) -> None:
        """Executing an agent-mode handoff action should pass pr_number to start_agent."""
        import json
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import control_service as cs

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff.json").write_text(
            json.dumps(
                {
                    "id": "test-pr",
                    "source": "reviewer",
                    "status": "awaiting_action",
                    "issue": "16",
                    "pr_number": 67,
                    "summary": "5 findings",
                    "created_at": "2026-05-02T22:00:00Z",
                    "next_actions": [
                        {
                            "id": "address_review",
                            "label": "Address Review",
                            "mode": "agent",
                            "args": {"issue": "16", "pr": 67, "role": "developer"},
                            "auto_execute": True,
                        }
                    ],
                }
            )
        )

        with patch.object(
            cs, "start_agent", new_callable=AsyncMock, return_value={"status": "started", "run_id": 1, "pid": 123}
        ) as mock_start:
            resp = await client.post("/api/handoff/execute", json={"action_id": "address_review"})

        assert resp.status_code == 200
        mock_start.assert_awaited_once()
        call_kwargs = mock_start.call_args
        assert call_kwargs.kwargs["pr_number"] == 67
        assert call_kwargs.args[0] == "16"

    async def test_execute_finds_a_pr_only_handoff_by_its_pr_ref(self, client: AsyncClient, tmp_path) -> None:
        """The activity feed posts ``pr:N`` as the issue for a PR with no issue, and that finds the PR's handoff."""
        import json
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import control_service as cs

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        base = {"source": "reviewer", "status": "awaiting_action", "created_at": "2026-05-02T22:00:00Z"}
        (control_dir / "handoff.json").write_text(
            json.dumps(
                {
                    **base,
                    "id": "pr-only",
                    "pr_number": 67,
                    "next_actions": [
                        {"id": "merge", "label": "Merge", "mode": "agent", "args": {"issue": "", "pr": 67}}
                    ],
                }
            )
        )
        with patch.object(
            cs, "start_agent", new_callable=AsyncMock, return_value={"status": "started", "run_id": 1, "pid": 123}
        ) as mock_start:
            wrong = await client.post("/api/handoff/execute", json={"action_id": "merge", "issue": "pr:68"})
            right = await client.post("/api/handoff/execute", json={"action_id": "merge", "issue": "pr:67"})

        assert wrong.status_code == 404
        assert right.status_code == 200
        assert right.json()["status"] == "started"
        mock_start.assert_awaited_once()
        call = mock_start.await_args
        # The agent starts against the PR, not against the raw ``pr:67`` ref passed off as an issue.
        assert call.kwargs["pr_number"] == 67
        assert call.args[0] == ""

    @pytest.mark.parametrize("ref", ["pr:", "pr:abc", "pr:67x", "pr:-1", "pr: 67", "PR:67"])
    async def test_a_malformed_pr_ref_matches_no_handoff_and_starts_nothing(
        self, client: AsyncClient, tmp_path, ref: str
    ) -> None:
        import json
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import control_service as cs

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff.json").write_text(
            json.dumps(
                {
                    "id": "pr-only",
                    "source": "reviewer",
                    "status": "awaiting_action",
                    "pr_number": 67,
                    "created_at": "2026-05-02T22:00:00Z",
                    "next_actions": [
                        {"id": "merge", "label": "Merge", "mode": "agent", "args": {"issue": "", "pr": 67}}
                    ],
                }
            )
        )
        with patch.object(cs, "start_agent", new_callable=AsyncMock) as mock_start:
            resp = await client.post("/api/handoff/execute", json={"action_id": "merge", "issue": ref})

        assert resp.status_code == 404
        mock_start.assert_not_awaited()

    async def test_a_pr_ref_runs_the_handoff_that_carries_that_pr_not_another_one_offering_the_same_action(
        self, client: AsyncClient, tmp_path
    ) -> None:
        """Two handoffs: one for issue 16 (with PR 67) and one for PR 68 alone, both offering ``merge``. A ``pr:N``
        ref picks the handoff carrying that PR, never another one that offers the same action."""
        import json
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import control_service as cs

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        base = {"source": "reviewer", "status": "awaiting_action", "created_at": "2026-05-02T22:00:00Z"}
        (control_dir / "handoff-16.json").write_text(
            json.dumps(
                {
                    **base,
                    "id": "with-issue",
                    "issue": "16",
                    "pr_number": 67,
                    "next_actions": [
                        {"id": "merge", "label": "Merge", "mode": "agent", "args": {"issue": "16", "pr": 67}}
                    ],
                }
            )
        )
        # A handoff with no issue lives in the legacy file (``handoff_filename`` of no issue).
        (control_dir / "handoff.json").write_text(
            json.dumps(
                {
                    **base,
                    "id": "pr-only",
                    "pr_number": 68,
                    "next_actions": [
                        {"id": "merge", "label": "Merge", "mode": "agent", "args": {"issue": "", "pr": 68}}
                    ],
                }
            )
        )
        with patch.object(
            cs, "start_agent", new_callable=AsyncMock, return_value={"status": "started", "run_id": 1, "pid": 123}
        ) as mock_start:
            # The PR of an issue-backed handoff finds it (the PR is the same work), and clears only that issue's file.
            by_pr = await client.post("/api/handoff/execute", json={"action_id": "merge", "issue": "pr:67"})
            assert by_pr.status_code == 200
            assert mock_start.await_args.kwargs["pr_number"] == 67
            assert mock_start.await_args.args[0] == "16"
            pr_only = await client.post("/api/handoff/execute", json={"action_id": "merge", "issue": "pr:68"})
            assert pr_only.status_code == 200
            assert mock_start.await_args.kwargs["pr_number"] == 68
            assert mock_start.await_args.args[0] == ""
        assert mock_start.await_count == 2

    async def test_agents_page_has_interrupted_banner(self, client: AsyncClient) -> None:
        resp = await client.get("/work")
        assert resp.status_code == 200
        assert 'id="interrupted-banner"' in resp.text
        assert "checkInterrupted" in agents_page_source()
        assert "dismissInterrupted" in agents_page_source()

    async def test_agents_page_has_status_filter(self, client: AsyncClient) -> None:
        from sova.core.state import TASK_RUN_TERMINAL

        resp = await client.get("/work")
        assert resp.status_code == 200
        assert 'id="filter-status"' in resp.text
        for status in TASK_RUN_TERMINAL:
            assert f'<option value="{status}">' in resp.text

    async def test_agents_page_history_filters_are_rendered(self, client: AsyncClient) -> None:
        """The filter bar's controls; tests/test_dashboard_run_history.py drives them under Node and checks
        each one reaches the /work/history query."""
        resp = await client.get("/work")
        assert resp.status_code == 200
        for element_id in ("filter-action", "filter-number", "history-from", "history-to", "history-clear"):
            assert f'id="{element_id}"' in resp.text, element_id
        for preset in ("today", "7d", "30d", "all", "custom"):
            assert f'id="history-range-{preset}"' in resp.text, preset
        seven_days = resp.text[resp.text.index('id="history-range-7d"') :]
        assert 'aria-pressed="true"' in seven_days[: seven_days.index(">")], "Last 7 days is the default range"
        assert 'id="filter-role"' not in resp.text

    async def test_agents_page_history_rerun_button_carries_run_id(self, client: AsyncClient) -> None:
        """The history re-run button must thread data-run-id through so the delegated
        click handler can auto-dismiss the originating interrupted run, matching the
        banner's own re-run button (source-level check, same pattern as above)."""
        resp = await client.get("/work")
        assert resp.status_code == 200
        idx = agents_page_source().find("function _rerunButton(")
        assert idx != -1, "_rerunButton() not found in the history module"
        body = agents_page_source()[idx : idx + 800]
        assert "data-run-id=" in body

    async def test_agents_page_has_pr_summary_strip(self, client: AsyncClient) -> None:
        resp = await client.get("/work")
        assert resp.status_code == 200
        assert 'id="pr-summary-strip"' in resp.text
        assert "renderPrSummary" in agents_page_source()

    async def test_pr_summary_strip_js_constants_and_structure(self, client: AsyncClient) -> None:
        """Verify JS constants, metric labels, and stale threshold for renderPrSummary."""
        resp = await client.get("/work")
        assert resp.status_code == 200
        html = agents_page_source()
        # Stale threshold: 7 days in seconds
        assert "_STALE_THRESHOLD_SECONDS = 7 * 86400" in html
        # Caching key mechanism
        assert "_lastPrSummaryKey" in html
        # All six metric labels present in the metrics array
        for label in ("Open", "Review", "Changes", "CI Fail", "Ready", "Stale"):
            assert f"label: '{label}'" in html
        # Avg Age fallback label when stale count is zero
        assert "'Avg Age'" in html
        # Error handling: try-catch wraps the function body
        assert "console.error('PR summary render failed:'" in html

    async def test_pr_summary_strip_state_counting_logic(self, client: AsyncClient) -> None:
        """Verify JS logic counts the correct computed_state values for each metric."""
        resp = await client.get("/work")
        assert resp.status_code == 200
        html = agents_page_source()
        # awaiting_review state counted
        assert "st === 'awaiting_review'" in html
        # changes_requested state counted
        assert "st === 'changes_requested'" in html
        # Only approved_ci_green counted for Ready metric
        assert "st === 'approved_ci_green'" in html
        # CI fail detection via ci_status field
        assert "pr.ci_status === 'failed'" in html

    async def test_pr_summary_strip_visible_when_collapsed(self, client: AsyncClient) -> None:
        """Verify the summary strip stays visible when the tracker list is collapsed."""
        resp = await client.get("/work")
        assert resp.status_code == 200
        html = agents_page_source()
        # Strip starts hidden (empty data, not collapsed state)
        assert 'id="pr-summary-strip" class="hidden"' in resp.text
        # togglePrTracker only hides the list, not the strip
        assert "list.classList.toggle('hidden', _prTrackerCollapsed)" in html
        # Strip becomes visible via classList.remove('hidden') when data arrives
        assert "strip.classList.remove('hidden')" in html

    async def test_pr_summary_strip_dynamic_grid_cols(self, client: AsyncClient) -> None:
        """Verify grid-cols is computed from metrics.length, not hardcoded."""
        resp = await client.get("/work")
        assert resp.status_code == 200
        html = agents_page_source()
        assert "grid-cols-' + metrics.length + '" in html

    async def test_pr_api_provides_fields_for_metrics(self, client: AsyncClient, monkeypatch) -> None:
        """Verify /api/prs/open returns all fields needed by renderPrSummary."""
        from unittest.mock import AsyncMock

        mock_prs = [
            {
                "number": 1,
                "title": "Test PR",
                "computed_state": "awaiting_review",
                "age_seconds": 700000,
                "ci_status": "failed",
            },
            {
                "number": 2,
                "title": "Old PR",
                "computed_state": "approved",
                "age_seconds": 100,
                "ci_status": "passed",
            },
        ]
        monkeypatch.setattr(
            "sova.dashboard.routers.prs.list_open_prs_with_state",
            AsyncMock(return_value=mock_prs),
        )
        resp = await client.get("/api/prs/open")
        assert resp.status_code == 200
        prs = resp.json()["prs"]
        assert len(prs) == 2
        # All fields required by renderPrSummary are present
        for pr in prs:
            assert "computed_state" in pr
            assert "age_seconds" in pr
            assert "ci_status" in pr

    async def test_pr_summary_ready_counts_only_approved_ci_green(self, client: AsyncClient) -> None:
        """Ready metric must count only approved_ci_green, not plain approved."""
        resp = await client.get("/work")
        assert resp.status_code == 200
        html = agents_page_source()
        # The approved counter increments only for approved_ci_green
        assert "st === 'approved_ci_green') approved++" in html
        # Plain 'approved' without ci_green must NOT increment the counter
        assert "st === 'approved') approved++" not in html

    async def test_pr_summary_empty_array_hides_strip(self, client: AsyncClient) -> None:
        """When PR data is empty, the strip is hidden and cache key is cleared."""
        resp = await client.get("/work")
        assert resp.status_code == 200
        html = agents_page_source()
        # Empty-array guard: hide strip and clear key
        assert "if (!prs.length)" in html
        assert "_lastPrSummaryKey = null" in html

    async def test_pr_summary_avg_age_fallback(self, client: AsyncClient) -> None:
        """When stale count is zero, the Stale cell shows avg age instead."""
        resp = await client.get("/work")
        assert resp.status_code == 200
        html = agents_page_source()
        # Stale cell toggles between stale count and avg age
        assert "m.label === 'Stale' && m.value === 0 ? avgAge : String(m.value)" in html
        assert "m.label === 'Stale' && m.value === 0 ? 'Avg Age' : m.label" in html

    async def test_pr_summary_cache_key_includes_all_metrics(self, client: AsyncClient) -> None:
        """Cache key must include all metric values to detect any change."""
        resp = await client.get("/work")
        assert resp.status_code == 200
        html = agents_page_source()
        # Key joins all six computed values plus ageHours
        assert "[total, awaitingReview, changesRequested, ciFailing, approved, stale, ageHours].join(',')" in html

    async def test_pr_summary_strip_not_hidden_on_collapse_in_toggle(self, client: AsyncClient) -> None:
        """togglePrTracker must not hide the summary strip."""
        resp = await client.get("/work")
        assert resp.status_code == 200
        html = agents_page_source()
        # The toggle function should NOT contain strip visibility logic
        # (strip stays visible regardless of collapse state)
        assert "strip.classList.toggle('hidden'" not in html


# ---------------------------------------------------------------------------
# Synthesize PR actions
# ---------------------------------------------------------------------------


class TestHandoffFallback:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        from sova.dashboard.services import handoff_service

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

    async def test_get_handoff_falls_back_to_synthesized(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        synthesized = {
            "source": "pr-review-state",
            "status": "awaiting_action",
            "issue": "42",
            "pr_number": 99,
            "summary": "Actions synthesized from PR review state",
            "next_actions": [
                {
                    "id": "address_review",
                    "label": "Address Review",
                    "mode": "agent",
                    "args": {"issue": "42", "pr": 99, "role": "developer"},
                },
            ],
        }
        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.get_synthesized_handoff",
            AsyncMock(return_value=synthesized),
        )

        resp = await client.get("/api/handoff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_handoff"] is True
        assert data["handoff"]["source"] == "pr-review-state"
        assert len(data["handoff"]["next_actions"]) == 1

    async def test_file_handoff_takes_precedence(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        import json
        from unittest.mock import AsyncMock

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff.json").write_text(
            json.dumps(
                {
                    "id": "file-handoff",
                    "source": "reviewer",
                    "status": "awaiting_action",
                    "summary": "From file",
                    "created_at": "2026-04-20T10:00:00Z",
                    "next_actions": [{"id": "integrate", "label": "Integrate"}],
                }
            )
        )

        mock_synth = AsyncMock(return_value={"source": "pr-review-state", "next_actions": []})
        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.get_synthesized_handoff",
            mock_synth,
        )

        resp = await client.get("/api/handoff")
        data = resp.json()
        assert data["has_handoff"] is True
        assert data["handoff"]["source"] == "reviewer"
        # Synthesized should not have been called
        mock_synth.assert_not_awaited()

    async def test_no_handoff_and_no_synthesis(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.get_synthesized_handoff",
            AsyncMock(return_value=None),
        )

        resp = await client.get("/api/handoff")
        data = resp.json()
        assert data["has_handoff"] is False


# ---------------------------------------------------------------------------
# _summarize_ci_checks
# ---------------------------------------------------------------------------


class TestExecuteHandoffAction:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        from sova.dashboard.services import handoff_service

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

    async def test_execute_synthesized_handoff_action(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock, MagicMock

        synthesized = {
            "source": "pr-review-state",
            "status": "awaiting_action",
            "issue": "42",
            "pr_number": 99,
            "summary": "Synthesized",
            "next_actions": [
                {
                    "id": "address_review",
                    "label": "Address Review",
                    "description": "Address review findings",
                    "style": "approve",
                    "mode": "agent",
                    "command": "",
                    "args": {"issue": "42", "pr": 99, "role": "developer"},
                    "auto_execute": False,
                },
            ],
        }
        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.get_synthesized_handoff",
            AsyncMock(return_value=synthesized),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.control_service.start_agent",
            AsyncMock(return_value={"status": "started", "run_id": 1}),
        )
        mock_invalidate = MagicMock()
        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.invalidate_synthesis_cache",
            mock_invalidate,
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "address_review"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "Address Review"
        mock_invalidate.assert_called_once_with("42", 99)

    async def test_execute_action_not_found(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.get_synthesized_handoff",
            AsyncMock(
                return_value={
                    "source": "pr-review-state",
                    "next_actions": [{"id": "integrate", "label": "Integrate"}],
                }
            ),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "nonexistent"})
        assert resp.status_code == 404

    async def test_execute_no_handoff(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.get_synthesized_handoff",
            AsyncMock(return_value=None),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "anything"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Spec action execution via handoff endpoint
# ---------------------------------------------------------------------------


class TestExecuteSpecActions:
    """Test that synthesized spec actions (approve/reject) are executable."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        from sova.dashboard.services import handoff_service

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

    async def test_approve_spec_calls_resume_from_approval(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.get_synthesized_handoff",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.find_awaiting_approval_run",
            AsyncMock(return_value=42),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.control_service.resume_from_approval",
            AsyncMock(return_value={"run_id": 100, "resumed_from": 42, "issue": "258", "role": "researcher"}),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "approve-spec", "issue": "258"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "Approve Spec"
        assert data["run_id"] == 100

    async def test_reject_spec_calls_reject_spec(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.get_synthesized_handoff",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.find_awaiting_approval_run",
            AsyncMock(return_value=42),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.control_service.reject_spec",
            AsyncMock(return_value={"run_id": 42, "issue": "258", "status": "rejected"}),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "reject-spec", "issue": "258"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "Reject"
        assert data["status"] == "rejected"

    async def test_spec_action_no_awaiting_run_returns_404(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.get_synthesized_handoff",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.find_awaiting_approval_run",
            AsyncMock(return_value=None),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "approve-spec", "issue": "258"})
        assert resp.status_code == 404

    async def test_spec_action_conflict_returns_409(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.get_synthesized_handoff",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.find_awaiting_approval_run",
            AsyncMock(return_value=42),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.control_service.resume_from_approval",
            AsyncMock(return_value={"error": "conflict", "detail": "Already claimed"}),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "approve-spec", "issue": "258"})
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# reject_spec (DB-level)
# ---------------------------------------------------------------------------


class TestRejectSpec:
    """DB-level tests for the reject_spec service function."""

    async def test_reject_success(self) -> None:
        from sova.dashboard.services.agent_lifecycle import reject_spec

        async with await get_session() as session, session.begin():
            run = TaskRun(issue_number="80", role="researcher", status="awaiting_approval")
            session.add(run)
        await session.commit()
        run_id = run.id

        result = await reject_spec(run_id)
        assert result["status"] == "rejected"
        assert result["run_id"] == run_id
        assert result["issue"] == "80"

        async with await get_session() as session, session.begin():
            updated = await session.get(TaskRun, run_id)
        assert updated.status == "rejected"

    async def test_reject_not_found(self) -> None:
        from sova.dashboard.services.agent_lifecycle import reject_spec

        result = await reject_spec(999999)
        assert result["error"] == "not_found"

    async def test_reject_wrong_status(self) -> None:
        from sova.dashboard.services.agent_lifecycle import reject_spec

        async with await get_session() as session, session.begin():
            run = TaskRun(issue_number="81", role="researcher", status="done")
            session.add(run)
        await session.commit()
        run_id = run.id

        result = await reject_spec(run_id)
        assert result["error"] == "conflict"
        assert "done" in result["detail"]


# ---------------------------------------------------------------------------
# _find_awaiting_approval_run (DB-level)
# ---------------------------------------------------------------------------


class TestFindAwaitingApprovalRun:
    """DB-level tests for the handoff router helper."""

    async def test_finds_most_recent(self) -> None:
        from sova.dashboard.services.handoff_service import find_awaiting_approval_run

        async with await get_session() as session, session.begin():
            older = TaskRun(issue_number="90", role="researcher", status="awaiting_approval")
            newer = TaskRun(issue_number="90", role="researcher", status="awaiting_approval")
            session.add_all([older, newer])
        await session.commit()

        result = await find_awaiting_approval_run("90")
        assert result == newer.id

    async def test_returns_none_when_no_match(self) -> None:
        from sova.dashboard.services.handoff_service import find_awaiting_approval_run

        result = await find_awaiting_approval_run("nonexistent")
        assert result is None

    async def test_ignores_non_awaiting_runs(self) -> None:
        from sova.dashboard.services.handoff_service import find_awaiting_approval_run

        async with await get_session() as session, session.begin():
            run = TaskRun(issue_number="91", role="researcher", status="done")
            session.add(run)
        await session.commit()

        result = await find_awaiting_approval_run("91")
        assert result is None


# ---------------------------------------------------------------------------
# invalidate_synthesis_cache
# ---------------------------------------------------------------------------


class TestExecuteHandoffActionBranches:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        from sova.dashboard.services import handoff_service

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

    async def test_execute_claude_command_action(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        synthesized = {
            "source": "pr-review-state",
            "issue": "42",
            "pr_number": 99,
            "next_actions": [
                {
                    "id": "integrate",
                    "label": "Integrate PR",
                    "mode": "claude-command",
                    "command": "/integrate-pr 99",
                    "args": {"issue": "42", "pr": 99},
                },
            ],
        }
        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.get_synthesized_handoff",
            AsyncMock(return_value=synthesized),
        )
        start = AsyncMock(return_value={"status": "started", "run_id": 2})
        monkeypatch.setattr("sova.dashboard.services.control_service.start_command", start)
        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.invalidate_synthesis_cache",
            lambda *a: None,
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "integrate"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "Integrate PR"
        # The stored slash form reaches start_command() as the catalog name it validates.
        start.assert_awaited_once_with("integrate-pr", {"issue": "42", "pr": 99}, _skip_handoff_clear=True)

    async def test_execute_unknown_claude_command_is_refused(
        self, client: AsyncClient, monkeypatch, project_agents, fake_runtime
    ) -> None:
        from unittest.mock import AsyncMock

        synthesized = {
            "source": "pr-review-state",
            "issue": "42",
            "pr_number": 99,
            "next_actions": [
                {
                    "id": "next",
                    "label": "Next step",
                    "mode": "claude-command",
                    "command": "/not-a-real-command 99",
                    "args": {"issue": "42", "pr": 99},
                },
            ],
        }
        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.get_synthesized_handoff",
            AsyncMock(return_value=synthesized),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "next"})

        # The same input refusal is a 400 here as on /agents/command.
        assert resp.status_code == 400
        assert "not-a-real-command" in resp.json()["detail"]
        assert fake_runtime.spawn_calls == []

    async def test_execute_shell_action_rejected(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        synthesized = {
            "source": "test",
            "next_actions": [
                {"id": "shell_action", "label": "Run Shell", "mode": "shell", "command": "echo hi", "args": {}},
            ],
        }
        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.get_synthesized_handoff",
            AsyncMock(return_value=synthesized),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "shell_action"})
        assert resp.status_code == 400
        assert "Shell mode not yet supported" in resp.json()["detail"]

    async def test_execute_unknown_mode_rejected(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        synthesized = {
            "source": "test",
            "next_actions": [
                {"id": "weird", "label": "Weird", "mode": "unknown_mode", "command": "x", "args": {}},
            ],
        }
        monkeypatch.setattr(
            "sova.dashboard.services.handoff_service.get_synthesized_handoff",
            AsyncMock(return_value=synthesized),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "weird"})
        assert resp.status_code == 400
        assert "Unknown execution type" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Resume from approval: POST /api/agents/{run_id}/resume-from-approval
# ---------------------------------------------------------------------------


class TestResumeFromApproval:
    """Tests for the approval resume endpoint and service function."""

    async def test_resume_success(self) -> None:
        """Successful resume spawns a new agent with resume_run_id."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        # Seed a TaskRun with awaiting_approval status
        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="50",
                    role="developer",
                    status="awaiting_approval",
                    pr_number=10,
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        with patch(
            "sova.dashboard.services.agent_lifecycle.start_agent",
            new_callable=AsyncMock,
            return_value={"status": "started", "pid": 999, "run_id": run_id + 1},
        ) as mock_start:
            result = await resume_from_approval(run_id)

        assert result["run_id"] == run_id + 1
        assert result["resumed_from"] == run_id
        assert result["issue"] == "50"
        assert result["role"] == "developer"
        mock_start.assert_awaited_once()
        call_kwargs = mock_start.call_args
        assert call_kwargs.kwargs["resume_run_id"] == run_id
        assert call_kwargs.kwargs["pr_number"] == 10
        assert call_kwargs.kwargs["force"] is True

    async def test_resume_not_found(self) -> None:
        """Resuming a non-existent run returns not_found error."""
        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        result = await resume_from_approval(999999)
        assert result["error"] == "not_found"

    async def test_resume_wrong_status(self) -> None:
        """Resuming a run with wrong status returns conflict error."""
        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="51",
                    role="developer",
                    status="done",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        result = await resume_from_approval(run_id)
        assert result["error"] == "conflict"
        assert "done" in result["detail"]

    async def test_resume_clears_handoff(self) -> None:
        """Successful resume clears the handoff file for the issue."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="52",
                    role="developer",
                    status="awaiting_approval",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        with (
            patch(
                "sova.dashboard.services.agent_lifecycle.start_agent",
                new_callable=AsyncMock,
                return_value={"status": "started", "pid": 111, "run_id": run_id + 1},
            ),
            patch(
                "sova.dashboard.services.handoff_service.clear_handoff",
            ) as mock_clear,
        ):
            result = await resume_from_approval(run_id)

        assert "error" not in result
        mock_clear.assert_called_once_with(issue="52")

    async def test_resume_endpoint_404(self) -> None:
        """The router endpoint returns 404 for missing run."""

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/agents/999999/resume-from-approval", headers=_csrf_request_headers(client))
        assert resp.status_code == 404

    async def test_resume_endpoint_409(self) -> None:
        """The router endpoint returns 409 for wrong status."""

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="53",
                    role="developer",
                    status="done",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/agents/{run_id}/resume-from-approval", headers=_csrf_request_headers(client)
            )
        assert resp.status_code == 409

    async def test_resume_spawn_failure_reverts_status(self) -> None:
        """When start_agent fails, the TaskRun status reverts to awaiting_approval."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="54",
                    role="developer",
                    status="awaiting_approval",
                    pr_number=11,
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        with patch(
            "sova.dashboard.services.agent_lifecycle.start_agent",
            new_callable=AsyncMock,
            return_value={"error": "Maximum concurrent agents reached (1)", "running": 1},
        ):
            result = await resume_from_approval(run_id)

        assert "error" in result
        # Verify status was reverted so the approval button reappears
        async with await get_session() as session:
            async with session.begin():
                reverted = await session.get(TaskRun, run_id)
        assert reverted.status == "awaiting_approval"

    async def test_resume_spawn_failure_preserves_handoff(self) -> None:
        """When start_agent fails, the handoff file is NOT cleared."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="55",
                    role="developer",
                    status="awaiting_approval",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        with (
            patch(
                "sova.dashboard.services.agent_lifecycle.start_agent",
                new_callable=AsyncMock,
                return_value={"error": "spawn failed"},
            ),
            patch(
                "sova.dashboard.services.handoff_service.clear_handoff",
            ) as mock_clear,
        ):
            result = await resume_from_approval(run_id)

        assert "error" in result
        mock_clear.assert_not_called()

    async def test_resume_endpoint_500_on_spawn_error(self) -> None:
        """The router returns 500 for generic spawn errors (not 404/409)."""
        from unittest.mock import AsyncMock, patch

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="56",
                    role="developer",
                    status="awaiting_approval",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        app = create_app()
        transport = ASGITransport(app=app)

        with patch(
            "sova.dashboard.services.agent_lifecycle.start_agent",
            new_callable=AsyncMock,
            return_value={"error": "spawn_failed", "detail": "Agent process died"},
        ):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/agents/{run_id}/resume-from-approval", headers=_csrf_request_headers(client)
                )

        assert resp.status_code == 500
        assert "Agent process died" in resp.json()["detail"]

    async def test_resume_idempotent_double_call(self) -> None:
        """Two concurrent resume calls: second one gets conflict due to CAS."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="57",
                    role="developer",
                    status="awaiting_approval",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        call_count = 0

        async def mock_start(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return {"status": "started", "pid": 999, "run_id": run_id + call_count}

        with patch(
            "sova.dashboard.services.agent_lifecycle.start_agent",
            new_callable=AsyncMock,
            side_effect=mock_start,
        ):
            result1 = await resume_from_approval(run_id)
            result2 = await resume_from_approval(run_id)

        # First call succeeds
        assert "error" not in result1
        # Second call gets conflict (status already changed by CAS)
        assert result2["error"] == "conflict"

    async def test_resume_start_agent_error_budget_exceeded(self) -> None:
        """When start_agent returns a budget/concurrent error, resume propagates it."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="60",
                    role="developer",
                    status="awaiting_approval",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        with patch(
            "sova.dashboard.services.agent_lifecycle.start_agent",
            new_callable=AsyncMock,
            return_value={"error": "budget_exceeded", "detail": "Run budget exceeded"},
        ):
            result = await resume_from_approval(run_id)

        assert result["error"] == "budget_exceeded"
        # Status should be reverted
        async with await get_session() as session:
            async with session.begin():
                reverted = await session.get(TaskRun, run_id)
        assert reverted.status == "awaiting_approval"

    async def test_resume_issueless_run(self) -> None:
        """Resuming a run with no issue_number still works (skips handoff clear)."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="",
                    role="developer",
                    status="awaiting_approval",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        with (
            patch(
                "sova.dashboard.services.agent_lifecycle.start_agent",
                new_callable=AsyncMock,
                return_value={"status": "started", "pid": 222, "run_id": run_id + 1},
            ),
            patch(
                "sova.dashboard.services.handoff_service.clear_handoff",
            ) as mock_clear,
        ):
            result = await resume_from_approval(run_id)

        assert "error" not in result
        assert result["run_id"] == run_id + 1
        # clear_handoff should NOT be called when issue is empty
        mock_clear.assert_not_called()

    async def test_resume_clear_handoff_exception(self) -> None:
        """When clear_handoff raises, resume still succeeds (non-fatal)."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="61",
                    role="developer",
                    status="awaiting_approval",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        with (
            patch(
                "sova.dashboard.services.agent_lifecycle.start_agent",
                new_callable=AsyncMock,
                return_value={"status": "started", "pid": 333, "run_id": run_id + 1},
            ),
            patch(
                "sova.dashboard.services.handoff_service.clear_handoff",
                side_effect=OSError("disk full"),
            ),
        ):
            result = await resume_from_approval(run_id)

        # Should succeed despite clear_handoff failure
        assert "error" not in result
        assert result["run_id"] == run_id + 1


class TestHandoffIssueFilter:
    async def test_handoff_filter_by_issue(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        handoffs = [
            {
                "source": "planner",
                "status": "awaiting_action",
                "issue": "planner",
                "summary": "Proposed 3 tasks",
                "details": {"planned_tasks": []},
                "next_actions": [],
            },
            {
                "source": "developer",
                "status": "awaiting_action",
                "issue": "42",
                "summary": "PR ready",
                "next_actions": [],
            },
        ]

        with patch("sova.dashboard.services.handoff_service.get_all_handoffs", return_value=handoffs):
            resp = await client.get("/api/handoff?issue=planner")

        assert resp.status_code == 200
        data = resp.json()
        assert data["has_handoff"] is True
        assert len(data["handoffs"]) == 1
        assert data["handoff"]["issue"] == "planner"

    async def test_handoff_filter_no_match(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        handoffs = [
            {
                "source": "developer",
                "status": "awaiting_action",
                "issue": "42",
                "summary": "PR ready",
                "next_actions": [],
            },
        ]

        with patch("sova.dashboard.services.handoff_service.get_all_handoffs", return_value=handoffs):
            resp = await client.get("/api/handoff?issue=planner")

        assert resp.status_code == 200
        data = resp.json()
        assert data["has_handoff"] is False
        assert data["handoffs"] == []
