"""Tests for sova.utils.env: spawn-boundary environment sanitization."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from sova.config import models as config_models
from sova.config.models import ProjectConfig
from sova.utils.env import (
    ANTHROPIC_CREDENTIAL_VARS,
    CODEX_CREDENTIAL_VARS,
    OPENAI_CREDENTIAL_VARS,
    PARENT_SESSION_VARS,
    PROVIDER_ROUTING_VARS,
    PYTEST_WORKERS_ENV_KEY,
    SCRUBBED_VARS,
    SERVICE_MANAGER_VARS,
    WORKER_CAP_SOURCE_COMPUTED,
    WORKER_CAP_SOURCE_CONFIG,
    WORKER_CAP_SOURCE_ENV_KEY,
    auto_workers,
    pytest_worker_count,
    scrub_agent_env,
    scrub_check_env,
    worker_shares,
)


class TestScrubAgentEnv:
    def test_removes_vertex_routing_vars(self) -> None:
        """The reported failure mode: inherited Vertex vars override the provider."""
        env = {
            "PATH": "/usr/bin",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_VERTEX_PROJECT_ID": "itpc-ca-1e0b0000a3",
            "CLOUD_ML_REGION": "global",
        }
        result = scrub_agent_env(env)
        assert result == {"PATH": "/usr/bin"}

    def test_removes_bedrock_routing_vars(self) -> None:
        env = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_BEARER_TOKEN_BEDROCK": "tok", "HOME": "/home/x"}
        assert scrub_agent_env(env) == {"HOME": "/home/x"}

    def test_removes_gateway_and_auth_skip_vars(self) -> None:
        """These can redirect Claude Code to an arbitrary gateway or skip provider auth."""
        env = {
            "ANTHROPIC_BASE_URL": "https://evil.example.com",
            "ANTHROPIC_VERTEX_BASE_URL": "https://evil.example.com",
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
            "CLAUDE_CODE_SKIP_VERTEX_AUTH": "1",
            "HOME": "/home/x",
        }
        assert scrub_agent_env(env) == {"HOME": "/home/x"}

    def test_removes_model_pinning_vars(self) -> None:
        """SOVA owns model selection through llm.routing; inherited pins override it."""
        env = {"ANTHROPIC_MODEL": "claude-3-opus", "ANTHROPIC_SMALL_FAST_MODEL": "claude-3-haiku"}
        assert scrub_agent_env(env) == {}

    def test_removes_parent_session_vars(self) -> None:
        """Starting the server inside a Claude Code session must not leak that session."""
        env = {
            "CLAUDECODE": "1",
            "CLAUDE_CODE_SESSION_ID": "abc-123",
            "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
            "LANG": "en_US.UTF-8",
        }
        assert scrub_agent_env(env) == {"LANG": "en_US.UTF-8"}

    def test_preserves_anthropic_api_key(self) -> None:
        """The anthropic provider reads this from the environment; scrubbing breaks it."""
        env = {"ANTHROPIC_API_KEY": "sk-ant-xxx", "CLAUDE_CODE_USE_VERTEX": "1"}
        assert scrub_agent_env(env) == {"ANTHROPIC_API_KEY": "sk-ant-xxx"}

    def test_passthrough_preserves_named_vars(self) -> None:
        """Deployments deliberately on Vertex opt back in via agent.env_passthrough."""
        env = {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "proj"}
        result = scrub_agent_env(env, passthrough=["CLAUDE_CODE_USE_VERTEX"])
        assert result == {"CLAUDE_CODE_USE_VERTEX": "1"}

    def test_passthrough_ignores_blank_entries(self) -> None:
        env = {"CLAUDE_CODE_USE_VERTEX": "1", "PATH": "/bin"}
        assert scrub_agent_env(env, passthrough=["", "  ", None]) == {"PATH": "/bin"}  # type: ignore[list-item]

    def test_passthrough_tolerates_surrounding_whitespace(self) -> None:
        env = {"CLOUD_ML_REGION": "global"}
        assert scrub_agent_env(env, passthrough=["  CLOUD_ML_REGION  "]) == {"CLOUD_ML_REGION": "global"}

    def test_defaults_to_process_environment(self) -> None:
        with patch.dict("os.environ", {"CLAUDE_CODE_USE_VERTEX": "1"}, clear=False):
            assert "CLAUDE_CODE_USE_VERTEX" not in scrub_agent_env()

    def test_returns_a_copy(self) -> None:
        env = {"PATH": "/bin"}
        result = scrub_agent_env(env)
        result["INJECTED"] = "1"
        assert "INJECTED" not in env

    def test_empty_env_is_handled(self) -> None:
        assert scrub_agent_env({}) == {}

    def test_never_logs_values(self) -> None:
        """Scrubbed values may be credentials; only names are safe to log."""
        env = {"AWS_BEARER_TOKEN_BEDROCK": "super-secret-token"}
        with patch("sova.utils.env.log") as mock_log:
            scrub_agent_env(env)
            _, kwargs = mock_log.info.call_args
        assert "super-secret-token" not in repr(kwargs)
        assert kwargs["removed"] == ["AWS_BEARER_TOKEN_BEDROCK"]

    def test_no_log_when_nothing_scrubbed(self) -> None:
        with patch("sova.utils.env.log") as mock_log:
            scrub_agent_env({"PATH": "/bin"})
        mock_log.info.assert_not_called()

    def test_removes_codex_api_key_by_default(self) -> None:
        """A CODEX_API_KEY meant for Codex automation must not leak into every spawn."""
        env = {"CODEX_API_KEY": "sk-codex-secret", "PATH": "/bin"}
        assert scrub_agent_env(env) == {"PATH": "/bin"}

    def test_removes_openai_api_key_by_default(self) -> None:
        """OPENAI_API_KEY is an in-process litellm credential; it must never reach a child."""
        env = {"OPENAI_API_KEY": "sk-openai-secret", "PATH": "/bin"}
        assert scrub_agent_env(env) == {"PATH": "/bin"}

    def test_passthrough_cannot_readmit_codex_api_key(self) -> None:
        """A credential can never be opted back in via agent.env_passthrough."""
        env = {"CODEX_API_KEY": "sk-codex-secret", "PATH": "/bin"}
        assert scrub_agent_env(env, passthrough=["CODEX_API_KEY"]) == {"PATH": "/bin"}

    def test_passthrough_cannot_readmit_openai_api_key(self) -> None:
        """A credential can never be opted back in via agent.env_passthrough."""
        env = {"OPENAI_API_KEY": "sk-openai-secret", "PATH": "/bin"}
        assert scrub_agent_env(env, passthrough=["OPENAI_API_KEY"]) == {"PATH": "/bin"}

    def test_extra_scrub_removes_named_vars(self) -> None:
        env = {"ANTHROPIC_API_KEY": "sk-ant-secret", "PATH": "/bin"}
        result = scrub_agent_env(env, extra_scrub=ANTHROPIC_CREDENTIAL_VARS)
        assert result == {"PATH": "/bin"}

    def test_extra_scrub_wins_over_passthrough(self) -> None:
        """extra_scrub is a per-spawn hard exclusion; env_passthrough cannot re-admit it."""
        env = {"ANTHROPIC_API_KEY": "sk-ant-secret"}
        result = scrub_agent_env(env, passthrough=["ANTHROPIC_API_KEY"], extra_scrub=["ANTHROPIC_API_KEY"])
        assert result == {}

    def test_extra_scrub_does_not_affect_unrelated_callers(self) -> None:
        """Without extra_scrub, ANTHROPIC_API_KEY is still preserved by default."""
        env = {"ANTHROPIC_API_KEY": "sk-ant-secret", "PATH": "/bin"}
        assert scrub_agent_env(env) == env

    def test_extra_scrub_never_logs_values(self) -> None:
        env = {"ANTHROPIC_API_KEY": "sk-ant-secret"}
        with patch("sova.utils.env.log") as mock_log:
            scrub_agent_env(env, extra_scrub=["ANTHROPIC_API_KEY"])
            _, kwargs = mock_log.info.call_args
        assert "sk-ant-secret" not in repr(kwargs)
        assert kwargs["removed"] == ["ANTHROPIC_API_KEY"]


class TestScrubbedVarSets:
    def test_scrubbed_is_the_union(self) -> None:
        assert (
            SCRUBBED_VARS
            == PROVIDER_ROUTING_VARS
            | PARENT_SESSION_VARS
            | SERVICE_MANAGER_VARS
            | CODEX_CREDENTIAL_VARS
            | OPENAI_CREDENTIAL_VARS
        )

    def test_var_sets_are_disjoint(self) -> None:
        assert not (PROVIDER_ROUTING_VARS & PARENT_SESSION_VARS)
        assert not (PROVIDER_ROUTING_VARS & CODEX_CREDENTIAL_VARS)
        assert not (PARENT_SESSION_VARS & CODEX_CREDENTIAL_VARS)
        assert not (PROVIDER_ROUTING_VARS & OPENAI_CREDENTIAL_VARS)
        assert not (PARENT_SESSION_VARS & OPENAI_CREDENTIAL_VARS)
        assert not (CODEX_CREDENTIAL_VARS & OPENAI_CREDENTIAL_VARS)

    def test_service_manager_vars_never_reach_a_child(self) -> None:
        """A nested ``sova server`` in an agent must not believe it runs under the outer service."""
        env = {name: "x" for name in SERVICE_MANAGER_VARS} | {"PATH": "/usr/bin"}
        assert scrub_agent_env(env) == {"PATH": "/usr/bin"}
        assert scrub_agent_env(env, passthrough=list(SERVICE_MANAGER_VARS)) == {"PATH": "/usr/bin"}
        assert {"NOTIFY_SOCKET", "SOVA_SERVER_LOG", "SOVA_SERVER_ERR_LOG"} <= SERVICE_MANAGER_VARS

    def test_credential_vars_are_not_scrubbed(self) -> None:
        """Guards the deliberate carve-out documented in sova/utils/env.py."""
        assert not (ANTHROPIC_CREDENTIAL_VARS & SCRUBBED_VARS)

    def test_openai_api_key_is_scrubbed_by_default(self) -> None:
        """OPENAI_API_KEY must never reach a spawned child; only the in-process
        litellm/OpenAI SDK read in litellm_provider.py may use it."""
        assert "OPENAI_API_KEY" in SCRUBBED_VARS
        assert "OPENAI_API_KEY" in OPENAI_CREDENTIAL_VARS

    def test_codex_api_key_is_scrubbed_by_default(self) -> None:
        assert "CODEX_API_KEY" in SCRUBBED_VARS
        assert "CODEX_API_KEY" in CODEX_CREDENTIAL_VARS


class TestScrubCheckEnv:
    """Project check commands must not inherit the orchestrator's database URL."""

    def test_drops_only_the_database_url(self) -> None:
        from sova.utils.env import scrub_check_env

        source = {
            "SOVA_DATABASE_URL": "sqlite+aiosqlite:////tmp/orchestrator.db",
            "PATH": "/bin",
            "SOVA_X": "1",
            "PYTEST_XDIST_AUTO_NUM_WORKERS": "3",
        }
        assert scrub_check_env(source) == {"PATH": "/bin", "SOVA_X": "1", "PYTEST_XDIST_AUTO_NUM_WORKERS": "3"}
        assert "SOVA_DATABASE_URL" in source

    def test_reads_os_environ_by_default(self) -> None:
        from sova.utils.env import scrub_check_env

        with patch.dict("os.environ", {"SOVA_DATABASE_URL": "postgresql://team/sova"}):
            assert "SOVA_DATABASE_URL" not in scrub_check_env()


