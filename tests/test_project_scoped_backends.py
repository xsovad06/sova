"""Per-project LLM provider and agent runtime on a multi-project server.

The server's global provider and runtime are built from the project it started in. Before these were
resolved per project, a project on OpenAI and Codex served by a hub on Claude Code had its supervisor
planner send OpenAI model names to the Claude CLI, and its dashboard commands spawn Claude Code.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sova.config.models import CodexConfig, LLMConfig, ProjectConfig
from sova.dashboard.services.agent_pool import set_server_project_dir
from sova.ipc import runtime as runtime_mod
from sova.llm import client
from sova.llm.models import LLMResult


def _project(tmp_path: Path, name: str) -> Path:
    """A directory that owns its config (a ``sova.toml``), so it resolves to itself."""
    project = tmp_path / name
    project.mkdir()
    (project / "sova.toml").write_text("", encoding="utf-8")
    return project


def _openai_cfg(model: str = "openai/gpt-6.1-sol") -> ProjectConfig:
    return ProjectConfig(llm=LLMConfig(provider="openai", model=model))


class _FakeProvider:
    """Records the model each call asked for; answers like a provider."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.models: list[str | None] = []

    def normalize_model_name(self, model: str) -> str:
        return model

    async def invoke(self, prompt: str, **kwargs: object) -> LLMResult:
        self.models.append(kwargs.get("model"))  # type: ignore[arg-type]
        return LLMResult(text=self.name, model=str(kwargs.get("model")))


class TestProviderFor:
    def test_disabled_returns_the_global_provider(self, tmp_path: Path) -> None:
        hub = object()
        with patch.object(client, "get_provider", return_value=hub):
            assert client.provider_for(_openai_cfg(), _project(tmp_path, "rbac")) is hub

    def test_enabled_builds_each_project_its_own_provider(self, tmp_path: Path) -> None:
        rbac, turnpike = _project(tmp_path, "rbac"), _project(tmp_path, "turnpike")
        client.enable_project_scoped_providers()
        with patch.object(client, "create_provider", side_effect=lambda cfg, d: (cfg.provider, d)) as create:
            first = client.provider_for(_openai_cfg(), rbac)
            assert first == ("openai", rbac)
            assert client.provider_for(_openai_cfg(), rbac) is first  # cached
            assert client.provider_for(_openai_cfg(), turnpike) == ("openai", turnpike)

        assert create.call_count == 2

    def test_a_changed_llm_section_rebuilds_the_provider(self, tmp_path: Path) -> None:
        rbac = _project(tmp_path, "rbac")
        client.enable_project_scoped_providers()
        with patch.object(client, "create_provider", side_effect=lambda cfg, d: cfg.model):
            assert client.provider_for(_openai_cfg("openai/a"), rbac) == "openai/a"
            assert client.provider_for(_openai_cfg("openai/b"), rbac) == "openai/b"

    def test_a_worktree_shares_its_primary_checkouts_provider(self, tmp_path: Path) -> None:
        rbac = _project(tmp_path, "rbac")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        client.enable_project_scoped_providers()
        with (
            patch("sova.llm.client._config_root", side_effect=lambda cwd: rbac),
            patch.object(client, "create_provider", side_effect=lambda cfg, d: object()) as create,
        ):
            assert client.provider_for(_openai_cfg(), worktree) is client.provider_for(_openai_cfg(), rbac)
        assert create.call_count == 1

    def test_no_cwd_uses_the_global_provider(self) -> None:
        hub = object()
        client.enable_project_scoped_providers()
        with patch.object(client, "get_provider", return_value=hub):
            assert client.provider_for(_openai_cfg(), None) is hub

    async def test_invoke_uses_the_projects_provider_not_the_hubs(self, tmp_path: Path) -> None:
        """The supervisor planner's call: an OpenAI project's model must reach its own provider."""
        rbac = _project(tmp_path, "rbac")
        hub, project = _FakeProvider("hub"), _FakeProvider("rbac")
        client.enable_project_scoped_providers()
        with (
            patch.object(client, "get_provider", return_value=hub),
            patch.object(client, "create_provider", return_value=project),
            patch.object(client, "prepare_invocation", return_value=(_openai_cfg(), "openai/gpt-6.1-sol", 30.0)),
        ):
            result = await client.invoke("plan", task_type="planner", cwd=rbac)

        assert result.text == "rbac"
        assert project.models == ["openai/gpt-6.1-sol"]
        assert hub.models == []


