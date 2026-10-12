"""Per-run "Internal LLM model (this run only)" override on agent start (issue #1036).

Covers the dashboard half: the ``/agents/start`` router's server-side checks
(role eligibility, discovery membership at submit time) and
``agent_lifecycle.start_agent()`` threading the validated value into only that
run's ``spawn_direct()`` child env. The ``sova run`` child half (the override
reaching ``_invoke_with_fallback``) is in tests/test_llm_fallback_loop.py.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.app import create_app
from sova.dashboard.services import agent_lifecycle
from sova.dashboard.services.control_service import ProjectAgents, start_agent
from sova.ipc.runtime import _inject_agent_marker
from sova.llm.client import RUN_MODEL_OVERRIDE_ENV

_DISCOVERY = {
    "layer": "llm",
    "provider_identity": "claude-code:firstparty",
    "models": [
        {"id": "claude-sonnet-5", "family": "anthropic", "display_name": "Sonnet 5", "tier": "sonnet"},
        {"id": "claude-haiku-4-5", "family": "anthropic", "display_name": "Haiku 4.5", "tier": "haiku"},
    ],
    "source": "enumerated",
    "detail": "",
    "cached_at": "2026-10-12T00:00:00+00:00",
    "cached": False,
}


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(multi_project=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _mock_process(pid: int = 12345) -> MagicMock:
    process = MagicMock()
    process.pid = pid

    async def _empty_async_iter():  # type: ignore[no-untyped-def]
        return
        yield

    process.stdout_lines = _empty_async_iter
    process.stderr_lines = _empty_async_iter
    process.wait = AsyncMock(return_value=0)
    return process


@contextmanager
def _spawn_harness(pa: ProjectAgents, spawn_direct: AsyncMock, *, run_ids: list[int] | None = None) -> Iterator[None]:
    ids = iter(run_ids if run_ids is not None else [7])
    with (
        patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
        patch.object(agent_lifecycle, "spawn_direct", spawn_direct),
        patch.object(
            agent_lifecycle, "_create_task_run", new_callable=AsyncMock, side_effect=lambda *a, **k: next(ids)
        ),
        patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
        patch.object(agent_lifecycle, "_resolve_repo_context_agent_enabled", return_value=False),
        patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
        patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
        patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
    ):
        yield


class TestRoleEligibility:
    @pytest.mark.parametrize("role", ["researcher", "developer", "planner", "reviewer", None])
    def test_direct_spawn_pipeline_roles_are_eligible(self, role: str | None) -> None:
        assert agent_lifecycle.llm_model_override_role_error(role) is None

    @pytest.mark.parametrize("role", ["triage", "custom", "my-custom-role", "command:integrate-pr"])
    def test_other_roles_are_rejected_with_reason(self, role: str) -> None:
        error = agent_lifecycle.llm_model_override_role_error(role)
        assert error is not None
        assert role in error

    def test_override_roles_are_the_direct_spawn_roles(self) -> None:
        """An eligible role that did not direct-spawn would silently drop the env var,
        so the eligible set is the direct-spawn set itself, not a second copy of it.
        """
        assert agent_lifecycle.LLM_MODEL_OVERRIDE_ROLES is agent_lifecycle._DIRECT_SPAWN_ROLES


class TestStartAgentService:
    async def test_override_reaches_spawn_direct_child_env(self) -> None:
        pa = ProjectAgents()
        spawn = AsyncMock(return_value=_mock_process())
        with _spawn_harness(pa, spawn):
            result = await start_agent("99", role="developer", llm_model_override="claude-haiku-4-5")

        assert result["status"] == "started"
        assert "warnings" not in result
        assert spawn.call_args.kwargs["extra_env"] == {RUN_MODEL_OVERRIDE_ENV: "claude-haiku-4-5"}
        assert RUN_MODEL_OVERRIDE_ENV not in os.environ

    async def test_no_override_leaves_extra_env_unset(self) -> None:
        pa = ProjectAgents()
        spawn = AsyncMock(return_value=_mock_process())
        with _spawn_harness(pa, spawn):
            await start_agent("99", role="developer")
        assert spawn.call_args.kwargs["extra_env"] is None

    async def test_override_never_written_to_cmd_or_settings(self) -> None:
        pa = ProjectAgents()
        spawn = AsyncMock(return_value=_mock_process())
        with (
            _spawn_harness(pa, spawn),
            patch("sova.dashboard.services.settings_service.update_config", new_callable=AsyncMock) as mock_update,
            patch(
                "sova.dashboard.services.settings_service.update_config_many", new_callable=AsyncMock
            ) as mock_update_many,
        ):
            await start_agent("99", role="researcher", llm_model_override="claude-haiku-4-5")
        mock_update.assert_not_awaited()
        mock_update_many.assert_not_awaited()
        assert "claude-haiku-4-5" not in " ".join(spawn.call_args.args[0])

    async def test_reviewer_override_merges_with_codex_env(self) -> None:
        pa = ProjectAgents()
        spawn = AsyncMock(return_value=_mock_process())
        with (
            _spawn_harness(pa, spawn),
            patch.object(agent_lifecycle, "_resolve_repo_context_agent_enabled", return_value=True),
            patch.object(agent_lifecycle, "_codex_extra_env", return_value={"CODEX_API_KEY": "k"}),
        ):
            await start_agent("99", role="reviewer", llm_model_override="claude-sonnet-5")
        assert spawn.call_args.kwargs["extra_env"] == {
            "CODEX_API_KEY": "k",
            RUN_MODEL_OVERRIDE_ENV: "claude-sonnet-5",
        }

    async def test_ineligible_role_is_rejected_before_any_side_effect(self) -> None:
        pa = ProjectAgents()
        spawn = AsyncMock(return_value=_mock_process())
        with _spawn_harness(pa, spawn), patch.object(agent_lifecycle, "_create_task_run") as mock_create:
            result = await start_agent("99", role="custom", llm_model_override="claude-sonnet-5")
        assert result["code"] == "llm_model_override_unsupported"
        assert "custom" in result["error"]
        spawn.assert_not_awaited()
        mock_create.assert_not_called()

    async def test_concurrent_runs_get_independent_overrides(self) -> None:
        """Two runs started together must each see only their own override."""
        pa = ProjectAgents()
        pa.max_concurrent = 5
        spawn = AsyncMock(side_effect=[_mock_process(1), _mock_process(2)])
        with _spawn_harness(pa, spawn, run_ids=[7, 8]):
            await asyncio.gather(
                start_agent("98", role="developer", llm_model_override="claude-sonnet-5"),
                start_agent("99", role="researcher", llm_model_override="claude-haiku-4-5"),
            )

        envs = [call.kwargs["extra_env"] for call in spawn.call_args_list]
        assert sorted(env[RUN_MODEL_OVERRIDE_ENV] for env in envs) == ["claude-haiku-4-5", "claude-sonnet-5"]
        assert envs[0] is not envs[1]
        assert RUN_MODEL_OVERRIDE_ENV not in os.environ

    def test_override_survives_the_spawn_scrub(self) -> None:
        """extra_env is layered after scrubbing, so the child really receives it."""
        child_env = _inject_agent_marker({"PATH": "/usr/bin"}, extra_env={RUN_MODEL_OVERRIDE_ENV: "claude-sonnet-5"})
        assert child_env[RUN_MODEL_OVERRIDE_ENV] == "claude-sonnet-5"
        assert RUN_MODEL_OVERRIDE_ENV not in os.environ


class TestStartAgentRouter:
    async def test_valid_override_is_passed_to_service(self, client: AsyncClient) -> None:
        with (
            patch(
                "sova.dashboard.services.models_service.get_available_models",
                new_callable=AsyncMock,
                return_value=_DISCOVERY,
            ) as mock_discovery,
            patch(
                "sova.dashboard.services.control_service.start_agent",
                new_callable=AsyncMock,
                return_value={"run_id": 1, "status": "started"},
            ) as mock_start,
        ):
            resp = await client.post(
                "/api/agents/start",
                json={"issue": "42", "role": "developer", "llm_model_override": "claude-haiku-4-5"},
            )
        assert resp.status_code == 200
        assert mock_start.call_args.kwargs["llm_model_override"] == "claude-haiku-4-5"
        assert mock_discovery.call_args.kwargs["layer"] == "llm"

    async def test_missing_override_skips_discovery(self, client: AsyncClient) -> None:
        with (
            patch(
                "sova.dashboard.services.models_service.get_available_models", new_callable=AsyncMock
            ) as mock_discovery,
            patch(
                "sova.dashboard.services.control_service.start_agent",
                new_callable=AsyncMock,
                return_value={"run_id": 1, "status": "started"},
            ) as mock_start,
        ):
            resp = await client.post("/api/agents/start", json={"issue": "42", "llm_model_override": "  "})
        assert resp.status_code == 200
        assert mock_start.call_args.kwargs["llm_model_override"] is None
        mock_discovery.assert_not_awaited()

    async def test_ineligible_role_is_400(self, client: AsyncClient) -> None:
        with patch("sova.dashboard.services.control_service.start_agent", new_callable=AsyncMock) as mock_start:
            resp = await client.post(
                "/api/agents/start",
                json={"issue": "42", "role": "triage", "llm_model_override": "claude-haiku-4-5"},
            )
        assert resp.status_code == 400
        assert "triage" in resp.json()["detail"]
        mock_start.assert_not_awaited()

    async def test_unknown_model_is_400(self, client: AsyncClient) -> None:
        """An ID that stopped being offered since page load is caught at submit time."""
        with (
            patch(
                "sova.dashboard.services.models_service.get_available_models",
                new_callable=AsyncMock,
                return_value=_DISCOVERY,
            ),
            patch("sova.dashboard.services.control_service.start_agent", new_callable=AsyncMock) as mock_start,
        ):
            resp = await client.post(
                "/api/agents/start",
                json={"issue": "42", "role": "reviewer", "llm_model_override": "gpt-4o"},
            )
        assert resp.status_code == 400
        assert "gpt-4o" in resp.json()["detail"]
        mock_start.assert_not_awaited()

    async def test_curated_discovery_is_503_and_does_not_launch(self, client: AsyncClient) -> None:
        """A curated fallback list is no answer, so it must not validate an override.

        It is what models_service returns while the provider is unauthenticated or
        misconfigured, and the launch modal already refuses to offer any model then;
        validating against it would accept an ID the provider cannot serve.
        """
        curated = {**_DISCOVERY, "source": "curated"}
        with (
            patch(
                "sova.dashboard.services.models_service.get_available_models",
                new_callable=AsyncMock,
                return_value=curated,
            ),
            patch("sova.dashboard.services.control_service.start_agent", new_callable=AsyncMock) as mock_start,
        ):
            resp = await client.post(
                "/api/agents/start",
                json={"issue": "42", "llm_model_override": "claude-haiku-4-5"},
            )
        assert resp.status_code == 503
        mock_start.assert_not_awaited()

    async def test_discovery_failure_is_503_and_does_not_launch(self, client: AsyncClient) -> None:
        with (
            patch(
                "sova.dashboard.services.models_service.get_available_models",
                new_callable=AsyncMock,
                side_effect=RuntimeError("provider down"),
            ),
            patch("sova.dashboard.services.control_service.start_agent", new_callable=AsyncMock) as mock_start,
        ):
            resp = await client.post(
                "/api/agents/start",
                json={"issue": "42", "llm_model_override": "claude-haiku-4-5"},
            )
        assert resp.status_code == 503
        mock_start.assert_not_awaited()
