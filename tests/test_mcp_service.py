"""Tests for MCP service -- token generation/validation and tool handlers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sova.dashboard.services.mcp_service import (
    generate_mcp_token,
    get_budget,
    get_gate_results,
    get_issue_context,
    get_pr_status,
    get_run_status,
    list_run_history,
    validate_mcp_token,
)
from sova.db.models import StepExecution, TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for MCP tests."""
    import os

    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
async def session() -> AsyncSession:
    return await get_session()


@pytest.fixture
async def seed_runs(session: AsyncSession):
    """Create test task runs for MCP tool testing."""
    now = datetime.now(timezone.utc)

    run1 = TaskRun(
        issue_number="100",
        role="developer",
        status="running",
        current_step="develop",
        branch_name="feat/mcp",
        pr_number=50,
        total_cost_usd=Decimal("2.50"),
        project_slug="test-proj",
        started_at=now - timedelta(hours=1),
    )
    run2 = TaskRun(
        issue_number="100",
        role="developer",
        status="done",
        current_step="complete",
        branch_name="feat/prev",
        pr_number=49,
        total_cost_usd=Decimal("1.00"),
        project_slug="test-proj",
        started_at=now - timedelta(hours=5),
        ended_at=now - timedelta(hours=4),
    )
    run3 = TaskRun(
        issue_number="101",
        role="reviewer",
        status="running",
        current_step="review",
        pr_number=51,
        total_cost_usd=Decimal("0.25"),
        project_slug="test-proj",
        started_at=now - timedelta(minutes=10),
    )
    session.add_all([run1, run2, run3])
    await session.flush()

    # Add step executions for run1 with gate checks
    step1 = StepExecution(
        task_run_id=run1.id,
        step_name="sync",
        status="done",
        started_at=now - timedelta(hours=1),
        ended_at=now - timedelta(hours=1, minutes=-5),
        duration_ms=300000,
        gate_check_result=json.dumps({"passed": True}),
    )
    step2 = StepExecution(
        task_run_id=run1.id,
        step_name="develop",
        status="running",
        started_at=now - timedelta(minutes=30),
        gate_check_result=None,
    )
    session.add_all([step1, step2])
    await session.commit()

    return [run1, run2, run3]


def test_generate_mcp_token():
    """Token generation creates valid HMAC-signed tokens."""
    secret = "test-secret-key-12345"
    run_id = 42
    token = generate_mcp_token(run_id, secret)
    assert isinstance(token, str)
    assert len(token) > 20


def test_validate_mcp_token():
    """Token validation verifies signature and extracts run_id."""
    secret = "test-secret-key-12345"
    run_id = 42
    token = generate_mcp_token(run_id, secret)
    validated_run_id = validate_mcp_token(token, secret)
    assert validated_run_id == run_id


def test_validate_mcp_token_wrong_secret():
    """Token validation fails with wrong secret."""
    secret = "test-secret-key-12345"
    wrong_secret = "different-secret"
    run_id = 42
    token = generate_mcp_token(run_id, secret)

    with pytest.raises(ValueError, match="Invalid MCP token"):
        validate_mcp_token(token, wrong_secret)


def test_validate_mcp_token_expired():
    """Token validation fails for expired tokens."""
    import hmac
    import json
    from datetime import datetime, timedelta, timezone

    secret = "test-secret-key-12345"
    run_id = 42
    exp = datetime.now(timezone.utc) - timedelta(hours=1)
    payload = json.dumps({"run_id": run_id, "exp": exp.isoformat()}).encode()
    sig = hmac.new(secret.encode(), payload, "sha256").hexdigest()
    token = f"{payload.hex()}.{sig}"

    with pytest.raises(ValueError, match="MCP token expired"):
        validate_mcp_token(token, secret)


def test_validate_mcp_token_malformed():
    """Token validation fails for malformed tokens."""
    secret = "test-secret-key-12345"

    with pytest.raises(ValueError, match="Invalid MCP token"):
        validate_mcp_token("not-a-valid-token", secret)


@pytest.mark.asyncio
async def test_get_run_status(session: AsyncSession, seed_runs):
    """get_run_status returns current step, status, and elapsed time."""
    run1 = seed_runs[0]
    project_dir = Path("/fake/project")

    result = await get_run_status(run1.id, project_dir)

    assert result["status"] == "running"
    assert result["current_step"] == "develop"
    assert result["started_at"] is not None
    assert result["elapsed_seconds"] > 0
    # pipeline_variant is derived from role when no step history exists
    assert "pipeline_variant" in result
    assert result["pipeline_variant"] == "developer"
    assert result["role"] == "developer"
    assert result["issue_number"] == "100"


@pytest.mark.asyncio
async def test_get_run_status_not_found(session: AsyncSession):
    """get_run_status raises for nonexistent run_id."""
    project_dir = Path("/fake/project")

    with pytest.raises(ValueError, match="Run 9999 not found"):
        await get_run_status(9999, project_dir)


@pytest.mark.asyncio
async def test_get_budget(session: AsyncSession, seed_runs):
    """get_budget returns spent, limit, and remaining for run and issue."""
    run1 = seed_runs[0]
    project_dir = Path("/fake/project")

    result = await get_budget(run1.id, project_dir)

    assert isinstance(result["spent_usd"], str)
    assert Decimal(result["spent_usd"]) == Decimal("2.50")
    assert Decimal(result["run_limit_usd"]) == Decimal("10.00")
    assert Decimal(result["remaining_usd"]) == Decimal("7.50")
    assert Decimal(result["issue_total_usd"]) == Decimal("3.50")
    assert Decimal(result["issue_limit_usd"]) == Decimal("50.00")