class TestRuntimeFor:
    def test_disabled_returns_the_global_runtime(self, tmp_path: Path) -> None:
        hub = object()
        with patch.object(runtime_mod, "get_runtime", return_value=hub):
            assert runtime_mod.runtime_for(_project(tmp_path, "rbac")) is hub

    def test_enabled_builds_the_projects_runtime(self, tmp_path: Path) -> None:
        rbac = _project(tmp_path, "rbac")
        cfg = ProjectConfig(agent={"runtime": "codex"}, codex=CodexConfig(model="gpt-6.1-sol"))
        runtime_mod.enable_project_scoped_runtimes()
        with patch.object(runtime_mod, "create_runtime", side_effect=lambda kind, codex: (kind, codex.model)) as create:
            assert runtime_mod.runtime_for(rbac, cfg) == ("codex", "gpt-6.1-sol")
            assert runtime_mod.runtime_for(rbac, cfg) == ("codex", "gpt-6.1-sol")
        assert create.call_count == 1

    def test_enabled_loads_the_projects_config_when_not_given(self, tmp_path: Path) -> None:
        rbac = _project(tmp_path, "rbac")
        (rbac / "sova.toml").write_text('[agent]\nruntime = "codex"\n', encoding="utf-8")
        runtime_mod.enable_project_scoped_runtimes()
        with patch.object(runtime_mod, "create_runtime", side_effect=lambda kind, codex: kind):
            assert runtime_mod.runtime_for(rbac) == "codex"


class TestDashboardSpawnRuntime:
    def test_command_spawn_uses_the_projects_runtime_when_scoped(self, tmp_path: Path) -> None:
        from sova.dashboard.services import agent_lifecycle

        rbac = _project(tmp_path, "rbac")
        project_runtime, hub = MagicMock(name="codex"), MagicMock(name="claude")
        runtime_mod.enable_project_scoped_runtimes()
        with (
            patch.object(agent_lifecycle, "runtime_for", return_value=project_runtime) as resolve,
            patch.object(agent_lifecycle, "get_runtime", return_value=hub),
        ):
            assert agent_lifecycle._project_runtime(rbac) is project_runtime
        resolve.assert_called_once_with(rbac)

    def test_command_spawn_uses_the_global_runtime_when_not_scoped(self, tmp_path: Path) -> None:
        from sova.dashboard.services import agent_lifecycle

        hub = MagicMock(name="claude")
        with patch.object(agent_lifecycle, "get_runtime", return_value=hub):
            assert agent_lifecycle._project_runtime(_project(tmp_path, "rbac")) is hub


