"""Tests for sova.dashboard.services.agent_pool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import sova.dashboard.services.agent_pool as pool_mod
from sova.dashboard.services.agent_pool import (
    _DEFAULT_SLUG,
    _get_project_agents,
)


@pytest.fixture(autouse=True)
def _clean_pool():
    """Reset module-level state between tests."""
    old_projects = pool_mod._projects.copy()
    old_default = pool_mod._default_project_dir
    pool_mod._projects.clear()
    yield
    pool_mod._projects.clear()
    pool_mod._projects.update(old_projects)
    pool_mod._default_project_dir = old_default


class TestGetProjectAgents:
    """Tests for _get_project_agents registry resolution."""

    def test_registry_lookup_for_non_default_slug(self, tmp_path: Path) -> None:
        """When get_project_dir() returns None and slug is a real project,
        resolve project_dir from the registry."""
        project_path = tmp_path / "gwym"
        project_path.mkdir()

        with (
            patch.object(pool_mod, "get_project_dir", return_value=None),
            patch.object(pool_mod, "get_project_slug", return_value=None),
            patch("sova.config.registry.get_project_path", return_value=project_path),
            patch.object(pool_mod, "read_max_parallel", return_value=2),
        ):
            pa = _get_project_agents("gwym")

        assert pa.project_dir == project_path.resolve()

    def test_default_slug_skips_registry(self, tmp_path: Path) -> None:
        """Default slug should NOT trigger a registry lookup."""
        fallback = tmp_path / "default"
        fallback.mkdir()
        pool_mod._default_project_dir = fallback

        with (
            patch.object(pool_mod, "get_project_dir", return_value=None),
            patch.object(pool_mod, "get_project_slug", return_value=None),
            patch("sova.config.registry.get_project_path") as mock_reg,
            patch.object(pool_mod, "read_max_parallel", return_value=2),
        ):
            pa = _get_project_agents(_DEFAULT_SLUG)

        mock_reg.assert_not_called()
        assert pa.project_dir == fallback.resolve()

    def test_slug_not_in_registry_falls_through(self, tmp_path: Path) -> None:
        """Unknown slug falls through to _default_project_dir."""
        fallback = tmp_path / "fallback"
        fallback.mkdir()
        pool_mod._default_project_dir = fallback

        with (
            patch.object(pool_mod, "get_project_dir", return_value=None),
            patch.object(pool_mod, "get_project_slug", return_value=None),
            patch("sova.config.registry.get_project_path", return_value=None),
            patch.object(pool_mod, "read_max_parallel", return_value=2),
        ):
            pa = _get_project_agents("unknown-project")

        assert pa.project_dir == fallback.resolve()

    def test_http_context_takes_precedence(self, tmp_path: Path) -> None:
        """When get_project_dir() returns a value (HTTP context), registry is skipped."""
        http_dir = tmp_path / "http"
        http_dir.mkdir()
        registry_dir = tmp_path / "registry"
        registry_dir.mkdir()

        with (
            patch.object(pool_mod, "get_project_dir", return_value=http_dir),
            patch.object(pool_mod, "get_project_slug", return_value=None),
            patch("sova.config.registry.get_project_path") as mock_reg,
            patch.object(pool_mod, "read_max_parallel", return_value=2),
        ):
            pa = _get_project_agents("gwym")

        mock_reg.assert_not_called()
        assert pa.project_dir == http_dir.resolve()

    def test_cached_pool_returned_on_second_call(self, tmp_path: Path) -> None:
        """Second call with same slug returns the cached ProjectAgents."""
        project_path = tmp_path / "gwym"
        project_path.mkdir()

        with (
            patch.object(pool_mod, "get_project_dir", return_value=None),
            patch.object(pool_mod, "get_project_slug", return_value=None),
            patch("sova.config.registry.get_project_path", return_value=project_path),
            patch.object(pool_mod, "read_max_parallel", return_value=2),
        ):
            pa1 = _get_project_agents("gwym")
            pa2 = _get_project_agents("gwym")

        assert pa1 is pa2


class TestPruneCompleted:
    def test_prune_completed_default_now(self) -> None:
        """_prune_completed uses time.monotonic() when now=None."""
        import time

        from sova.dashboard.services.agent_pool import (
            CompletedAgent,
            ProjectAgents,
            _prune_completed,
        )

        pa = ProjectAgents()
        pa.recently_completed.append(
            CompletedAgent(
                run_id=1, issue="1", role="dev", status="done", cost=0.5, completed_at=time.monotonic() - 9999
            ),
        )
        _prune_completed(pa)
        assert len(pa.recently_completed) == 0

    def test_prune_completed_removes_expired(self) -> None:
        """_prune_completed poplefts expired entries."""
        from sova.dashboard.services.agent_pool import (
            RECENTLY_COMPLETED_TTL,
            CompletedAgent,
            ProjectAgents,
            _prune_completed,
        )

        now = 10000.0
        pa = ProjectAgents()
        pa.recently_completed.append(
            CompletedAgent(
                run_id=1, issue="1", role="dev", status="done", cost=0.5, completed_at=now - RECENTLY_COMPLETED_TTL - 1
            ),
        )
        pa.recently_completed.append(
            CompletedAgent(run_id=2, issue="2", role="dev", status="done", cost=0.5, completed_at=now - 1),
        )
        _prune_completed(pa, now=now)
        assert len(pa.recently_completed) == 1
        assert pa.recently_completed[0].run_id == 2


class TestAgentPoolConfig:
    def test_read_max_parallel_returns_config_value(self, tmp_path):
        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[project]\nmax_parallel_agents = 5\n")
        from sova.dashboard.services.agent_pool import read_max_parallel

        result = read_max_parallel(tmp_path)
        assert result == 5

    def test_read_max_parallel_fallback_on_missing_config(self, tmp_path):
        from sova.dashboard.services.agent_pool import ProjectAgents, read_max_parallel

        result = read_max_parallel(tmp_path / "nonexistent")
        assert result == ProjectAgents.max_concurrent

    def test_sync_max_concurrent_updates_pool(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        from sova.dashboard.services import agent_pool

        mock_cfg = MagicMock()
        mock_cfg.max_parallel_agents = 7
        mock_load_config = MagicMock(return_value=mock_cfg)
        monkeypatch.setattr("sova.config.loader.load_config", mock_load_config)
        old_projects = agent_pool._projects.copy()
        agent_pool._projects.clear()
        try:
            pa = agent_pool._get_project_agents("test-sync")
            pa.max_concurrent = 2
            agent_pool.sync_max_concurrent(project_dir=tmp_path, slug="test-sync")
            assert mock_load_config.called
            assert pa.max_concurrent == 7
        finally:
            agent_pool._projects.clear()
            agent_pool._projects.update(old_projects)

    def test_sync_max_concurrent_no_change_when_equal(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        from sova.dashboard.services import agent_pool

        mock_cfg = MagicMock()
        mock_cfg.max_parallel_agents = 2
        mock_load_config = MagicMock(return_value=mock_cfg)
        monkeypatch.setattr("sova.config.loader.load_config", mock_load_config)
        old_projects = agent_pool._projects.copy()
        agent_pool._projects.clear()
        try:
            pa = agent_pool._get_project_agents("test-noop")
            pa.max_concurrent = 2
            agent_pool.sync_max_concurrent(project_dir=tmp_path, slug="test-noop")
            assert mock_load_config.called
            assert pa.max_concurrent == 2
        finally:
            agent_pool._projects.clear()
            agent_pool._projects.update(old_projects)

    def test_sync_max_concurrent_swallows_config_errors(self, monkeypatch):
        from unittest.mock import MagicMock

        from sova.dashboard.services import agent_pool

        mock_load_config = MagicMock(side_effect=RuntimeError("no config"))
        monkeypatch.setattr("sova.config.loader.load_config", mock_load_config)
        old_projects = agent_pool._projects.copy()
        agent_pool._projects.clear()
        try:
            pa = agent_pool._get_project_agents("test-err")
            pa.max_concurrent = 3
            agent_pool.sync_max_concurrent(project_dir=Path("/nonexistent"), slug="test-err")
            assert mock_load_config.called
            assert pa.max_concurrent == 3
        finally:
            agent_pool._projects.clear()
            agent_pool._projects.update(old_projects)

    def test_get_project_agents_reads_config_on_create(self, tmp_path, monkeypatch):
        from sova.dashboard.services import agent_pool

        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[project]\nmax_parallel_agents = 4\n")
        monkeypatch.setattr("sova.dashboard.services.agent_pool.get_project_dir", lambda: tmp_path)
        old_projects = agent_pool._projects.copy()
        agent_pool._projects.clear()
        try:
            pa = agent_pool._get_project_agents("test-init")
            assert pa.max_concurrent == 4
        finally:
            agent_pool._projects.clear()
            agent_pool._projects.update(old_projects)


class TestGetProjectPool:
    def test_returns_and_caches_pool_for_slug(self, tmp_path: Path) -> None:
        project_path = tmp_path / "proj"
        project_path.mkdir()

        with (
            patch.object(pool_mod, "get_project_dir", return_value=None),
            patch.object(pool_mod, "get_project_slug", return_value=None),
            patch("sova.config.registry.get_project_path", return_value=project_path),
            patch.object(pool_mod, "read_max_parallel", return_value=2),
        ):
            pa = pool_mod.get_project_pool("proj")
            pa_again = pool_mod.get_project_pool("proj")

        assert pa.project_dir == project_path.resolve()
        assert pa is pa_again


class TestListAllPools:
    def test_returns_independent_snapshot(self, tmp_path: Path) -> None:
        project_path = tmp_path / "proj"
        project_path.mkdir()

        with (
            patch.object(pool_mod, "get_project_dir", return_value=None),
            patch.object(pool_mod, "get_project_slug", return_value=None),
            patch("sova.config.registry.get_project_path", return_value=project_path),
            patch.object(pool_mod, "read_max_parallel", return_value=2),
        ):
            pool_mod._get_project_agents("proj")

        pools = pool_mod.list_all_pools()
        assert set(pools) == {"proj"}

        pools["extra"] = pool_mod.ProjectAgents()
        assert "extra" not in pool_mod._projects


class TestEvictCompletedForIssue:
    def test_removes_only_matching_issue(self) -> None:
        from sova.dashboard.services.agent_pool import CompletedAgent, ProjectAgents, _evict_completed_for_issue

        pa = ProjectAgents()
        pa.recently_completed.append(CompletedAgent(run_id=1, issue="1", role="dev", status="done", cost=0.1))
        pa.recently_completed.append(CompletedAgent(run_id=2, issue="2", role="dev", status="done", cost=0.2))
        pa.recently_completed.append(CompletedAgent(run_id=3, issue="1", role="dev", status="failed", cost=0.3))

        _evict_completed_for_issue(pa, "1")

        assert len(pa.recently_completed) == 1
        assert pa.recently_completed[0].issue == "2"

    def test_noop_when_no_match(self) -> None:
        from sova.dashboard.services.agent_pool import CompletedAgent, ProjectAgents, _evict_completed_for_issue

        pa = ProjectAgents()
        pa.recently_completed.append(CompletedAgent(run_id=1, issue="5", role="dev", status="done", cost=0.1))

        _evict_completed_for_issue(pa, "99")

        assert len(pa.recently_completed) == 1
