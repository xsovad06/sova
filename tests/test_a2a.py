"""Tests for sova.a2a: A2A (Agent-to-Agent) protocol implementation."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.db.models import TaskRun
from sova.db.session import close_db, get_session, init_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
def project_dir(tmp_path):
    toml = tmp_path / "sova.toml"
    toml.write_text('[project]\ngithub_repo = "test/repo"\ngithub_user = "testuser"\n\n[a2a]\nenabled = true\n')
    return tmp_path


@pytest.fixture
def disabled_project_dir(tmp_path):
    toml = tmp_path / "sova.toml"
    toml.write_text('[project]\ngithub_repo = "test/repo"\ngithub_user = "testuser"\n')
    return tmp_path


@pytest.fixture
def app(project_dir):
    from sova.dashboard.app import create_app

    return create_app(project_dir=project_dir)


@pytest.fixture
def disabled_app(disabled_project_dir):
    from sova.dashboard.app import create_app

    return create_app(project_dir=disabled_project_dir)


# ---------------------------------------------------------------------------
# Agent Card generation
# ---------------------------------------------------------------------------


class TestAgentCard:
    def test_generate_agent_card_has_required_fields(self):
        from sova.a2a.agent_card import generate_agent_card

        card = generate_agent_card(endpoint_base="http://localhost:8111")
        assert card["name"] == "sova"
        assert "description" in card
        assert "skills" in card
        assert "url" in card
        assert card["url"] == "http://localhost:8111"

    def test_agent_card_includes_all_builtin_roles(self):
        from sova.a2a.agent_card import generate_agent_card
        from sova.roles.dispatcher import BUILTIN_ROLE_NAMES

        card = generate_agent_card(endpoint_base="http://localhost:8111")
        skill_names = {s["id"] for s in card["skills"]}
        for role_name in BUILTIN_ROLE_NAMES:
            assert role_name in skill_names, f"Missing skill for role: {role_name}"

    def test_agent_card_skills_have_metadata(self):
        from sova.a2a.agent_card import generate_agent_card

        card = generate_agent_card(endpoint_base="http://localhost:8111")
        for skill in card["skills"]:
            assert "id" in skill
            assert "name" in skill
            assert "description" in skill

    def test_generate_role_card(self):
        from sova.a2a.agent_card import generate_role_card

        card = generate_role_card("developer", endpoint_base="http://localhost:8111")
        assert card is not None
        assert card["name"] == "sova-developer"
        assert len(card["skills"]) == 1
        assert card["skills"][0]["id"] == "developer"

    def test_generate_role_card_unknown_role(self):
        from sova.a2a.agent_card import generate_role_card

        assert generate_role_card("nonexistent", endpoint_base="http://localhost:8111") is None


# ---------------------------------------------------------------------------
# Task state mapping
# ---------------------------------------------------------------------------


class TestTaskMapping:
    def test_sova_to_a2a_terminal_states(self):
        from sova.a2a.task_mapping import sova_status_to_a2a
        from sova.core.state import TaskStatus

        assert sova_status_to_a2a(TaskStatus.DONE) == "completed"
        assert sova_status_to_a2a(TaskStatus.FAILED) == "failed"
        assert sova_status_to_a2a(TaskStatus.REJECTED) == "failed"

    def test_sova_to_a2a_working_states(self):
        from sova.a2a.task_mapping import sova_status_to_a2a
        from sova.core.state import TaskStatus

        assert sova_status_to_a2a(TaskStatus.DEVELOPING) == "working"
        assert sova_status_to_a2a(TaskStatus.REVIEWING) == "working"
        assert sova_status_to_a2a(TaskStatus.CI_MONITORING) == "working"

    def test_sova_to_a2a_pending_states(self):
        from sova.a2a.task_mapping import sova_status_to_a2a
        from sova.core.state import TaskStatus

        assert sova_status_to_a2a(TaskStatus.PENDING) == "submitted"
        assert sova_status_to_a2a(TaskStatus.PAUSED) == "submitted"

    def test_a2a_to_sova_mapping(self):
        from sova.a2a.task_mapping import a2a_to_sova_status
        from sova.core.state import TaskStatus

        assert a2a_to_sova_status("submitted") == TaskStatus.PENDING
        assert a2a_to_sova_status("working") == TaskStatus.IN_PROGRESS
        assert a2a_to_sova_status("completed") == TaskStatus.DONE
        assert a2a_to_sova_status("failed") == TaskStatus.FAILED
        assert a2a_to_sova_status("canceled") == TaskStatus.REJECTED

    def test_a2a_to_sova_unknown_state(self):
        from sova.a2a.task_mapping import a2a_to_sova_status

        with pytest.raises(ValueError, match="Unknown A2A"):
            a2a_to_sova_status("nonsense")

    def test_task_run_to_a2a_task(self):
        from sova.a2a.task_mapping import task_run_to_a2a_task

        run = TaskRun(
            id=1,
            issue_number="42",
            role="developer",
            status="developing",
            current_step="develop",
            branch_name="feat/issue-42",
            project_slug="test",
            started_at=datetime.now(timezone.utc),
        )
        task = task_run_to_a2a_task(run)
        assert task["id"] == "sova-run-1"
        assert task["status"]["state"] == "working"
        assert task["status"]["message"] == "develop"
        assert task["metadata"]["issue_number"] == "42"
        assert task["metadata"]["role"] == "developer"
        assert task["metadata"]["sova_status"] == "developing"

    def test_task_run_to_a2a_with_interrupted_status(self):
        from sova.a2a.task_mapping import task_run_to_a2a_task

        run = TaskRun(
            id=3,
            issue_number="50",
            role="developer",
            status="interrupted",
            current_step="develop",
            branch_name="feat/issue-50",
            project_slug="test",
            started_at=datetime.now(timezone.utc),
        )
        task = task_run_to_a2a_task(run)
        assert task["status"]["state"] == "failed"

    def test_sova_status_to_a2a_interrupted_string(self):
        from sova.a2a.task_mapping import sova_status_to_a2a

        assert sova_status_to_a2a("interrupted") == "failed"

    def test_task_run_to_a2a_task_with_handoff(self):
        from sova.a2a.task_mapping import task_run_to_a2a_task

        run = TaskRun(
            id=2,
            issue_number="99",
            role="reviewer",
            status="done",
            current_step="complete",
            branch_name="feat/issue-99",
            project_slug="test",
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc),
            handoff_json={"summary": "Review complete", "next_action": "address_review"},
        )
        task = task_run_to_a2a_task(run)
        assert task["status"]["state"] == "completed"
        assert len(task["artifacts"]) == 1
        assert task["artifacts"][0]["parts"][0]["type"] == "data"


# ---------------------------------------------------------------------------
# Router endpoints
# ---------------------------------------------------------------------------


class TestA2ARouter:
    @pytest.mark.asyncio
    async def test_well_known_agent_card(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/.well-known/agent.json")
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "sova"
            assert "skills" in data
            assert "url" in data

    @pytest.mark.asyncio
    async def test_well_known_returns_404_when_disabled(self, disabled_app):
        async with AsyncClient(transport=ASGITransport(app=disabled_app), base_url="http://test") as client:
            resp = await client.get("/.well-known/agent.json")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_task_not_found(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/a2a/tasks/sova-run-999")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_task_found(self, app):
        async with await get_session() as session:
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="developing",
                current_step="develop",
                branch_name="feat/issue-42",
                total_cost_usd=Decimal("0.50"),
                project_slug="test",
                started_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/a2a/tasks/sova-run-{run_id}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["id"] == f"sova-run-{run_id}"
            assert data["status"]["state"] == "working"

    @pytest.mark.asyncio
    async def test_cancel_task_not_found(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/a2a/tasks/sova-run-999/cancel")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_tasks_return_404_when_disabled(self, disabled_app):
        async with AsyncClient(transport=ASGITransport(app=disabled_app), base_url="http://test") as client:
            resp = await client.get("/a2a/tasks")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_tasks(self, app):
        async with await get_session() as session:
            run = TaskRun(
                issue_number="10",
                role="triage",
                status="done",
                current_step="complete",
                branch_name="",
                total_cost_usd=Decimal("0.10"),
                project_slug="test",
                started_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/a2a/tasks")
            assert resp.status_code == 200
            data = resp.json()
            assert "tasks" in data
            assert len(data["tasks"]) >= 1
            assert "total" in data
            assert "limit" in data
            assert "offset" in data

    @pytest.mark.asyncio
    async def test_list_tasks_pagination(self, app):
        async with await get_session() as session:
            for i in range(5):
                session.add(
                    TaskRun(
                        issue_number=str(i),
                        role="triage",
                        status="done",
                        current_step="complete",
                        branch_name="",
                        total_cost_usd=Decimal("0.01"),
                        project_slug="test",
                        started_at=datetime.now(timezone.utc),
                    )
                )
            await session.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/a2a/tasks?limit=2&offset=0")
            data = resp.json()
            assert len(data["tasks"]) == 2
            assert data["total"] >= 5
            assert data["limit"] == 2
            assert data["offset"] == 0

            resp2 = await client.get("/a2a/tasks?limit=2&offset=2")
            data2 = resp2.json()
            assert len(data2["tasks"]) == 2
            assert data2["offset"] == 2

    @pytest.mark.asyncio
    async def test_cancel_running_task(self, app):
        async with await get_session() as session:
            run = TaskRun(
                issue_number="77",
                role="developer",
                status="developing",
                current_step="develop",
                branch_name="feat/issue-77",
                total_cost_usd=Decimal("1.00"),
                project_slug="test",
                started_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        with patch(
            "sova.dashboard.services.control_service.stop_agent",
            new_callable=AsyncMock,
        ) as mock_stop:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(f"/a2a/tasks/sova-run-{run_id}/cancel")
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"]["state"] == "canceled"
            mock_stop.assert_called_once_with(run_id=run_id)

        async with await get_session() as session:
            updated = await session.get(TaskRun, run_id)
            assert updated.status == "rejected"

    @pytest.mark.asyncio
    async def test_cancel_already_terminal_task(self, app):
        async with await get_session() as session:
            run = TaskRun(
                issue_number="78",
                role="developer",
                status="failed",
                current_step="develop",
                branch_name="feat/issue-78",
                total_cost_usd=Decimal("0.50"),
                project_slug="test",
                started_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(f"/a2a/tasks/sova-run-{run_id}/cancel")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"]["state"] == "failed"
            assert data["status"]["message"] == "Already terminal"

    @pytest.mark.asyncio
    async def test_cancel_stop_agent_failure_still_succeeds(self, app):
        async with await get_session() as session:
            run = TaskRun(
                issue_number="79",
                role="developer",
                status="developing",
                current_step="develop",
                branch_name="feat/issue-79",
                total_cost_usd=Decimal("0.10"),
                project_slug="test",
                started_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        with patch(
            "sova.dashboard.services.control_service.stop_agent",
            new_callable=AsyncMock,
            side_effect=RuntimeError("process already dead"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(f"/a2a/tasks/sova-run-{run_id}/cancel")
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"]["state"] == "canceled"

    @pytest.mark.asyncio
    async def test_role_agent_card(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/a2a/developer/agent.json")
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "sova-developer"
            assert len(data["skills"]) == 1
            assert data["skills"][0]["id"] == "developer"

    @pytest.mark.asyncio
    async def test_role_agent_card_unknown_role(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/a2a/nonexistent/agent.json")
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Multi-project mode
# ---------------------------------------------------------------------------


class TestA2AMultiProject:
    @pytest.mark.asyncio
    async def test_well_known_returns_card_without_project_context(self):
        """In multi-project mode with no project context, returns a generic card."""
        from sova.dashboard.app import create_app

        app = create_app(multi_project=True)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/.well-known/agent.json")
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "sova"
            assert "skills" in data


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestA2AConfig:
    def test_default_disabled(self):
        from sova.config.models import A2AConfig

        cfg = A2AConfig()
        assert cfg.enabled is False
        assert cfg.endpoint_base == ""

    def test_enabled_via_toml(self, project_dir):
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        assert cfg.a2a.enabled is True

    def test_disabled_by_default(self, disabled_project_dir):
        from sova.config.loader import load_config

        cfg = load_config(disabled_project_dir)
        assert cfg.a2a.enabled is False