class TestSettingsReloadOnMultiProjectServer:
    """A settings save for one project must not swap the server-wide provider or runtime to that project's."""

    async def test_llm_save_drops_that_projects_provider_without_replacing_the_global_one(self, tmp_path: Path) -> None:
        from sova.dashboard.services import settings_service

        rbac, turnpike = _project(tmp_path, "rbac"), _project(tmp_path, "turnpike")
        set_server_project_dir(_project(tmp_path, "hub"))
        client.enable_project_scoped_providers()
        client._project_providers[rbac.resolve()] = ("fp", object())
        client._project_providers[turnpike.resolve()] = ("fp", object())
        with patch("sova.llm.client.reload_provider_async") as reload:
            await settings_service.dispatch_config_reload("llm", _openai_cfg(), {}, rbac)

        reload.assert_not_called()
        # Only the saved project's provider is dropped: the others are still current.
        assert list(client._project_providers) == [turnpike.resolve()]

    async def test_runtime_save_drops_that_projects_runtime_without_replacing_the_global_one(
        self, tmp_path: Path
    ) -> None:
        from sova.dashboard.services import settings_service

        rbac, turnpike = _project(tmp_path, "rbac"), _project(tmp_path, "turnpike")
        set_server_project_dir(_project(tmp_path, "hub"))
        runtime_mod.enable_project_scoped_runtimes()
        runtime_mod._project_runtimes[rbac.resolve()] = ("fp", object())
        runtime_mod._project_runtimes[turnpike.resolve()] = ("fp", object())
        with patch("sova.ipc.runtime.reload_runtime") as reload:
            await settings_service.dispatch_config_reload("runtime", _openai_cfg(), {}, rbac)

        reload.assert_not_called()
        assert list(runtime_mod._project_runtimes) == [turnpike.resolve()]

    @pytest.mark.parametrize("target", ["llm", "runtime"])
    async def test_a_save_for_the_servers_own_project_also_reloads_the_global_backend(
        self, tmp_path: Path, target: str
    ) -> None:
        """Callers with no project in scope still use the global provider and runtime: they must switch too."""
        from sova.dashboard.services import settings_service

        hub = _project(tmp_path, "hub")
        set_server_project_dir(hub)
        client.enable_project_scoped_providers()
        runtime_mod.enable_project_scoped_runtimes()
        cfg = _openai_cfg()
        with (
            patch("sova.llm.client.reload_provider_async") as reload_provider,
            patch("sova.ipc.runtime.reload_runtime") as reload_runtime,
        ):
            await settings_service.dispatch_config_reload(target, cfg, {}, hub)

        if target == "llm":
            reload_provider.assert_awaited_once_with(cfg, hub)
        else:
            reload_runtime.assert_called_once_with(cfg)

    @pytest.mark.parametrize("target", ["llm", "runtime"])
    async def test_a_save_scoped_to_a_worktree_of_the_servers_project_reloads_the_global_backend(
        self, tmp_path: Path, target: str
    ) -> None:
        """A worktree resolves to its owning checkout, so it counts as the server's own project."""
        from sova.dashboard.services import settings_service

        hub = _project(tmp_path, "hub")
        worktree = tmp_path / "hub-worktree"
        worktree.mkdir()
        set_server_project_dir(hub)
        client.enable_project_scoped_providers()
        runtime_mod.enable_project_scoped_runtimes()
        cfg = _openai_cfg()

        def owner(path: Path) -> Path:
            return hub.resolve() if path.resolve() == worktree.resolve() else path.resolve()

        with (
            patch("sova.config.root.owning_config_dir", side_effect=owner),
            patch("sova.llm.client.reload_provider_async") as reload_provider,
            patch("sova.ipc.runtime.reload_runtime") as reload_runtime,
        ):
            await settings_service.dispatch_config_reload(target, cfg, {}, worktree)

        if target == "llm":
            reload_provider.assert_awaited_once_with(cfg, worktree)
        else:
            reload_runtime.assert_called_once_with(cfg)

    @pytest.mark.parametrize("target", ["llm", "runtime"])
    async def test_a_save_without_scoping_reloads_the_global_backend(self, tmp_path: Path, target: str) -> None:
        """A single-project server has no per-project registry: every save swaps the one global backend."""
        from sova.dashboard.services import settings_service

        rbac = _project(tmp_path, "rbac")
        cfg = _openai_cfg()
        with (
            patch("sova.llm.client.reload_provider_async") as reload_provider,
            patch("sova.ipc.runtime.reload_runtime") as reload_runtime,
        ):
            await settings_service.dispatch_config_reload(target, cfg, {}, rbac)

        if target == "llm":
            reload_provider.assert_awaited_once_with(cfg, rbac)
        else:
            reload_runtime.assert_called_once_with(cfg)


