"""Tests for the outbound telemetry push (fire-and-forget after finalization)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.config.models import ProjectConfig, TelemetryConfig
from sova.db.models import StepExecution, TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _make_cfg(**telemetry_overrides) -> ProjectConfig:
    tel = TelemetryConfig(**telemetry_overrides)
    return ProjectConfig(telemetry=tel)


async def _seed_run(
    *,
    status: str = "done",
    role: str = "developer",
    error_message: str | None = None,
    cost: float = 0.05,
    project_slug: str = "test-project",
    ended_at: datetime | None = None,
    steps: list[tuple[str, str]] | None = None,
) -> int:
    """Insert a TaskRun with optional StepExecution records. Returns run ID."""
    started = datetime(2026, 7, 26, 10, 0, 0, tzinfo=timezone.utc)
    async with await get_session() as session, session.begin():
        run = TaskRun(
            role=role,
            status=status,
            error_message=error_message,
            total_cost_usd=Decimal(str(cost)),
            project_slug=project_slug,
            started_at=started,
            ended_at=ended_at,
        )
        session.add(run)
        await session.flush()
        run_id = run.id

        if steps:
            for i, (name, st) in enumerate(steps):
                se = StepExecution(
                    task_run_id=run_id,
                    step_name=name,
                    status=st,
                    started_at=datetime(2026, 7, 26, 10, i, 0, tzinfo=timezone.utc),
                )
                session.add(se)

    return run_id


@pytest.mark.asyncio
class TestPushTelemetry:
    async def test_push_sends_payload(self) -> None:
        from sova.dashboard.services.telemetry_push import push_telemetry

        run_id = await _seed_run(
            steps=[("sync", "done"), ("develop", "done")],
            ended_at=datetime(2026, 7, 26, 10, 14, 7, tzinfo=timezone.utc),
        )
        cfg = _make_cfg(hub_url="https://hub.example.com", hub_token="secret123")

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await push_telemetry(run_id, Path.cwd(), cfg)

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        url = call_args[0][0]
        payload = call_args[1]["json"]
        headers = call_args[1]["headers"]

        assert url == "https://hub.example.com/api/telemetry/ingest"
        assert payload["role"] == "developer"
        assert payload["status"] == "done"
        assert Decimal(payload["cost_usd"]) == Decimal("0.05")
        assert payload["step_outcomes"] == {"sync": "done", "develop": "done"}
        assert payload["duration_seconds"] == pytest.approx(847.0)
        assert payload["exit_step"] is None
        assert payload["run_at"] is not None
        assert headers["Authorization"] == "Bearer secret123"

    async def test_no_auth_header_when_token_empty(self) -> None:
        from sova.dashboard.services.telemetry_push import push_telemetry

        run_id = await _seed_run()
        cfg = _make_cfg(hub_url="https://hub.example.com", hub_token="")

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await push_telemetry(run_id, Path.cwd(), cfg)

        headers = mock_client.post.call_args[1]["headers"]
        assert "Authorization" not in headers

    async def test_exception_swallowed(self) -> None:
        from sova.dashboard.services.telemetry_push import push_telemetry

        run_id = await _seed_run()
        cfg = _make_cfg(hub_url="https://hub.example.com")

        with patch("httpx.AsyncClient", side_effect=RuntimeError("connection refused")):
            # Should not raise
            await push_telemetry(run_id, Path.cwd(), cfg)

    async def test_server_error_swallowed(self) -> None:
        import httpx

        from sova.dashboard.services.telemetry_push import push_telemetry

        run_id = await _seed_run()
        cfg = _make_cfg(hub_url="https://hub.example.com")

        mock_resp = MagicMock(status_code=500)
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "500 Internal Server Error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            )
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            # Should not raise despite 500 response
            await push_telemetry(run_id, Path.cwd(), cfg)

    async def test_duration_none_when_ended_at_missing(self) -> None:
        from sova.dashboard.services.telemetry_push import push_telemetry

        run_id = await _seed_run(ended_at=None)
        cfg = _make_cfg(hub_url="https://hub.example.com")

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await push_telemetry(run_id, Path.cwd(), cfg)

        payload = mock_client.post.call_args[1]["json"]
        assert payload["duration_seconds"] is None

    async def test_exit_step_from_failed_step(self) -> None:
        from sova.dashboard.services.telemetry_push import push_telemetry

        run_id = await _seed_run(
            status="failed",
            error_message="timeout after 120s",
            steps=[("sync", "done"), ("develop", "done"), ("monitor_ci", "failed")],
        )
        cfg = _make_cfg(hub_url="https://hub.example.com")

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await push_telemetry(run_id, Path.cwd(), cfg)

        payload = mock_client.post.call_args[1]["json"]
        assert payload["exit_step"] == "monitor_ci"
        assert payload["failure_message"] == "timeout after 120s"

    async def test_run_not_found_returns_silently(self) -> None:
        from sova.dashboard.services.telemetry_push import push_telemetry

        cfg = _make_cfg(hub_url="https://hub.example.com")
        # Non-existent run_id
        await push_telemetry(99999, Path.cwd(), cfg)


@pytest.mark.asyncio
class TestMachineIdDerivation:
    async def test_configured_machine_id_used(self) -> None:
        from sova.dashboard.services.telemetry_push import _derive_machine_id

        assert _derive_machine_id("my-machine-01") == "my-machine-01"

    async def test_auto_derived_when_empty(self) -> None:
        from sova.dashboard.services.telemetry_push import _derive_machine_id

        result = _derive_machine_id("")
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    async def test_auto_derived_deterministic(self) -> None:
        from sova.dashboard.services.telemetry_push import _derive_machine_id

        a = _derive_machine_id("")
        b = _derive_machine_id("")
        assert a == b


@pytest.mark.asyncio
class TestProjectSlugFallback:
    async def test_empty_project_slug_uses_dir_name(self) -> None:
        from sova.dashboard.services.telemetry_push import push_telemetry

        run_id = await _seed_run(project_slug="")
        cfg = _make_cfg(hub_url="https://hub.example.com")

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        project_dir = Path("/tmp/my-cool-project")
        with patch("httpx.AsyncClient", return_value=mock_client):
            await push_telemetry(run_id, project_dir, cfg)

        payload = mock_client.post.call_args[1]["json"]
        assert payload["project_slug"] == "my-cool-project"

    async def test_failure_message_truncated(self) -> None:
        from sova.dashboard.services.telemetry_push import push_telemetry

        long_msg = "x" * 1000
        run_id = await _seed_run(status="failed", error_message=long_msg)
        cfg = _make_cfg(hub_url="https://hub.example.com")

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await push_telemetry(run_id, Path.cwd(), cfg)

        payload = mock_client.post.call_args[1]["json"]
        assert len(payload["failure_message"]) == 500
