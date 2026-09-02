"""Shared pytest fixtures for the SOVA test suite.

The key fixture here is ``seed_config``, which seeds DB-backed project settings
for a test project directory. Production SOVA stores configuration in
``.claude/sova.db`` (the ``project_settings`` table), not in ``sova.toml``, so
tests that need configured values should seed the DB rather than writing a TOML
file. Writing ``sova.toml`` only works today because ``load_config()`` still has
an auto-migration fallback that epic #550 intends to remove. See issue #557.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def _seed_project_settings(project_dir: Path, values: dict[str, Any]) -> None:
    """Upsert flattened, JSON-encoded config values into ``project_settings``.

    Creates the SQLite DB and schema under ``{project_dir}/.claude/sova.db`` if
    absent. Nested dicts are flattened to dot-notation keys; flat dotted keys
    pass through unchanged. Purely synchronous so it works with or without a
    running event loop.
    """
    from sqlalchemy import create_engine

    from sova.config.db_loader import _flatten_config_dict, _write_flat_config
    from sova.db.models import Base

    flat = _flatten_config_dict(values)

    db_path = project_dir / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            _write_flat_config(conn, values, flat)
    finally:
        engine.dispose()


@pytest.fixture
def seed_config():
    """Seed DB-backed project settings so ``load_config`` reads them.

    Returns a callable ``seed(project_dir, values=None, **kwargs)`` that writes
    ``ProjectSetting`` rows into ``{project_dir}/.claude/sova.db``, mirroring
    production (config lives in the DB, not sova.toml). This replaces the legacy
    test pattern of writing a ``sova.toml`` file.

    Values may be provided as a mapping and/or keyword arguments. Nested dicts
    are flattened to dot-notation (``{"pipeline": {"auto_handoff": False}}`` ->
    ``pipeline.auto_handoff``); flat dotted keys (``{"pipeline.auto_handoff":
    False}``) are accepted as-is. Values are JSON-encoded to match production
    storage, so native Python types (str, int, float, bool, list) are passed
    naturally at call sites.

    Example::

        def test_something(tmp_path, seed_config):
            seed_config(tmp_path, github_repo="user/repo", pipeline={"auto_handoff": False})
            cfg = load_config(tmp_path)
            assert cfg.github_repo == "user/repo"

    Calling it with no values still creates the DB and ``project_settings``
    table, matching an empty (installed but unconfigured) project.

    Note: ``SOVA_*`` environment variables still take precedence over seeded
    values (env > DB > TOML > defaults), matching production config priority.
    """

    def _seed(project_dir: Path | str, values: dict[str, Any] | None = None, **kwargs: Any) -> None:
        merged: dict[str, Any] = {}
        if values:
            merged.update(values)
        merged.update(kwargs)
        _seed_project_settings(Path(project_dir), merged)

    return _seed