class TestScrubOrchestratorEnv:
    """A git-only call gets a pure scrub: no worker cap, and no config read."""

    def test_drops_the_database_url_and_adds_no_cap(self) -> None:
        from sova.utils.env import scrub_orchestrator_env

        source = {"SOVA_DATABASE_URL": "sqlite+aiosqlite:////tmp/o.db", "PATH": "/bin"}
        assert scrub_orchestrator_env(source) == {"PATH": "/bin"}

    def test_never_loads_a_config(self) -> None:
        from sova.utils.env import scrub_orchestrator_env

        with patch("sova.config.loader.load_config", side_effect=AssertionError("config read")):
            assert PYTEST_WORKERS_ENV_KEY not in scrub_orchestrator_env({"PATH": "/bin"})


@contextmanager
def _machine(*, cpus: int, active: int | OSError = 1, available_gb: int | OSError = 64) -> Iterator[None]:
    """A machine with *cpus* cores, *active* agents and *available_gb* of free memory; an exception means unreadable."""
    memory = MagicMock()
    if isinstance(available_gb, OSError):
        memory_patch = patch("sova.utils.env.psutil.virtual_memory", side_effect=available_gb)
    else:
        memory.available = available_gb * 1024**3
        memory_patch = patch("sova.utils.env.psutil.virtual_memory", return_value=memory)
    leases = (
        patch("sova.utils.env.active_agent_count", side_effect=active)
        if isinstance(active, OSError)
        else patch("sova.utils.env.active_agent_count", return_value=active)
    )
    with patch("sova.utils.env.os.cpu_count", return_value=cpus), memory_patch, leases:
        yield


