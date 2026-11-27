"""Agents, batch and planner API routes.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import TaskRun

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


# ---------------------------------------------------------------------------
# Control API
# ---------------------------------------------------------------------------


class TestAgentsInterruptedAPI:
    async def test_interrupted_runs_returns_interrupted(self, client: AsyncClient, session: AsyncSession) -> None:
        """Interrupted TaskRuns should appear in the interrupted endpoint."""
        async with session.begin():
            run = TaskRun(
                issue_number="67",
                role="developer",
                status="interrupted",
                pid=99999,
                error_message="Dashboard restarted while agent was running",
            )
            session.add(run)
        resp = await client.get("/api/agents/interrupted")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["interrupted"]) == 1
        assert data["interrupted"][0]["issue_number"] == "67"

    async def test_interrupted_runs_excludes_other_statuses(self, client: AsyncClient, session: AsyncSession) -> None:
        async with session.begin():
            session.add(TaskRun(issue_number="1", role="dev", status="done"))
            session.add(TaskRun(issue_number="2", role="dev", status="failed"))
            session.add(TaskRun(issue_number="3", role="dev", status="running"))
        resp = await client.get("/api/agents/interrupted")
        assert resp.status_code == 200
        assert len(resp.json()["interrupted"]) == 0

    async def test_dismiss_interrupted_marks_as_failed(self, client: AsyncClient, session: AsyncSession) -> None:
        """Dismissing interrupted runs should change their status to failed."""
        run1 = TaskRun(issue_number="73", role="developer", status="interrupted", pid=99999)
        run2 = TaskRun(issue_number="73", role="developer", status="interrupted", pid=99998)
        async with session.begin():
            session.add(run1)
            session.add(run2)
            session.add(TaskRun(issue_number="42", role="auto", status="done"))

        resp = await client.post("/api/agents/interrupted/dismiss", json={"run_ids": [run1.id, run2.id]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["dismissed"] == 2

        resp2 = await client.get("/api/agents/interrupted")
        assert resp2.json()["interrupted"] == []

    async def test_dismiss_interrupted_scoped_to_given_ids(self, client: AsyncClient, session: AsyncSession) -> None:
        """Dismissal must not touch an interrupted run outside the banner's displayed ids."""
        run1 = TaskRun(issue_number="73", role="developer", status="interrupted", pid=99999)
        run2 = TaskRun(issue_number="74", role="developer", status="interrupted", pid=99998)
        async with session.begin():
            session.add(run1)
            session.add(run2)

        resp = await client.post("/api/agents/interrupted/dismiss", json={"run_ids": [run1.id]})
        assert resp.status_code == 200
        assert resp.json()["dismissed"] == 1

        resp2 = await client.get("/api/agents/interrupted")
        remaining = resp2.json()["interrupted"]
        assert len(remaining) == 1
        assert remaining[0]["issue_number"] == "74"

    async def test_dismiss_interrupted_empty_run_ids_rejected(self, client: AsyncClient) -> None:
        """An empty run_ids list must be a validation error, not a silent no-op success."""
        resp = await client.post("/api/agents/interrupted/dismiss", json={"run_ids": []})
        assert resp.status_code == 422

    async def test_dismiss_interrupted_missing_run_ids_rejected(self, client: AsyncClient) -> None:
        resp = await client.post("/api/agents/interrupted/dismiss", json={})
        assert resp.status_code == 422

    async def test_dismiss_interrupted_ignores_non_interrupted_run(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        """Dismissing a run id that isn't actually interrupted must not mark it failed."""
        run = TaskRun(issue_number="80", role="developer", status="done")
        async with session.begin():
            session.add(run)

        resp = await client.post("/api/agents/interrupted/dismiss", json={"run_ids": [run.id]})
        assert resp.status_code == 200
        assert resp.json()["dismissed"] == 0

    async def test_dismiss_interrupted_superseded_by_relaunch_keeps_error_message(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        """A relaunch-triggered dismiss preserves the original interruption diagnostic."""
        run = TaskRun(
            issue_number="80",
            role="developer",
            status="interrupted",
            error_message="Stale run recovered on startup (was 'developing')",
        )
        async with session.begin():
            session.add(run)

        resp = await client.post(
            "/api/agents/interrupted/dismiss",
            json={"run_ids": [run.id], "superseded_by_relaunch": True},
        )
        assert resp.status_code == 200
        assert resp.json()["dismissed"] == 1

        await session.refresh(run)
        assert run.status == "failed"
        assert run.error_message == "Stale run recovered on startup (was 'developing')"

    async def test_dismiss_interrupted_db_error_returns_503(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A swallowed DB error must not be reported as a 200 no-op success."""

        async def _fail(*a, **kw):
            return -1

        monkeypatch.setattr(
            "sova.dashboard.services.agent_recovery.dismiss_interrupted_runs",
            _fail,
        )
        resp = await client.post("/api/agents/interrupted/dismiss", json={"run_ids": [1]})
        assert resp.status_code == 503

    async def test_interrupted_runs_reports_total(self, client: AsyncClient, session: AsyncSession) -> None:
        """The `total` count must reflect all interrupted runs, not just the capped display limit."""
        now = datetime.now(timezone.utc)
        async with session.begin():
            for i in range(7):
                session.add(
                    TaskRun(
                        issue_number=str(100 + i),
                        role="developer",
                        status="interrupted",
                        started_at=now - timedelta(minutes=7 - i),
                    )
                )

        resp = await client.get("/api/agents/interrupted")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 7
        assert len(data["interrupted"]) == 5
        # The returned rows are the most recent ones: those are what the
        # displayed ids (and thus the scoped dismiss action) can act on.
        shown_issues = {run["issue_number"] for run in data["interrupted"]}
        assert shown_issues == {str(100 + i) for i in range(2, 7)}


class TestBatchAPI:
    """Tests for the batch queue API endpoints."""

    async def test_start_batch_rejects_empty_issues(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/queue/batch",
            json={"issues": [], "action": "triage"},
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["loc"][-1] == "issues"

    async def test_start_batch_rejects_non_numeric_issue(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/queue/batch",
            json={"issues": ["abc"], "action": "triage"},
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["loc"][-1] == "issues"

    async def test_batch_status_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/queue/batch/nonexistent/status")
        assert resp.status_code == 404
        data = resp.json()
        assert "detail" in data

    async def test_start_batch_invalid_action(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/queue/batch",
            json={"issues": ["1"], "action": "invalid_action"},
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["loc"][-1] == "action"

    async def test_cancel_nonexistent_batch(self, client: AsyncClient) -> None:
        resp = await client.post("/api/queue/batch/nonexistent/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cancelled"] is False

    async def test_active_batch_none(self, client: AsyncClient) -> None:
        resp = await client.get("/api/queue/batch/active")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is False

    async def test_active_batch_returns_running(self, client: AsyncClient) -> None:
        from sova.dashboard.services.batch_service import BatchJob, _active_batches

        _active_batches["test_active"] = BatchJob(batch_id="test_active", action="triage", status="running", results=[])
        try:
            resp = await client.get("/api/queue/batch/active")
            assert resp.status_code == 200
            data = resp.json()
            assert data["active"] is True
            assert data["batch"]["batch_id"] == "test_active"
        finally:
            _active_batches.pop("test_active", None)

    async def test_start_from_queue_rejects_non_numeric_issue(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/queue/start/abc",
            json={},
        )
        assert resp.status_code == 422

    async def test_start_from_queue_rejects_empty_role(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/queue/start/123",
            json={"role": "   "},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["loc"][-1] == "role"


class TestAgentsAPI:
    """Tests for the new agents API endpoints."""

    async def test_start_agent_rejects_empty_issue_without_role(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/agents/start",
            json={"issue": ""},
        )

        assert resp.status_code == 400

    async def test_start_agent_rejects_non_numeric_issue(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/agents/start",
            json={"issue": "abc"},
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["loc"][-1] == "issue"

    async def test_start_agent_allows_empty_issue_with_role(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "sova.dashboard.services.control_service.start_agent",
            new_callable=AsyncMock,
            return_value={"run_id": 1, "status": "started"},
        ):
            resp = await client.post(
                "/api/agents/start",
                json={"issue": "", "role": "planner"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("status") == "started"

    async def test_start_agent_rejects_empty_role(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/agents/start",
            json={"issue": "123", "role": "   "},
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["loc"][-1] == "role"

    async def test_start_agent_accepts_custom_role(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "sova.dashboard.services.control_service.start_agent",
            new_callable=AsyncMock,
            return_value={"run_id": 1, "status": "started"},
        ):
            resp = await client.post(
                "/api/agents/start",
                json={"issue": "123", "role": "custom-workflow"},
            )
            assert resp.status_code == 200

    async def test_work_items_returns_unified_payload(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        mock_result = {
            "items": [{"issue_number": "42", "state": "triaged"}],
            "running_count": 0,
            "slots_available": 3,
            "max_concurrent": 3,
        }
        with patch(
            "sova.dashboard.services.work_item_service.get_work_items",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = await client.get("/api/agents/work-items")
            assert resp.status_code == 200
            data = resp.json()
            assert "items" in data
            assert data["items"][0]["issue_number"] == "42"
            assert data["running_count"] == 0
            assert data["max_concurrent"] == 3

    async def test_next_tasks_returns_ranked_payload(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        mock_result = {
            "tasks": [{"id": 7, "rank": 1, "why": ["priority:high", "state:researched"]}],
            "unclassified_count": 2,
        }
        with patch(
            "sova.dashboard.services.work_item_service.get_next_tasks",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_next:
            resp = await client.get("/api/agents/next-tasks")
        assert resp.status_code == 200
        assert resp.json() == mock_result
        assert mock_next.await_args.kwargs == {"limit": 5}

    async def test_next_tasks_passes_the_limit_through(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "sova.dashboard.services.work_item_service.get_next_tasks",
            new_callable=AsyncMock,
            return_value={"tasks": [], "unclassified_count": 0},
        ) as mock_next:
            resp = await client.get("/api/agents/next-tasks?limit=12")
        assert resp.status_code == 200
        assert mock_next.await_args.kwargs == {"limit": 12}

    @pytest.mark.parametrize("limit", ["0", "-1", "51", "abc"])
    async def test_next_tasks_rejects_an_invalid_limit(self, client: AsyncClient, limit: str) -> None:
        resp = await client.get(f"/api/agents/next-tasks?limit={limit}")
        assert resp.status_code == 422

    async def test_next_tasks_empty_pool_is_200_not_an_error(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "sova.dashboard.services.work_item_service.get_next_tasks",
            new_callable=AsyncMock,
            return_value={"tasks": [], "unclassified_count": 0},
        ):
            resp = await client.get("/api/agents/next-tasks")
        assert resp.status_code == 200
        assert resp.json() == {"tasks": [], "unclassified_count": 0}

    async def test_next_tasks_config_failure_is_503_not_an_empty_list(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.work_item_service import GraphConfigError

        with patch(
            "sova.dashboard.services.work_item_service.get_next_tasks",
            new_callable=AsyncMock,
            side_effect=GraphConfigError("bad config"),
        ):
            resp = await client.get("/api/agents/next-tasks")
        assert resp.status_code == 503
        assert "tasks" not in resp.json()

    async def test_next_tasks_graph_failure_is_500_not_an_empty_list(self, client_no_raise: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "sova.dashboard.services.work_item_service.get_next_tasks",
            new_callable=AsyncMock,
            side_effect=RuntimeError("tracker down"),
        ):
            resp = await client_no_raise.get("/api/agents/next-tasks")
        assert resp.status_code == 500
        assert resp.json() == {"detail": "Internal error"}

    async def test_agents_active_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agents/active")
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data
        assert "completed" in data
        assert "max_concurrent" in data
        assert "slots_available" in data
        assert isinstance(data["agents"], list)
        assert data["agents"] == []

    async def test_agents_active_zero_cost_same_precision_running_and_completed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zero-cost running agent and a zero-cost completed agent must serialize cost_usd
        with the same precision. Before #1051's shared _format_cost_usd() helper, the running
        branch's pool-tracked-float fallback produced "0" (via Decimal(str(0))) while the
        completed branch produced the same "0", but either could drift independently of the
        DB-column branch's "0.000000" (a Numeric(10,6) read via decimal_to_json()) since all
        three were serialized by hand at each call site.
        """
        from unittest.mock import MagicMock

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, CompletedAgent, ProjectAgents

        mock_process = MagicMock()
        mock_process.pid = 88888
        agent = AgentState(run_id=8001, issue="800", role="developer", process=mock_process, last_result_cost=0.0)

        pa = ProjectAgents()
        pa.agents[8001] = agent
        pa.recently_completed.append(
            CompletedAgent(run_id=8002, issue="801", role="developer", status="done", cost=0.0)
        )

        monkeypatch.setattr(agent_lifecycle, "_get_project_agents", lambda slug=None: pa)

        result = await agent_lifecycle.get_all_agents()
        assert result["agents"][0]["cost_usd"] == "0.000000"
        assert result["completed"][0]["cost_usd"] == "0.000000"

    async def test_agents_interrupted_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agents/interrupted")
        assert resp.status_code == 200
        data = resp.json()
        assert "interrupted" in data

    async def test_agents_pipeline(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agents/pipeline")
        assert resp.status_code == 200
        data = resp.json()
        assert "steps" in data
        assert "sync" in data["steps"]
        assert "handoff_to_reviewer" in data["steps"]

    async def test_agents_output_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agents/999/output?since=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["lines"] == []

    async def test_pr_status_no_project_dir(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agents/issue/42/pr-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_pr"] is False

    async def test_pr_status_with_pr(self, client: AsyncClient, tmp_path: Path) -> None:
        from dataclasses import dataclass
        from unittest.mock import AsyncMock, patch

        @dataclass
        class FakePRInfo:
            number: int = 10
            url: str = "https://github.com/test/repo/pull/10"

        @dataclass
        class FakePRStatus:
            number: int = 10
            state: str = "OPEN"
            mergeable: str = "MERGEABLE"
            review_decision: str = "APPROVED"
            url: str = "https://github.com/test/repo/pull/10"
            title: str = "Test PR"
            is_approved: bool = True
            is_mergeable: bool = True

        @dataclass
        class FakeConfig:
            github_repo: str = "test/repo"
            github_user: str = "testuser"

        with (
            patch("sova.config.context.get_project_dir", return_value=str(tmp_path)),
            patch("sova.dashboard.services.agent_recovery.load_config", return_value=FakeConfig()),
            patch("sova.git.operations.find_pr_for_issue", new_callable=AsyncMock, return_value=FakePRInfo()),
            patch("sova.git.operations.get_pr_status", new_callable=AsyncMock, return_value=FakePRStatus()),
            patch("sova.git.operations.get_ci_checks", new_callable=AsyncMock, return_value=[]),
            patch("sova.git.operations.get_pr_head_sha", new_callable=AsyncMock, return_value=""),
            patch("sova.git.pr.get_pr_review_data", new_callable=AsyncMock, return_value={}),
        ):
            resp = await client.get("/api/agents/issue/42/pr-status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["has_pr"] is True
        assert data["pr_number"] == 10
        assert data["is_approved"] is True
        assert data["ci_status"] == "none"
        assert data["review_decision"] == "APPROVED"

    async def test_pr_status_no_pr_found(self, client: AsyncClient, tmp_path: Path) -> None:
        from dataclasses import dataclass
        from unittest.mock import AsyncMock, patch

        @dataclass
        class FakeConfig:
            github_repo: str = "test/repo"
            github_user: str = "testuser"

        with (
            patch("sova.config.context.get_project_dir", return_value=str(tmp_path)),
            patch("sova.dashboard.services.agent_recovery.load_config", return_value=FakeConfig()),
            patch("sova.git.operations.find_pr_for_issue", new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.get("/api/agents/issue/99/pr-status")

        assert resp.status_code == 200
        assert resp.json()["has_pr"] is False

    async def test_run_command_endpoint(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import control_service as cs

        with patch.object(
            cs, "start_command", new_callable=AsyncMock, return_value={"status": "started", "run_id": 1, "pid": 123}
        ):
            resp = await client.post("/api/agents/command", json={"command": "integrate-pr", "args": {"pr": 73}})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"
        assert data["run_id"] == 1

    @pytest.mark.parametrize(
        "body",
        [
            {"command": "../../etc/passwd"},
            {"command": "standup", "args": {"model": "other"}},
            {"command": "standup", "args": {"issue": ["42"]}},
        ],
    )
    async def test_run_command_rejects_invalid_input(self, client: AsyncClient, fake_runtime, body: dict) -> None:
        """An unknown command name or a non-allowlisted argument is a 400, and nothing starts.

        _resolve_command_prompt falls back to sending a bare "/{command}" to the
        headless agent for anything it cannot resolve to a real file, and the
        same unvalidated string is also used to build a filesystem path, so an
        unknown value is both a prompt-injection and a path-traversal vector.
        start_command() validates both, so the route answers from its refusal.
        """
        from sova.dashboard.services import action_registry

        action_registry.reset_actions()
        resp = await client.post("/api/agents/command", json=body)

        assert resp.status_code == 400
        assert fake_runtime.spawn_calls == []
        # Rejected before tracked_action(), so bad input is not a failed action in the history.
        assert action_registry.list_actions(None) == []


# ---------------------------------------------------------------------------
# _detect_variant / _detect_variant_from_steps: command role coverage
# ---------------------------------------------------------------------------


class TestDetectVariantCommandRoles:
    """Tests for command/standalone role detection in work_service."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"role": "command:integrate-pr"}, id="command-integrate"),
            pytest.param({"role": "command:review-pr"}, id="command-review-pr"),
            pytest.param({"role": "reviewer"}, id="reviewer-standalone"),
            pytest.param({"role": "reviewer", "pr_number": 42}, id="reviewer-with-pr"),
        ],
    )
    def test_detect_variant_is_command(self, kwargs: dict[str, object]) -> None:
        from sova.dashboard.services.work_service import _detect_variant

        assert _detect_variant(None, **kwargs) == "command"

    @pytest.mark.parametrize(
        "role",
        [
            pytest.param("command:integrate-pr", id="command-role"),
            pytest.param("reviewer", id="reviewer"),
        ],
    )
    def test_detect_variant_from_steps_is_command(self, role: str) -> None:
        from sova.dashboard.services.work_service import _detect_variant_from_steps

        assert _detect_variant_from_steps([], None, role=role) == "command"

    def test_detect_variant_from_steps_developer_not_command(self) -> None:
        from sova.dashboard.services.work_service import _detect_variant_from_steps

        assert _detect_variant_from_steps([], None, role="developer") != "command"


# ---------------------------------------------------------------------------
# Planner create-issues endpoint
# ---------------------------------------------------------------------------


class TestPlannerCreateIssues:
    async def test_create_issues_success(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.adapters.base import Task, TaskState

        mock_adapter = AsyncMock()
        mock_adapter.create_issue.return_value = Task(
            id="200", title="feat(cli): new cmd", state=TaskState.BACKLOG, url="https://example.test/issues/200"
        )

        with patch("sova.adapters.create_adapter", return_value=mock_adapter):
            resp = await client.post(
                "/api/agents/planner/create-issues",
                json={
                    "tasks": [
                        {"title": "feat(cli): new cmd", "description": "A new command"},
                    ]
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 1
        assert data["created"][0]["number"] == "200"
        assert data["created"][0]["url"] == "https://example.test/issues/200"
        assert "errors" not in data

    async def test_create_issues_empty_list(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/agents/planner/create-issues",
            json={"tasks": []},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] == []

    async def test_create_issues_partial_failure(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.adapters.base import Task, TaskState

        mock_adapter = AsyncMock()
        call_count = 0

        async def _create_issue(title: str, body: str = "", labels=None, **_kw):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Rate limited")
            return Task(
                id=str(100 + call_count),
                title=title,
                state=TaskState.BACKLOG,
                url=f"https://example.test/issues/{100 + call_count}",
            )

        mock_adapter.create_issue.side_effect = _create_issue

        with patch("sova.adapters.create_adapter", return_value=mock_adapter):
            resp = await client.post(
                "/api/agents/planner/create-issues",
                json={
                    "tasks": [
                        {"title": "Task 1", "description": "Desc 1"},
                        {"title": "Task 2", "description": "Desc 2"},
                        {"title": "Task 3", "description": "Desc 3"},
                    ]
                },
            )

        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert detail["message"] == "Some issues could not be created on the tracker"
        results = detail["results"]
        assert [r["ok"] for r in results] == [True, False, True]
        assert results[1] == {"ok": False, "title": "Task 2", "error": "Issue creation failed"}
        # The created entries carry the number and url so a caller can reconcile without re-creating them.
        assert [(r["number"], r["url"]) for r in (results[0], results[2])] == [
            ("101", "https://example.test/issues/101"),
            ("103", "https://example.test/issues/103"),
        ]
        assert "Rate limited" not in resp.text

    async def test_create_issues_jira_project_sends_priority_natively_and_no_labels(self, client: AsyncClient) -> None:
        """Jira rejects any label containing a space; a Jira-configured project
        must not forward the GitHub-style 'area: x' / 'priority: y' strings
        the client used to build itself. The priority goes to Jira's native field instead.
        """
        from unittest.mock import AsyncMock, patch

        from sova.adapters.base import Task, TaskState
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        cfg.task_source.type = "jira"

        mock_adapter = AsyncMock()
        mock_adapter.create_issue.return_value = Task(id="200", title="feat(cli): new cmd", state=TaskState.BACKLOG)

        with (
            patch("sova.dashboard.services.planner_issue_service.load_config", return_value=cfg),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
        ):
            resp = await client.post(
                "/api/agents/planner/create-issues",
                json={
                    "tasks": [
                        {
                            "title": "feat(cli): new cmd",
                            "description": "A new command",
                            "component": "cli",
                            "priority": "high",
                        },
                    ]
                },
            )

        assert resp.status_code == 200
        assert mock_adapter.create_issue.call_count == 1
        _, kwargs = mock_adapter.create_issue.call_args
        assert kwargs["labels"] is None
        assert kwargs["priority"] == "high"


class TestPlannerTaskLabels:
    """Direct unit tests for planner_task_labels(), the function that turns
    a planner-proposed task into GitHub label strings. Only the endpoint-level
    behavior (Jira sends no labels) was covered before; these pin the
    GitHub-side label construction and its validation against the catalog.
    """

    def test_github_component_and_priority(self) -> None:
        from sova.dashboard.routers.agents import PlannedTaskRequest
        from sova.dashboard.services.planner_issue_service import planner_task_labels

        task = PlannedTaskRequest(title="t", component="dashboard", priority="high")
        assert planner_task_labels(task, is_github=True) == ["area: dashboard", "priority: high"]

    @pytest.mark.parametrize(
        ("component", "priority", "expected"),
        [
            # A component outside the catalog's area vocabulary (e.g. the planner LLM echoing a prompt example that
            # isn't a real area) is dropped and logged rather than becoming a label gh rejects the whole issue over.
            pytest.param("orchestrator", "high", ["priority: high"], id="unknown-component-dropped"),
            pytest.param("dashboard", "urgent", ["area: dashboard"], id="unknown-priority-dropped"),
        ],
    )
    def test_github_unknown_value_dropped(self, component: str, priority: str, expected: list[str]) -> None:
        from sova.dashboard.routers.agents import PlannedTaskRequest
        from sova.dashboard.services.planner_issue_service import planner_task_labels

        task = PlannedTaskRequest(title="t", component=component, priority=priority)
        assert planner_task_labels(task, is_github=True) == expected

    def test_github_no_fields_returns_none(self) -> None:
        from sova.dashboard.routers.agents import PlannedTaskRequest
        from sova.dashboard.services.planner_issue_service import planner_task_labels

        task = PlannedTaskRequest(title="t")
        assert planner_task_labels(task, is_github=True) is None

    @pytest.mark.parametrize(
        ("priority", "expected"),
        [("High ", "high"), ("undefined", "undefined"), ("", "undefined"), (" ", "undefined"), ("urgent", "undefined")],
    )
    def test_priority_for_jiras_native_field(self, priority: str, expected: str) -> None:
        from sova.dashboard.routers.agents import PlannedTaskRequest
        from sova.dashboard.services.planner_issue_service import planner_task_priority

        assert planner_task_priority(PlannedTaskRequest(title="t", priority=priority)) == expected

    def test_non_github_returns_none(self) -> None:
        from sova.dashboard.routers.agents import PlannedTaskRequest
        from sova.dashboard.services.planner_issue_service import planner_task_labels

        task = PlannedTaskRequest(title="t", component="dashboard", priority="high")
        assert planner_task_labels(task, is_github=False) is None