class TestOffTheEventLoop:
    """Building a project's provider or runtime reads its config or the keyring: never on the event loop."""

    async def test_provider_for_async_builds_a_missing_provider_in_a_worker_thread(self, tmp_path: Path) -> None:
        import threading

        rbac = _project(tmp_path, "rbac")
        loop_thread = threading.get_ident()
        built_on: list[int] = []

        def build(cfg: LLMConfig, project_dir: Path) -> object:
            built_on.append(threading.get_ident())
            return object()

        client.enable_project_scoped_providers()
        with patch.object(client, "create_provider", side_effect=build):
            first = await client.provider_for_async(_openai_cfg(), rbac)
            assert await client.provider_for_async(_openai_cfg(), rbac) is first  # a hit stays on the loop

        assert len(built_on) == 1
        assert built_on[0] != loop_thread

    async def test_start_agent_runtime_is_resolved_off_the_loop_after_an_invalidation(self, tmp_path: Path) -> None:
        """A settings save empties the cache; the next spawn's config read must not run on the loop."""
        import threading

        from sova.dashboard.services import agent_lifecycle

        rbac = _project(tmp_path, "rbac")
        loop_thread = threading.get_ident()
        loaded_on: list[int] = []

        def load(project_dir: Path) -> ProjectConfig:
            loaded_on.append(threading.get_ident())
            return ProjectConfig(agent={"runtime": "codex"})

        runtime_mod.enable_project_scoped_runtimes()
        runtime_mod.clear_project_runtimes()
        with (
            patch("sova.config.loader.load_config", side_effect=load),
            patch.object(runtime_mod, "create_runtime", side_effect=lambda kind, codex: kind),
        ):
            assert await agent_lifecycle._project_runtime_async(rbac) == "codex"

        assert loaded_on
        assert loop_thread not in loaded_on

    async def test_unscoped_runtime_reads_no_config(self, tmp_path: Path) -> None:
        from sova.dashboard.services import agent_lifecycle

        hub = MagicMock(name="claude")
        with (
            patch.object(agent_lifecycle, "get_runtime", return_value=hub),
            patch("sova.config.loader.load_config") as load,
        ):
            assert await agent_lifecycle._project_runtime_async(_project(tmp_path, "rbac")) is hub
        load.assert_not_called()

    async def test_provider_for_async_returns_the_global_provider_when_not_scoped(self, tmp_path: Path) -> None:
        hub = object()
        with (
            patch.object(client, "get_provider", return_value=hub),
            patch.object(client, "create_provider") as create,
        ):
            assert await client.provider_for_async(_openai_cfg(), _project(tmp_path, "rbac")) is hub
        create.assert_not_called()

    async def test_runtime_for_async_returns_the_global_runtime_when_not_scoped(self, tmp_path: Path) -> None:
        hub = object()
        with (
            patch.object(runtime_mod, "get_runtime", return_value=hub),
            patch("sova.config.loader.load_config") as load,
        ):
            assert await runtime_mod.runtime_for_async(_project(tmp_path, "rbac")) is hub
        load.assert_not_called()

    async def test_runtime_for_async_builds_the_projects_runtime_in_a_worker_thread(self, tmp_path: Path) -> None:
        import threading

        rbac = _project(tmp_path, "rbac")
        loop_thread = threading.get_ident()
        built_on: list[int] = []

        def build(kind: str, codex: CodexConfig) -> str:
            built_on.append(threading.get_ident())
            return kind

        runtime_mod.enable_project_scoped_runtimes()
        with (
            patch(
                "sova.config.loader.load_config",
                return_value=ProjectConfig(agent={"runtime": "codex"}),
            ),
            patch.object(runtime_mod, "create_runtime", side_effect=build),
        ):
            assert await runtime_mod.runtime_for_async(rbac) == "codex"

        assert built_on
        assert loop_thread not in built_on


class TestAdoptionUsesTheSpawnResolver:
    def test_adopt_and_spawn_resolve_the_same_runtime(self, tmp_path: Path) -> None:
        from sova.dashboard.services import agent_adopt, agent_lifecycle

        rbac = _project(tmp_path, "rbac")
        spawned = MagicMock(name="codex")
        with patch.object(agent_lifecycle, "_project_runtime", return_value=spawned) as resolve:
            assert agent_adopt._project_runtime(rbac) is spawned
        resolve.assert_called_once_with(rbac)


class TestOversightProvider:
    async def test_unscoped_oversight_uses_the_global_provider_without_loading_config(self, tmp_path: Path) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(OversightConfig(), project_dir=str(tmp_path))
        hub = object()
        with (
            patch.object(client, "get_provider", return_value=hub),
            patch("sova.oversight.agent.load_config") as load,
        ):
            assert await agent._analysis_provider() is hub
        load.assert_not_called()


class TestServerLifespanScoping:
    """Per-project resolution is switched on by the multi-project lifespan, and only there."""

    async def _enter_lifespan(self, app: object) -> tuple[bool, bool]:
        with patch("sova.utils.logging.setup_logging"):
            async with app.router.lifespan_context(app):  # type: ignore[attr-defined]
                return client.project_scoped_providers_enabled(), runtime_mod.project_scoped_runtimes_enabled()

    async def test_multi_project_server_scopes_providers_and_runtimes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sova.config.registry as reg_mod
        from sova.dashboard.app import create_app

        monkeypatch.setattr(reg_mod, "_REGISTRY_FILE", tmp_path / "registry" / "projects.json")
        monkeypatch.setattr(reg_mod, "_REGISTRY_DIR", tmp_path / "registry")
        hub = _project(tmp_path, "hub")
        (hub / ".claude").mkdir()
        reg_mod.register_project(hub, slug="hub")

        assert await self._enter_lifespan(create_app(project_dir=hub, multi_project=True)) == (True, True)

    async def test_single_project_server_keeps_the_global_backends(self, tmp_path: Path) -> None:
        from sova.dashboard.app import create_app

        project = _project(tmp_path, "solo")
        (project / ".claude").mkdir()
        client.enable_project_scoped_providers()  # a stale switch from an earlier server must be turned off
        runtime_mod.enable_project_scoped_runtimes()

        assert await self._enter_lifespan(create_app(project_dir=project)) == (False, False)


@pytest.fixture(autouse=True)
def _empty_registries() -> Iterator[None]:
    client.clear_project_providers()
    runtime_mod.clear_project_runtimes()
    yield
    set_server_project_dir(None)