@pytest.mark.asyncio
async def test_get_gate_results(session: AsyncSession, seed_runs):
    """get_gate_results returns step execution history with gate checks."""
    run1 = seed_runs[0]
    project_dir = Path("/fake/project")

    result = await get_gate_results(run1.id, project_dir)

    assert len(result) == 2
    sync_step = [s for s in result if s["step_name"] == "sync"][0]
    assert sync_step["status"] == "done"
    assert sync_step["duration_ms"] == 300000
    assert sync_step["gate_check_result"] == {"passed": True}


@pytest.mark.asyncio
async def test_get_pr_status(session: AsyncSession, seed_runs, monkeypatch):
    """get_pr_status returns PR state, CI checks, and review decision."""
    from dataclasses import dataclass
    from enum import StrEnum
    from unittest.mock import AsyncMock

    class _CheckStatus(StrEnum):
        COMPLETED = "completed"

    class _CheckConclusion(StrEnum):
        SUCCESS = "success"

    @dataclass
    class _PRStatus:
        number: int = 50
        state: str = "OPEN"
        mergeable: str = "MERGEABLE"
        review_decision: str = "APPROVED"
        url: str = "https://github.com/test/repo/pull/50"
        title: str = "test PR"

    @dataclass
    class _CICheck:
        name: str = "CI"
        status: _CheckStatus = _CheckStatus.COMPLETED
        conclusion: _CheckConclusion | None = _CheckConclusion.SUCCESS
        details_url: str = ""

    monkeypatch.setattr(
        "sova.dashboard.services.mcp_service._git_get_pr_status",
        AsyncMock(return_value=_PRStatus()),
    )
    monkeypatch.setattr(
        "sova.dashboard.services.mcp_service._git_get_ci_checks",
        AsyncMock(return_value=[_CICheck()]),
    )
    monkeypatch.setattr(
        "sova.dashboard.services.mcp_service.load_config",
        lambda _: type("C", (), {"github_repo": "test/repo", "github_user": "testuser"})(),
    )

    run1 = seed_runs[0]
    project_dir = Path("/fake/project")

    result = await get_pr_status(run1.id, project_dir)

    assert result["pr_number"] == 50
    assert result["pr_state"] == "OPEN"
    assert len(result["ci_checks"]) == 1
    assert result["ci_checks"][0]["name"] == "CI"
    assert result["review_decision"] == "APPROVED"


@pytest.mark.asyncio
async def test_get_pr_status_no_pr(session: AsyncSession, seed_runs):
    """get_pr_status returns null PR number when not yet created."""
    run3 = seed_runs[2]
    run3.pr_number = None
    session.add(run3)
    await session.commit()
    project_dir = Path("/fake/project")

    result = await get_pr_status(run3.id, project_dir)

    assert result["pr_number"] is None
    assert result["pr_state"] is None
    assert result["ci_checks"] == []
    assert result["review_decision"] is None


@pytest.mark.asyncio
async def test_get_issue_context(session: AsyncSession, seed_runs, monkeypatch):
    """get_issue_context returns issue body, labels, comments."""
    from dataclasses import dataclass
    from unittest.mock import AsyncMock, MagicMock

    @dataclass
    class _Task:
        title: str = "Test issue"
        body: str = "Issue body"
        labels: list = None

        def __post_init__(self):
            if self.labels is None:
                self.labels = ["bug", "priority:high"]

    @dataclass
    class _Comment:
        body: str = "A comment"
        author: str = "testuser"

    mock_adapter = MagicMock()
    mock_adapter.get_task = AsyncMock(return_value=_Task())
    mock_adapter.get_comments = AsyncMock(return_value=[_Comment()])
    monkeypatch.setattr(
        "sova.dashboard.services.mcp_service.create_adapter",
        lambda _: mock_adapter,
    )
    monkeypatch.setattr(
        "sova.dashboard.services.mcp_service.load_config",
        lambda _: MagicMock(),
    )

    run1 = seed_runs[0]
    project_dir = Path("/fake/project")
    result = await get_issue_context(run1.id, project_dir)

    assert result["issue_number"] == "100"
    assert result["title"] == "Test issue"
    assert result["body"] == "Issue body"
    assert result["labels"] == ["bug", "priority:high"]
    assert len(result["comments"]) == 1
    assert result["comments"][0]["author"] == "testuser"


@pytest.mark.asyncio
async def test_list_run_history(session: AsyncSession, seed_runs):
    """list_run_history returns all runs for the same issue."""
    run1 = seed_runs[0]
    project_dir = Path("/fake/project")

    result = await list_run_history(run1.id, project_dir)

    assert len(result) == 2
    run_ids = {r["run_id"] for r in result}
    assert seed_runs[0].id in run_ids
    assert seed_runs[1].id in run_ids
    assert seed_runs[2].id not in run_ids
    assert all(isinstance(r["cost_usd"], str) for r in result)


@pytest.mark.asyncio
async def test_list_run_history_ordered_by_start(session: AsyncSession, seed_runs):
    """list_run_history returns runs ordered newest first."""
    run1 = seed_runs[0]
    project_dir = Path("/fake/project")

    result = await list_run_history(run1.id, project_dir)

    assert result[0]["run_id"] == seed_runs[0].id
    assert result[1]["run_id"] == seed_runs[1].id
