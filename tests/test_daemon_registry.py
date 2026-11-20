"""Tests for the supervisor daemon registry service (sova/dashboard/services/daemon_registry.py)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sova.dashboard.services import daemon_registry


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    """Give each test its own registry and put the module's back afterwards."""
    original = daemon_registry.get_registry()
    daemon_registry.set_daemon_registry({})
    try:
        yield
    finally:
        daemon_registry.set_daemon_registry(original)


class TestGetDaemon:
    def test_finds_the_daemon_registered_for_a_project(self, tmp_path: Path) -> None:
        daemon = MagicMock()
        daemon_registry.register_daemon(tmp_path, daemon)

        assert daemon_registry.get_daemon(tmp_path) is daemon

    def test_other_project_has_no_daemon(self, tmp_path: Path) -> None:
        daemon_registry.register_daemon(tmp_path / "a", MagicMock())

        assert daemon_registry.get_daemon(tmp_path / "b") is None

    def test_no_project_falls_back_to_the_sole_entry(self) -> None:
        daemon = MagicMock()
        daemon_registry.set_daemon_registry({"/only/project": daemon})

        assert daemon_registry.get_daemon() is daemon

    def test_no_project_and_several_entries_is_ambiguous(self) -> None:
        daemon_registry.set_daemon_registry({"/a": MagicMock(), "/b": MagicMock()})

        assert daemon_registry.get_daemon() is None

    def test_no_project_and_empty_registry(self) -> None:
        assert daemon_registry.get_daemon() is None


class TestRegistryRebind:
    def test_set_daemon_registry_rebinds_to_the_callers_dict(self, tmp_path: Path) -> None:
        shared: dict = {}
        daemon_registry.set_daemon_registry(shared)
        daemon = MagicMock()

        daemon_registry.register_daemon(tmp_path, daemon)

        assert daemon_registry.get_registry() is shared
        assert shared == {str(tmp_path.resolve()): daemon}


class TestUnregisterDaemon:
    def test_removes_the_project_entry(self, tmp_path: Path) -> None:
        daemon_registry.register_daemon(tmp_path, MagicMock())

        daemon_registry.unregister_daemon(tmp_path)

        assert daemon_registry.get_registry() == {}

    def test_no_project_removes_the_sole_entry(self) -> None:
        daemon_registry.set_daemon_registry({"/only/project": MagicMock()})

        daemon_registry.unregister_daemon(None)

        assert daemon_registry.get_registry() == {}

    def test_no_project_leaves_an_ambiguous_registry_alone(self) -> None:
        daemon_registry.set_daemon_registry({"/a": MagicMock(), "/b": MagicMock()})

        daemon_registry.unregister_daemon(None)

        assert len(daemon_registry.get_registry()) == 2

    def test_unknown_project_is_a_no_op(self, tmp_path: Path) -> None:
        daemon = MagicMock()
        daemon_registry.register_daemon(tmp_path / "a", daemon)

        daemon_registry.unregister_daemon(tmp_path / "b")

        assert daemon_registry.get_daemon(tmp_path / "a") is daemon


class TestLifecycleLock:
    def test_same_project_shares_one_lock(self, tmp_path: Path) -> None:
        assert daemon_registry.get_lifecycle_lock(tmp_path) is daemon_registry.get_lifecycle_lock(tmp_path)

    def test_projects_have_separate_locks(self, tmp_path: Path) -> None:
        assert daemon_registry.get_lifecycle_lock(tmp_path / "a") is not daemon_registry.get_lifecycle_lock(
            tmp_path / "b"
        )

    def test_the_lock_is_keyed_on_the_resolved_path(self, tmp_path: Path) -> None:
        assert daemon_registry.get_lifecycle_lock(tmp_path / "a" / "..") is daemon_registry.get_lifecycle_lock(tmp_path)
