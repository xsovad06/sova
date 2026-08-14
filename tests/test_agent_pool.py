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