class TestPytestWorkerCap:
    """Concurrent full checks each ran `-n auto` across every core; SOVA splits the cores between agents."""

    @pytest.mark.parametrize(
        ("check_workers", "max_parallel_agents", "cpus", "expected"),
        [
            pytest.param(0, 4, 14, 3, id="cores split between the agents"),
            pytest.param(0, 2, 14, 7, id="default two agents"),
            pytest.param(0, 8, 14, 2, id="never below two workers"),
            pytest.param(0, 0, 14, 14, id="zero agents reads as one"),
            pytest.param(5, 4, 14, 5, id="a fixed count wins"),
        ],
    )
    def test_worker_count(self, check_workers: int, max_parallel_agents: int, cpus: int, expected: int) -> None:
        assert pytest_worker_count(check_workers, max_parallel_agents, cpu_count=cpus) == expected

    @pytest.mark.parametrize(
        ("cpus", "active", "available_gb", "expected"),
        [
            pytest.param(14, 1, 32, 14, id="one agent gets every core"),
            pytest.param(14, 3, 32, 4, id="cores split between the active agents"),
            pytest.param(14, 3, 6, 2, id="memory binds below the floor"),
            pytest.param(14, 2, 11, 7, id="memory share equals the cpu share"),
            pytest.param(14, 2, 8, 4, id="memory binds below the cpu share"),
            pytest.param(14, 14, 32, 2, id="never below two workers"),
            pytest.param(14, 1, 1, 2, id="below the memory reserve is the floor"),
            pytest.param(14, 0, 32, 14, id="zero active agents reads as one"),
            pytest.param(14, 1, 8, 8, id="memory binds a lone agent"),
            pytest.param(14, 2, 8, 4, id="the same memory is divided between two agents"),
            pytest.param(14, 4, 8, 2, id="the same memory is divided between four agents"),
            pytest.param(8, 1, 16, 8, id="8 cores with 16 GB free: one agent gets every core"),
            pytest.param(8, 2, 16, 4, id="8 cores with 16 GB free: two agents split the cores"),
            pytest.param(8, 1, 6, 4, id="8 cores with 6 GB free: memory binds one agent"),
            pytest.param(8, 2, 6, 2, id="8 cores with 6 GB free: memory binds two agents to the floor"),
            pytest.param(8, 3, 5, 2, id="8 cores with 5 GB free: three agents sit on the floor"),
        ],
    )
    def test_worker_count_for_the_machine(self, cpus: int, active: int, available_gb: int, expected: int) -> None:
        """The production function's result: the floor, the smaller share, and memory divided by agents."""
        assert auto_workers(cpu_count=cpus, available_bytes=available_gb * 1024**3, active_agents=active) == expected

    def test_the_shares_split_the_memory_above_the_reserve_between_the_agents(self) -> None:
        # 8 GiB free less the 4 GiB reserve is 4 GiB, which holds eight 512 MiB workers for one agent, four for two.
        assert worker_shares(cpu_count=14, available_bytes=8 * 1024**3, active_agents=1) == (14, 8)
        assert worker_shares(cpu_count=14, available_bytes=8 * 1024**3, active_agents=2) == (7, 4)

    def test_check_env_carries_the_computed_split_and_logs_it(self) -> None:
        with _machine(cpus=12, active=3, available_gb=64), patch("sova.utils.env.log") as log:
            env = scrub_check_env({"PATH": "/bin"}, config=ProjectConfig(max_parallel_agents=8))
        assert env[PYTEST_WORKERS_ENV_KEY] == "4"
        log.info.assert_called_once()
        assert log.info.call_args.args == ("env.worker_cap",)
        fields = log.info.call_args.kwargs
        assert fields == {
            "source": "computed",
            "workers": 4,
            "cpu_cores": 12,
            "active_agents": 3,
            "available_mb": 64 * 1024,
            "cpu_share": 4,
            "mem_share": 40,
            "limited_by": "cpu",
            "floor_applied": False,
        }

    def test_the_floor_is_logged_when_memory_forces_it(self) -> None:
        with _machine(cpus=12, active=1, available_gb=1), patch("sova.utils.env.log") as log:
            env = scrub_check_env({}, config=ProjectConfig())
        assert env[PYTEST_WORKERS_ENV_KEY] == "2"
        assert log.info.call_args.kwargs["floor_applied"] is True
        assert log.info.call_args.kwargs["limited_by"] == "memory"

    def test_the_floor_is_attributed_to_the_cpu_when_the_cores_force_it(self) -> None:
        with _machine(cpus=4, active=4, available_gb=64), patch("sova.utils.env.log") as log:
            env = scrub_check_env({}, config=ProjectConfig())
        assert env[PYTEST_WORKERS_ENV_KEY] == "2"
        assert log.info.call_args.kwargs["floor_applied"] is True
        assert log.info.call_args.kwargs["limited_by"] == "cpu"

    def test_unreadable_leases_fall_back_to_the_static_split(self) -> None:
        with _machine(cpus=12, available_gb=64, active=OSError("denied")), patch("sova.utils.env.log") as log:
            env = scrub_check_env({}, config=ProjectConfig(max_parallel_agents=4))
        assert env[PYTEST_WORKERS_ENV_KEY] == "3"
        assert log.warning.call_args.args == ("env.worker_cap",)
        assert log.warning.call_args.kwargs["source"] == "fallback"
        assert "leases unreadable" in log.warning.call_args.kwargs["reason"]

    def test_unreadable_memory_falls_back_to_the_static_split(self) -> None:
        with _machine(cpus=12, active=3, available_gb=OSError("no vm stats")), patch("sova.utils.env.log") as log:
            env = scrub_check_env({}, config=ProjectConfig(max_parallel_agents=4))
        assert env[PYTEST_WORKERS_ENV_KEY] == "3"
        assert log.warning.call_args.args == ("env.worker_cap",)
        assert log.warning.call_args.kwargs["source"] == "fallback"
        assert "memory unreadable" in log.warning.call_args.kwargs["reason"]

    def test_check_env_carries_the_fixed_setting_and_does_not_read_the_machine(self) -> None:
        config = ProjectConfig(max_parallel_agents=4, testing=config_models.TestingConfig(check_workers=5))
        with (
            patch("sova.utils.env.active_agent_count", side_effect=AssertionError("leases read")),
            patch("sova.utils.env.psutil.virtual_memory", side_effect=AssertionError("memory read")),
            patch("sova.utils.env.os.cpu_count", side_effect=AssertionError("cores read")),
            patch("sova.utils.env.log") as log,
        ):
            env = scrub_check_env({"PATH": "/bin"}, config=config)
        assert env[PYTEST_WORKERS_ENV_KEY] == "5"
        assert env[WORKER_CAP_SOURCE_ENV_KEY] == WORKER_CAP_SOURCE_CONFIG
        assert log.info.call_args.args == ("env.worker_cap",)
        assert log.info.call_args.kwargs == {"source": "config", "workers": 5}
        log.debug.assert_not_called()

    def test_operator_value_is_kept_and_logged(self) -> None:
        config = ProjectConfig(testing=config_models.TestingConfig(check_workers=5))
        with patch("sova.utils.env.log") as log:
            env = scrub_check_env({PYTEST_WORKERS_ENV_KEY: "11"}, config=config)
        assert env[PYTEST_WORKERS_ENV_KEY] == "11"
        assert log.info.call_args.kwargs == {"source": "operator", "workers": "11"}

    @pytest.mark.parametrize("source", ["config", "inherited", "operator", "computed"])
    def test_every_source_logs_at_info_for_a_real_check_and_at_debug_when_quiet(self, source: str) -> None:
        """A slow check must be explainable whichever source decided it; git commit and push stay quiet."""
        configs = {
            "config": (ProjectConfig(testing=config_models.TestingConfig(check_workers=5)), {}),
            "inherited": (ProjectConfig(), {PYTEST_WORKERS_ENV_KEY: "7", WORKER_CAP_SOURCE_ENV_KEY: "config"}),
            "operator": (ProjectConfig(), {PYTEST_WORKERS_ENV_KEY: "11"}),
            "computed": (ProjectConfig(), {}),
        }
        config, env = configs[source]
        with _machine(cpus=12, active=3, available_gb=64), patch("sova.utils.env.log") as log:
            scrub_check_env(env, config=config)
            assert log.info.call_args.kwargs["source"] == source
            log.debug.assert_not_called()
        with _machine(cpus=12, active=3, available_gb=64), patch("sova.utils.env.log") as log:
            scrub_check_env(env, config=config, quiet=True)
            assert log.debug.call_args.kwargs["source"] == source
            log.info.assert_not_called()

    def test_a_degraded_value_is_a_warning_even_when_quiet(self) -> None:
        with _machine(cpus=12, available_gb=64, active=OSError("denied")), patch("sova.utils.env.log") as log:
            scrub_check_env({}, config=ProjectConfig(), quiet=True)
        assert log.warning.call_args.kwargs["source"] == "fallback"

    def test_a_config_value_from_a_parent_holds_for_a_nested_project_without_its_own(self) -> None:
        """`testing.check_workers` set by the parent process is inherited over the machine split."""
        spawn = ProjectConfig(testing=config_models.TestingConfig(check_workers=5))
        spawn_env = scrub_check_env({"PATH": "/bin"}, config=spawn)
        assert spawn_env[WORKER_CAP_SOURCE_ENV_KEY] == WORKER_CAP_SOURCE_CONFIG
        with (
            patch("sova.utils.env.active_agent_count", side_effect=AssertionError("leases read")),
            patch("sova.utils.env.log") as log,
        ):
            nested = scrub_check_env(spawn_env, config=ProjectConfig())
        assert nested[PYTEST_WORKERS_ENV_KEY] == "5"
        assert nested[WORKER_CAP_SOURCE_ENV_KEY] == WORKER_CAP_SOURCE_CONFIG
        assert log.info.call_args.kwargs == {"source": "inherited", "workers": "5"}

    @pytest.mark.parametrize("nested_check_workers", [5, 3], ids=["same", "different"])
    def test_a_nested_projects_own_explicit_setting_beats_an_inherited_value(self, nested_check_workers: int) -> None:
        """`testing.check_workers > 0` of the config in use wins; inheritance only replaces the computed default."""
        spawn = ProjectConfig(testing=config_models.TestingConfig(check_workers=5))
        spawn_env = scrub_check_env({"PATH": "/bin"}, config=spawn)
        nested_config = ProjectConfig(testing=config_models.TestingConfig(check_workers=nested_check_workers))
        nested = scrub_check_env(spawn_env, config=nested_config)
        assert nested[PYTEST_WORKERS_ENV_KEY] == str(nested_check_workers)
        assert nested[WORKER_CAP_SOURCE_ENV_KEY] == WORKER_CAP_SOURCE_CONFIG

    def test_a_computed_value_is_marked_as_computed(self) -> None:
        with _machine(cpus=12, active=3, available_gb=64):
            env = scrub_check_env({}, config=ProjectConfig())
        assert env[WORKER_CAP_SOURCE_ENV_KEY] == WORKER_CAP_SOURCE_COMPUTED

    def test_an_inherited_computed_share_is_recomputed_from_the_live_agent_count(self) -> None:
        """An agent spawned alone on 14 cores gets the 3-agent share for a check run once three agents are active."""
        with _machine(cpus=14, active=1, available_gb=64):
            spawn_env = scrub_check_env({"PATH": "/bin"}, config=ProjectConfig())
        assert spawn_env[PYTEST_WORKERS_ENV_KEY] == "14"
        with _machine(cpus=14, active=3, available_gb=64), patch("sova.utils.env.log") as log:
            check_env = scrub_check_env(spawn_env, config=ProjectConfig())
        assert check_env[PYTEST_WORKERS_ENV_KEY] == "4"
        assert check_env[WORKER_CAP_SOURCE_ENV_KEY] == WORKER_CAP_SOURCE_COMPUTED
        assert log.info.call_args.kwargs["source"] == "computed"
        assert log.info.call_args.kwargs["active_agents"] == 3

    def test_an_inherited_computed_share_is_replaced_by_the_projects_own_setting(self) -> None:
        with _machine(cpus=14, active=1, available_gb=64):
            spawn_env = scrub_check_env({}, config=ProjectConfig())
        config = ProjectConfig(testing=config_models.TestingConfig(check_workers=3))
        assert scrub_check_env(spawn_env, config=config)[PYTEST_WORKERS_ENV_KEY] == "3"

    def test_an_unknown_marker_is_treated_as_the_operators_value(self) -> None:
        env = scrub_check_env({PYTEST_WORKERS_ENV_KEY: "9", WORKER_CAP_SOURCE_ENV_KEY: "sova"}, config=ProjectConfig())
        assert env[PYTEST_WORKERS_ENV_KEY] == "9"

    def test_an_operator_value_is_not_marked(self) -> None:
        env = scrub_check_env({PYTEST_WORKERS_ENV_KEY: "11"}, config=ProjectConfig())
        assert WORKER_CAP_SOURCE_ENV_KEY not in env

    def test_an_empty_operator_value_is_unset(self) -> None:
        """pytest-xdist ignores an empty value, so it does not count as the operator's choice."""
        config = ProjectConfig(testing=config_models.TestingConfig(check_workers=5))
        assert scrub_check_env({PYTEST_WORKERS_ENV_KEY: ""}, config=config)[PYTEST_WORKERS_ENV_KEY] == "5"

    def test_a_whitespace_only_operator_value_is_unset(self) -> None:
        config = ProjectConfig(testing=config_models.TestingConfig(check_workers=5))
        assert scrub_check_env({PYTEST_WORKERS_ENV_KEY: "  "}, config=config)[PYTEST_WORKERS_ENV_KEY] == "5"

    def test_without_a_config_the_project_config_is_loaded(self) -> None:
        loaded = ProjectConfig(testing=config_models.TestingConfig(check_workers=6))
        with patch("sova.config.loader.load_config", return_value=loaded):
            assert scrub_check_env({})[PYTEST_WORKERS_ENV_KEY] == "6"

    def test_an_unloadable_config_falls_back_to_the_default_split(self) -> None:
        with (
            patch("sova.config.loader.load_config", side_effect=RuntimeError("no config")),
            _machine(cpus=12, active=2, available_gb=64),
        ):
            assert scrub_check_env({})[PYTEST_WORKERS_ENV_KEY] == "6"

    @pytest.mark.parametrize("error", [OSError("unreadable"), ValueError("bad override")], ids=["os", "value"])
    def test_other_load_failures_fall_back_to_the_default_split(self, error: Exception) -> None:
        with patch("sova.config.loader.load_config", side_effect=error), _machine(cpus=12, active=2, available_gb=64):
            assert scrub_check_env({})[PYTEST_WORKERS_ENV_KEY] == "6"

    def test_the_fallback_does_not_rebuild_a_project_config(self) -> None:
        """A bad SOVA_* override makes ProjectConfig() raise, so the fallback must not construct one."""
        with (
            patch("sova.config.loader.load_config", side_effect=RuntimeError("no config")),
            patch("sova.config.models.ProjectConfig", side_effect=ValueError("bad SOVA_MAX_PARALLEL_AGENTS")),
            _machine(cpus=12, active=2, available_gb=64),
        ):
            assert scrub_check_env({})[PYTEST_WORKERS_ENV_KEY] == "6"

    def test_reads_the_operator_value_from_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PYTEST_WORKERS_ENV_KEY, "9")
        assert scrub_check_env(config=ProjectConfig())[PYTEST_WORKERS_ENV_KEY] == "9"

    def test_check_workers_rejects_a_negative_count(self) -> None:
        with pytest.raises(ValidationError):
            config_models.TestingConfig(check_workers=-1)
