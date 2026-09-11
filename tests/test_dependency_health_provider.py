"""Tests for the dependency health awareness provider."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from sova.awareness.base import ItemCategory
from sova.awareness.providers import dependency_health as provider_mod
from sova.config.models import AwarenessConfig
from sova.dashboard.services import dependency_health_service as svc


def _cfg(enabled: bool):
    from sova.config.models import DependencyHealthConfig, ProjectConfig

    return ProjectConfig(dependency_health=DependencyHealthConfig(enabled=enabled))


def _snapshot(packages: list[svc.PackageHealth]) -> svc.DependencyHealthSnapshot:
    return svc.DependencyHealthSnapshot(
        enabled=True,
        generated_at="2026-01-01T00:00:00+00:00",
        ecosystems=["pypi"],
        total=len(packages),
        up_to_date=0,
        major_behind=0,
        deprecated=0,
        vulnerable=0,
        stale=0,
        health_percent=0.0,
        packages=packages,
    )


class TestIsConfigured:
    async def test_false_when_no_project_enabled(self, tmp_path: Path) -> None:
        with (
            patch.object(provider_mod, "list_projects", return_value={"p": str(tmp_path)}),
            patch("sova.config.loader.load_config", return_value=_cfg(False)),
        ):
            assert await provider_mod.DependencyHealthProvider(AwarenessConfig()).is_configured() is False

    async def test_true_when_any_project_enabled(self, tmp_path: Path) -> None:
        with (
            patch.object(provider_mod, "list_projects", return_value={"p": str(tmp_path)}),
            patch("sova.config.loader.load_config", return_value=_cfg(True)),
        ):
            assert await provider_mod.DependencyHealthProvider(AwarenessConfig()).is_configured() is True

    async def test_config_load_failure_is_not_configured(self, tmp_path: Path) -> None:
        with (
            patch.object(provider_mod, "list_projects", return_value={"p": str(tmp_path)}),
            patch("sova.config.loader.load_config", side_effect=RuntimeError("boom")),
        ):
            assert await provider_mod.DependencyHealthProvider(AwarenessConfig()).is_configured() is False


class TestFetchItems:
    async def test_empty_registry(self) -> None:
        with patch.object(provider_mod, "list_projects", return_value={}):
            assert await provider_mod.DependencyHealthProvider(AwarenessConfig()).fetch_items() == []

    async def test_reads_cache_only_never_scans(self, tmp_path: Path) -> None:
        mock_snapshot = AsyncMock(return_value=_snapshot([]))
        with (
            patch.object(provider_mod, "list_projects", return_value={"p": str(tmp_path)}),
            patch("sova.config.loader.load_config", return_value=_cfg(True)),
            patch.object(svc, "get_snapshot", new=mock_snapshot),
        ):
            await provider_mod.DependencyHealthProvider(AwarenessConfig()).fetch_items()
        assert mock_snapshot.call_args.kwargs["cache_only"] is True

    async def test_flags_vulnerable_and_deprecated_packages(self, tmp_path: Path) -> None:
        packages = [
            svc.PackageHealth("vuln", "pypi", "1.0.0", "1.0.0", "up_to_date", False, True, None, None, 40),
            svc.PackageHealth("dep", "pypi", "1.0.0", "1.0.0", "up_to_date", True, False, None, None, 40),
            svc.PackageHealth("old", "pypi", "1.0.0", "2.0.0", "major", False, False, None, None, 40),
        ]
        with (
            patch.object(provider_mod, "list_projects", return_value={"p": str(tmp_path)}),
            patch("sova.config.loader.load_config", return_value=_cfg(True)),
            patch.object(svc, "get_snapshot", new=AsyncMock(return_value=_snapshot(packages))),
        ):
            items = await provider_mod.DependencyHealthProvider(AwarenessConfig()).fetch_items()
        assert [i.metadata["package"] for i in items] == ["vuln", "dep"]
        assert items[0].category is ItemCategory.NEEDS_ATTENTION
        assert items[0].urgency == 2
        assert "known vulnerability" in items[0].title

        dep_item = items[1]
        assert dep_item.category is ItemCategory.NEEDS_ATTENTION
        assert dep_item.urgency == 2
        assert "deprecated" in dep_item.title
        assert dep_item.id == "dependency_health:p:pypi:dep"

    async def test_mixed_ecosystem_same_name_yields_distinct_ids(self, tmp_path: Path) -> None:
        packages = [
            svc.PackageHealth("core", "npm", "1.0.0", "1.0.0", "up_to_date", True, False, None, None, 40),
            svc.PackageHealth("core", "pypi", "1.0.0", "1.0.0", "up_to_date", True, False, None, None, 40),
        ]
        with (
            patch.object(provider_mod, "list_projects", return_value={"p": str(tmp_path)}),
            patch("sova.config.loader.load_config", return_value=_cfg(True)),
            patch.object(svc, "get_snapshot", new=AsyncMock(return_value=_snapshot(packages))),
        ):
            items = await provider_mod.DependencyHealthProvider(AwarenessConfig()).fetch_items()
        ids = [i.id for i in items]
        assert len(ids) == len(set(ids)) == 2
        assert "dependency_health:p:npm:core" in ids
        assert "dependency_health:p:pypi:core" in ids

    async def test_disabled_project_yields_nothing(self, tmp_path: Path) -> None:
        with (
            patch.object(provider_mod, "list_projects", return_value={"p": str(tmp_path)}),
            patch("sova.config.loader.load_config", return_value=_cfg(False)),
        ):
            assert await provider_mod.DependencyHealthProvider(AwarenessConfig()).fetch_items() == []

    async def test_project_failure_is_isolated(self, tmp_path: Path) -> None:
        with (
            patch.object(provider_mod, "list_projects", return_value={"p": str(tmp_path)}),
            patch("sova.config.loader.load_config", return_value=_cfg(True)),
            patch.object(svc, "get_snapshot", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            assert await provider_mod.DependencyHealthProvider(AwarenessConfig()).fetch_items() == []

    async def test_caps_items_per_project(self, tmp_path: Path) -> None:
        packages = [
            svc.PackageHealth(f"p{i}", "pypi", "1.0.0", "2.0.0", "major", True, False, None, None, 80)
            for i in range(provider_mod._MAX_ITEMS_PER_PROJECT + 3)
        ]
        with (
            patch.object(provider_mod, "list_projects", return_value={"p": str(tmp_path)}),
            patch("sova.config.loader.load_config", return_value=_cfg(True)),
            patch.object(svc, "get_snapshot", new=AsyncMock(return_value=_snapshot(packages))),
        ):
            items = await provider_mod.DependencyHealthProvider(AwarenessConfig()).fetch_items()
        assert len(items) == provider_mod._MAX_ITEMS_PER_PROJECT
