"""Tests for sova.supervisor.planner: SupervisorPlanner LLM planning step."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sova.config.models import ProjectConfig, SupervisorConfig
from sova.dashboard.services.pr_service import list_open_prs_with_state
from sova.supervisor.planner import (
    _VALID_ACTIONS,
    DeferredAction,
    PlannedAction,
    PlanResult,
    SupervisorPlanner,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_warned_flag():
    """Reset the module-level warning flag before each test."""
    import sova.supervisor.planner as mod

    mod._warned_no_key = False
    yield
    mod._warned_no_key = False


@pytest.fixture
def config() -> ProjectConfig:
    return ProjectConfig(
        supervisor=SupervisorConfig(enabled=True, llm_planning=True),
        github_repo="test/repo",
    )


@pytest.fixture
def planner(config: ProjectConfig) -> SupervisorPlanner:
    return SupervisorPlanner(
        config=config,
        project_dir=Path("/tmp/test"),
        session_factory=MagicMock(),
    )


@pytest.fixture
def mock_adapter() -> AsyncMock:
    adapter = AsyncMock()
    adapter.list_tasks = AsyncMock(return_value=[])
    return adapter


@pytest.fixture
def valid_llm_response() -> dict:
    return {
        "reasoning": "3 PRs are approved. Merging costs no CI or CodeRabbit budget.",
        "actions": [
            {"action": "spawn_integrate", "issue": 42, "priority": 1, "reason": "Approved, CI green"},
            {"action": "spawn_developer", "issue": 17, "priority": 2, "reason": "Highest-priority researched issue"},
        ],
        "deferred": [
            {"action": "spawn_developer", "issue": 23, "reason": "CodeRabbit budget: only 1 review left"},
        ],
    }


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_planned_action_frozen(self) -> None:
        a = PlannedAction(action="spawn_developer", issue=42, priority=1, reason="test")
        assert a.action == "spawn_developer"
        assert a.issue == 42
        with pytest.raises(AttributeError):
            a.action = "other"  # type: ignore[misc]

    def test_deferred_action_frozen(self) -> None:
        d = DeferredAction(action="spawn_developer", issue=23, reason="budget")
        assert d.issue == 23
        with pytest.raises(AttributeError):
            d.issue = 99  # type: ignore[misc]

    def test_plan_result_defaults(self) -> None:
        p = PlanResult(reasoning="test")
        assert p.actions == ()
        assert p.deferred == ()

    def test_plan_result_frozen(self) -> None:
        p = PlanResult(reasoning="test")
        with pytest.raises(AttributeError):
            p.reasoning = "other"  # type: ignore[misc]

    def test_plan_result_with_actions(self) -> None:
        actions = (PlannedAction(action="spawn_developer", issue=1, priority=1, reason="r"),)
        deferred = (DeferredAction(action="spawn_researcher", issue=2, reason="d"),)
        p = PlanResult(reasoning="plan", actions=actions, deferred=deferred)
        assert len(p.actions) == 1
        assert len(p.deferred) == 1


class TestValidActions:
    def test_valid_actions_set(self) -> None:
        expected = {"spawn_researcher", "spawn_developer", "spawn_integrate", "spawn_address_review", "spawn_rebase"}
        assert _VALID_ACTIONS == expected


# ---------------------------------------------------------------------------
# SupervisorPlanner.plan() tests
# ---------------------------------------------------------------------------


class TestPlanNoApiKey:
    async def test_returns_none_without_api_key(self, planner: SupervisorPlanner, mock_adapter: AsyncMock) -> None:
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            result = await planner.plan(mock_adapter)
        assert result is None

    async def test_logs_once_without_api_key(self, planner: SupervisorPlanner, mock_adapter: AsyncMock) -> None:
        import sova.supervisor.planner as mod

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            await planner.plan(mock_adapter)
            assert mod._warned_no_key is True
            await planner.plan(mock_adapter)


class TestPlanLLMCall:
    async def test_successful_plan(
        self, planner: SupervisorPlanner, mock_adapter: AsyncMock, valid_llm_response: dict
    ) -> None:
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch.object(planner, "_assemble_context", new_callable=AsyncMock, return_value="context"),
            patch.object(planner, "_load_persona", return_value="persona"),
            patch.object(planner, "_call_llm", new_callable=AsyncMock, return_value=valid_llm_response),
        ):
            result = await planner.plan(mock_adapter)

        assert result is not None
        assert result.reasoning == "3 PRs are approved. Merging costs no CI or CodeRabbit budget."
        assert len(result.actions) == 2
        assert result.actions[0].action == "spawn_integrate"
        assert result.actions[0].issue == 42
        assert result.actions[0].priority == 1
        assert len(result.deferred) == 1
        assert result.deferred[0].issue == 23

    async def test_llm_timeout_returns_none(self, planner: SupervisorPlanner, mock_adapter: AsyncMock) -> None:
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch.object(planner, "_assemble_context", new_callable=AsyncMock, return_value="context"),
            patch.object(planner, "_load_persona", return_value="persona"),
            patch.object(planner, "_call_llm", new_callable=AsyncMock, return_value=None),
        ):
            result = await planner.plan(mock_adapter)
        assert result is None

    async def test_exception_returns_none(self, planner: SupervisorPlanner, mock_adapter: AsyncMock) -> None:
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch.object(planner, "_assemble_context", new_callable=AsyncMock, side_effect=RuntimeError("boom")),
        ):
            result = await planner.plan(mock_adapter)
        assert result is None

    async def test_persona_with_curly_braces(
        self, planner: SupervisorPlanner, mock_adapter: AsyncMock, valid_llm_response: dict
    ) -> None:
        persona_with_braces = "Use {this} pattern and {{that}} pattern"
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch.object(planner, "_assemble_context", new_callable=AsyncMock, return_value="context"),
            patch.object(planner, "_load_persona", return_value=persona_with_braces),
            patch.object(planner, "_call_llm", new_callable=AsyncMock, return_value=valid_llm_response) as mock_call,
        ):
            result = await planner.plan(mock_adapter)
        assert result is not None
        system_prompt = mock_call.call_args[0][1]
        assert "{{this}}" in system_prompt
        assert "{{{{that}}}}" in system_prompt


class TestCallLLM:
    async def test_successful_call(self, planner: SupervisorPlanner) -> None:
        response_data = {
            "content": [{"text": '{"reasoning": "test", "actions": [], "deferred": []}'}],
        }
        mock_response = MagicMock()
        mock_response.json.return_value = response_data
        mock_response.raise_for_status = MagicMock()

        with patch("sova.supervisor.planner.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await planner._call_llm("test-key", "system", "user")

        assert result == {"reasoning": "test", "actions": [], "deferred": []}

    async def test_timeout_returns_none(self, planner: SupervisorPlanner) -> None:
        with patch("sova.supervisor.planner.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await planner._call_llm("test-key", "system", "user")
        assert result is None

    async def test_http_error_returns_none(self, planner: SupervisorPlanner) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("error", request=MagicMock(), response=mock_response)
        )

        with patch("sova.supervisor.planner.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await planner._call_llm("test-key", "system", "user")
        assert result is None

    async def test_json_parse_error_returns_none(self, planner: SupervisorPlanner) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"content": [{"text": "not valid json"}]}
        mock_response.raise_for_status = MagicMock()

        with patch("sova.supervisor.planner.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await planner._call_llm("test-key", "system", "user")
        assert result is None


class TestParseResponse:
    def test_valid_response(self, planner: SupervisorPlanner, valid_llm_response: dict) -> None:
        result = planner._parse_response(valid_llm_response)
        assert result is not None
        assert result.reasoning == valid_llm_response["reasoning"]
        assert len(result.actions) == 2
        assert len(result.deferred) == 1

    def test_missing_reasoning_returns_none(self, planner: SupervisorPlanner) -> None:
        result = planner._parse_response({"actions": [], "deferred": []})
        assert result is None

    def test_empty_reasoning_returns_none(self, planner: SupervisorPlanner) -> None:
        result = planner._parse_response({"reasoning": "", "actions": []})
        assert result is None

    def test_invalid_action_name_dropped(self, planner: SupervisorPlanner) -> None:
        raw = {
            "reasoning": "test",
            "actions": [
                {"action": "invalid_action", "issue": 1, "priority": 1, "reason": "bad"},
                {"action": "spawn_developer", "issue": 2, "priority": 2, "reason": "good"},
            ],
        }
        result = planner._parse_response(raw)
        assert result is not None
        assert len(result.actions) == 1
        assert result.actions[0].issue == 2

    def test_invalid_issue_number_dropped(self, planner: SupervisorPlanner) -> None:
        raw = {
            "reasoning": "test",
            "actions": [
                {"action": "spawn_developer", "issue": -1, "priority": 1, "reason": "bad"},
                {"action": "spawn_developer", "issue": "not_a_number", "priority": 1, "reason": "bad"},
            ],
        }
        result = planner._parse_response(raw)
        assert result is not None
        assert len(result.actions) == 0

    def test_missing_priority_defaults_to_index(self, planner: SupervisorPlanner) -> None:
        raw = {
            "reasoning": "test",
            "actions": [
                {"action": "spawn_developer", "issue": 1, "reason": "no priority"},
            ],
        }
        result = planner._parse_response(raw)
        assert result is not None
        assert result.actions[0].priority == 1

    def test_missing_deferred_defaults_to_empty(self, planner: SupervisorPlanner) -> None:
        raw = {"reasoning": "test", "actions": []}
        result = planner._parse_response(raw)
        assert result is not None
        assert result.deferred == ()

    def test_empty_actions_is_valid(self, planner: SupervisorPlanner) -> None:
        raw = {"reasoning": "do nothing this cycle", "actions": [], "deferred": []}
        result = planner._parse_response(raw)
        assert result is not None
        assert len(result.actions) == 0


class TestContextAssembly:
    async def test_assembles_all_sections(self, planner: SupervisorPlanner, mock_adapter: AsyncMock) -> None:
        with (
            patch.object(
                planner, "_get_resource_snapshot", new_callable=AsyncMock, return_value="## Resource Snapshot"
            ),
            patch.object(planner, "_get_open_prs", new_callable=AsyncMock, return_value="## Open PRs"),
            patch.object(planner, "_get_issue_counts", new_callable=AsyncMock, return_value="## Issue Counts"),
            patch.object(planner, "_get_priority_queue", return_value="## Priority Queue"),
            patch.object(planner, "_get_recent_failures", new_callable=AsyncMock, return_value="## Recent Failures"),
            patch.object(planner, "_get_issue_health", new_callable=AsyncMock, return_value="## Issue Health"),
        ):
            result = await planner._assemble_context(mock_adapter)

        assert "## Resource Snapshot" in result
        assert "## Open PRs" in result
        assert "## Issue Counts" in result
        assert "## Priority Queue" in result
        assert "## Recent Failures" in result
        assert "## Issue Health" in result

    def test_priority_queue_with_items(self, planner: SupervisorPlanner) -> None:
        planner._config = ProjectConfig(
            supervisor=SupervisorConfig(task_queue=[42, 17, 23]),
            github_repo="test/repo",
        )
        result = planner._get_priority_queue()
        assert "#42" in result
        assert "#17" in result
        assert "#23" in result

    def test_priority_queue_empty(self, planner: SupervisorPlanner) -> None:
        result = planner._get_priority_queue()
        assert "No explicit task queue configured" in result


class TestSanitizeError:
    def test_none_returns_unknown(self) -> None:
        from sova.supervisor.planner import _sanitize_error

        assert _sanitize_error(None) == "unknown"

    def test_empty_returns_unknown(self) -> None:
        from sova.supervisor.planner import _sanitize_error

        assert _sanitize_error("") == "unknown"

    def test_normal_message_passes_through(self) -> None:
        from sova.supervisor.planner import _sanitize_error

        result = _sanitize_error("budget exceeded after 3 attempts")
        assert result == "budget exceeded after 3 attempts"

    def test_sensitive_content_redacted(self) -> None:
        from sova.supervisor.planner import _sanitize_error

        result = _sanitize_error("Failed: api_key=sk-abc123 in request")
        assert "sk-abc123" not in result
        assert "[REDACTED]" in result

    def test_authorization_header_redacted(self) -> None:
        from sova.supervisor.planner import _sanitize_error

        result = _sanitize_error("Error: Authorization: Bearer eyJabc123 in response")
        assert "eyJabc123" not in result
        assert "[REDACTED]" in result

    def test_standalone_bearer_redacted(self) -> None:
        from sova.supervisor.planner import _sanitize_error

        result = _sanitize_error("Got 401: Bearer eyJabc123 was invalid")
        assert "eyJabc123" not in result
        assert "[REDACTED]" in result

    def test_truncated_at_max_length(self) -> None:
        from sova.supervisor.planner import _sanitize_error

        long_msg = "x" * 200
        result = _sanitize_error(long_msg)
        assert len(result) <= 124  # 120 + "..."
        assert result.endswith("...")


# ---------------------------------------------------------------------------
# Context-gathering method tests (coverage for _get_resource_snapshot,
# _get_open_prs, _get_issue_counts, _get_recent_failures, _load_persona)
# ---------------------------------------------------------------------------


class TestLoadPersona:
    def test_delegates_to_persona_module(self, planner: SupervisorPlanner) -> None:
        with patch("sova.supervisor.planner.load_persona", create=True) as mock_load:
            mock_load.return_value = "Be strategic."
            # Patch the import path inside the method
            with patch("sova.supervisor.persona.load_persona", mock_load):
                result = planner._load_persona()
        assert result == "Be strategic."


class TestGetResourceSnapshot:
    async def test_happy_path(self, planner: SupervisorPlanner) -> None:
        mock_status = MagicMock(is_limited=False, hits_in_window=5, cooldown_remaining_seconds=0.0)
        mock_tracker = MagicMock()
        mock_tracker.get_status.return_value = mock_status

        mock_quota = MagicMock(reviews_in_window=2, reviews_per_hour=4, can_create_pr=True, next_available_minutes=None)

        mock_budget = MagicMock(used=100, total=2000, pct_used=5.0, remaining=1900)
        mock_ci_tracker = MagicMock()
        mock_ci_tracker.get_budget = AsyncMock(return_value=mock_budget)

        mock_session = AsyncMock()
        planner._session_factory = MagicMock(return_value=mock_session)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("sova.supervisor.github_quota.get_github_quota_tracker", return_value=mock_tracker),
            patch("sova.supervisor.coderabbit_quota.get_quota_status", new_callable=AsyncMock, return_value=mock_quota),
            patch("sova.supervisor.ci_budget.get_ci_budget_tracker", return_value=mock_ci_tracker),
        ):
            result = await planner._get_resource_snapshot()

        assert "## Resource Snapshot" in result
        assert "GitHub API" in result
        assert "CodeRabbit" in result
        assert "CI Budget" in result
        assert "Agent Slots" in result

    async def test_all_sources_fail(self, planner: SupervisorPlanner) -> None:
        with (
            patch(
                "sova.supervisor.github_quota.get_github_quota_tracker",
                side_effect=RuntimeError("no tracker"),
            ),
            patch(
                "sova.supervisor.coderabbit_quota.get_quota_status",
                new_callable=AsyncMock,
                side_effect=RuntimeError("no quota"),
            ),
            patch(
                "sova.supervisor.ci_budget.get_ci_budget_tracker",
                side_effect=RuntimeError("no ci"),
            ),
        ):
            result = await planner._get_resource_snapshot()

        assert "data unavailable" in result
        assert "Agent Slots" in result


class TestGetOpenPRs:
    async def test_successful_pr_list(self, planner: SupervisorPlanner) -> None:
        pr_data = [
            {"number": 42, "title": "feat: add feature", "computed_state": "approved_ci_green", "author": "user1"},
        ]
        with patch(
            "sova.supervisor.planner.list_open_prs_with_state",
            new_callable=AsyncMock,
            spec=list_open_prs_with_state,
            return_value=pr_data,
        ):
            result = await planner._get_open_prs()

        assert "## Open PRs" in result
        assert "#42" in result
        assert "approved_ci_green" in result

    async def test_no_open_prs(self, planner: SupervisorPlanner) -> None:
        with patch(
            "sova.supervisor.planner.list_open_prs_with_state",
            new_callable=AsyncMock,
            spec=list_open_prs_with_state,
            return_value=[],
        ):
            result = await planner._get_open_prs()

        assert "No open PRs" in result

    async def test_exception_returns_unavailable(self, planner: SupervisorPlanner) -> None:
        with patch(
            "sova.supervisor.planner.list_open_prs_with_state",
            new_callable=AsyncMock,
            spec=list_open_prs_with_state,
            side_effect=OSError("fail"),
        ):
            result = await planner._get_open_prs()

        assert "PR data unavailable" in result


class TestGetIssueCounts:
    async def test_counts_by_state(self, planner: SupervisorPlanner, mock_adapter: AsyncMock) -> None:
        from sova.adapters.base import Task, TaskState

        mock_adapter.list_tasks.return_value = [
            Task(id="1", title="A", body="", state=TaskState.BACKLOG, labels=[]),
            Task(id="2", title="B", body="", state=TaskState.BACKLOG, labels=[]),
            Task(id="3", title="C", body="", state=TaskState.IN_PROGRESS, labels=[]),
        ]
        result = await planner._get_issue_counts(mock_adapter)

        assert "## Issue Counts by State" in result
        assert "backlog: 2" in result
        assert "in_progress: 1" in result

    async def test_no_issues(self, planner: SupervisorPlanner, mock_adapter: AsyncMock) -> None:
        mock_adapter.list_tasks.return_value = []
        result = await planner._get_issue_counts(mock_adapter)
        assert "No issues found" in result

    async def test_adapter_error(self, planner: SupervisorPlanner, mock_adapter: AsyncMock) -> None:
        mock_adapter.list_tasks.side_effect = RuntimeError("API error")
        result = await planner._get_issue_counts(mock_adapter)
        assert "Data unavailable" in result


class TestGetRecentFailures:
    @pytest.fixture
    async def db_planner(self, monkeypatch: pytest.MonkeyPatch) -> SupervisorPlanner:
        """Planner with a real in-memory SQLite session factory."""
        monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
        from sova.db.session import close_db, get_session_factory, init_db

        project_dir = Path("/tmp/test-planner-failures")
        project_dir.mkdir(exist_ok=True)
        await init_db(project_dir)
        sf = await get_session_factory(project_dir)
        cfg = ProjectConfig(
            supervisor=SupervisorConfig(enabled=True, llm_planning=True),
            github_repo="test/repo",
        )
        p = SupervisorPlanner(config=cfg, project_dir=project_dir, session_factory=sf)
        yield p
        await close_db()

    async def test_with_failures(self, db_planner: SupervisorPlanner) -> None:
        from datetime import datetime, timezone

        from sova.db.models import TaskRun

        async with db_planner._session_factory() as session:
            async with session.begin():
                session.add(
                    TaskRun(
                        issue_number="42",
                        role="developer",
                        status="failed",
                        error_message="budget exceeded",
                        started_at=datetime.now(timezone.utc),
                    )
                )

        result = await db_planner._get_recent_failures()

        assert "## Recent Failures (24h)" in result
        assert "#42" in result
        assert "developer" in result
        assert "budget exceeded" in result

    async def test_no_failures(self, db_planner: SupervisorPlanner) -> None:
        result = await db_planner._get_recent_failures()
        assert "No failures in the last 24 hours" in result

    async def test_db_error(self, planner: SupervisorPlanner) -> None:
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=RuntimeError("db error"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        planner._session_factory = MagicMock(return_value=mock_session)

        result = await planner._get_recent_failures()
        assert "Data unavailable" in result


class TestGetIssueHealth:
    @pytest.fixture
    async def db_planner(self, monkeypatch: pytest.MonkeyPatch) -> SupervisorPlanner:
        """Planner with a real in-memory SQLite session factory."""
        monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
        from sova.db.session import close_db, get_session_factory, init_db

        project_dir = Path("/tmp/test-planner-health")
        project_dir.mkdir(exist_ok=True)
        await init_db(project_dir)
        sf = await get_session_factory(project_dir)
        cfg = ProjectConfig(
            supervisor=SupervisorConfig(enabled=True, llm_planning=True, task_queue=[42, 17]),
            github_repo="test/repo",
        )
        p = SupervisorPlanner(config=cfg, project_dir=project_dir, session_factory=sf)
        yield p
        await close_db()

    async def test_with_developer_runs(self, db_planner: SupervisorPlanner) -> None:
        from datetime import datetime, timedelta, timezone
        from decimal import Decimal

        from sova.db.models import CostRecord, TaskRun

        now = datetime.now(timezone.utc)
        async with db_planner._session_factory() as session:
            async with session.begin():
                session.add_all(
                    [
                        TaskRun(
                            issue_number="42",
                            role="developer",
                            status="failed",
                            error_message="test failed",
                            started_at=now - timedelta(hours=4),
                        ),
                        TaskRun(
                            issue_number="42",
                            role="developer",
                            status="failed",
                            error_message="lint error",
                            started_at=now - timedelta(hours=3),
                        ),
                        TaskRun(
                            issue_number="42",
                            role="developer",
                            status="done",
                            started_at=now - timedelta(hours=2),
                        ),
                        TaskRun(
                            issue_number="17",
                            role="developer",
                            status="failed",
                            error_message="timeout",
                            started_at=now - timedelta(hours=1),
                        ),
                        CostRecord(
                            issue="42",
                            phase="develop",
                            model="sonnet",
                            cost_usd=Decimal("1.25"),
                            recorded_at=now - timedelta(hours=3),
                        ),
                        CostRecord(
                            issue="42",
                            phase="develop",
                            model="sonnet",
                            cost_usd=Decimal("0.75"),
                            recorded_at=now - timedelta(hours=2),
                        ),
                        CostRecord(
                            issue="42",
                            phase="develop",
                            model="sonnet",
                            cost_usd=Decimal("5.00"),
                            recorded_at=now - timedelta(days=45),
                        ),
                    ]
                )

        result = await db_planner._get_issue_health()

        assert "## Issue Health (last 30 days)" in result
        assert "#42" in result
        assert "#17" in result
        assert "2 failed" in result
        assert "1 succeeded" in result
        assert "timeout" in result
        assert "$2.00" in result
        assert "$5.00" not in result
        assert "lint error" in result

    async def test_no_developer_runs(self, db_planner: SupervisorPlanner) -> None:
        result = await db_planner._get_issue_health()
        assert "## Issue Health" in result
        assert "No developer runs for queued issues" in result

    async def test_empty_task_queue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
        from sova.db.session import close_db, get_session_factory, init_db

        project_dir = Path("/tmp/test-planner-health-empty")
        project_dir.mkdir(exist_ok=True)
        await init_db(project_dir)
        sf = await get_session_factory(project_dir)
        cfg = ProjectConfig(
            supervisor=SupervisorConfig(enabled=True, llm_planning=True, task_queue=[]),
            github_repo="test/repo",
        )
        p = SupervisorPlanner(config=cfg, project_dir=project_dir, session_factory=sf)

        result = await p._get_issue_health()
        assert "No task queue configured" in result
        await close_db()

    async def test_db_error(self) -> None:
        cfg = ProjectConfig(
            supervisor=SupervisorConfig(enabled=True, llm_planning=True, task_queue=[42]),
            github_repo="test/repo",
        )
        p = SupervisorPlanner(config=cfg, project_dir=Path("/tmp/test"), session_factory=MagicMock())

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=RuntimeError("db error"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        p._session_factory = MagicMock(return_value=mock_session)

        result = await p._get_issue_health()
        assert "Data unavailable" in result


class TestParseResponseEdgeCases:
    def test_deferred_invalid_issue_dropped(self, planner: SupervisorPlanner) -> None:
        raw = {
            "reasoning": "test",
            "actions": [],
            "deferred": [
                {"action": "spawn_developer", "issue": -1, "reason": "bad"},
                {"action": "spawn_developer", "issue": "not_a_number", "reason": "bad"},
                {"action": "spawn_developer", "issue": 42, "reason": "good"},
            ],
        }
        result = planner._parse_response(raw)
        assert result is not None
        assert len(result.deferred) == 1
        assert result.deferred[0].issue == 42

    def test_invalid_priority_defaults(self, planner: SupervisorPlanner) -> None:
        raw = {
            "reasoning": "test",
            "actions": [
                {"action": "spawn_developer", "issue": 1, "priority": -5, "reason": "bad priority"},
                {"action": "spawn_developer", "issue": 2, "priority": "not_int", "reason": "string priority"},
            ],
        }
        result = planner._parse_response(raw)
        assert result is not None
        assert result.actions[0].priority == 1
        assert result.actions[1].priority == 2

    def test_queue_removals_parsed(self, planner: SupervisorPlanner) -> None:
        raw = {
            "reasoning": "prune stale issues",
            "actions": [],
            "queue_removals": [10, 20],
        }
        result = planner._parse_response(raw)
        assert result is not None
        assert result.queue_removals == (10, 20)

    def test_queue_reorder_parsed(self, planner: SupervisorPlanner) -> None:
        raw = {
            "reasoning": "reorder by priority",
            "actions": [],
            "queue_reorder": [30, 10, 20],
        }
        result = planner._parse_response(raw)
        assert result is not None
        assert result.queue_reorder == (30, 10, 20)

    def test_queue_fields_default_empty(self, planner: SupervisorPlanner) -> None:
        raw = {"reasoning": "no queue changes", "actions": []}
        result = planner._parse_response(raw)
        assert result is not None
        assert result.queue_removals == ()
        assert result.queue_reorder == ()

    def test_queue_removals_invalid_items_dropped(self, planner: SupervisorPlanner) -> None:
        raw = {
            "reasoning": "test",
            "actions": [],
            "queue_removals": [-1, "bad", 0, 42],
        }
        result = planner._parse_response(raw)
        assert result is not None
        assert result.queue_removals == (42,)

    def test_queue_removals_rejects_booleans(self, planner: SupervisorPlanner) -> None:
        raw = {
            "reasoning": "test",
            "actions": [],
            "queue_removals": [True, False, 5],
        }
        result = planner._parse_response(raw)
        assert result is not None
        assert result.queue_removals == (5,)

    def test_queue_reorder_rejects_booleans(self, planner: SupervisorPlanner) -> None:
        raw = {
            "reasoning": "test",
            "actions": [],
            "queue_reorder": [True, 10, False, 20],
        }
        result = planner._parse_response(raw)
        assert result is not None
        assert result.queue_reorder == (10, 20)
