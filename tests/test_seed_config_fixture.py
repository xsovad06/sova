"""Tests for the shared ``seed_config`` fixture (tests/conftest.py).

Verifies that seeding DB-backed project settings makes ``load_config`` read
them, matching production config behavior (config in .claude/sova.db, not
sova.toml). See issue #557.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from sova.config.loader import load_config


def _read_setting(project_dir: Path, key: str):
    """Read a raw JSON-decoded value straight from the seeded DB."""
    db_path = project_dir / ".claude" / "sova.db"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM project_settings WHERE key = :key"),
                {"key": key},
            ).fetchone()
    finally:
        engine.dispose()
    return json.loads(row[0]) if row is not None else None


def test_creates_db_file(tmp_path: Path, seed_config) -> None:
    seed_config(tmp_path, github_repo="user/repo")
    assert (tmp_path / ".claude" / "sova.db").exists()


def test_scalar_kwarg_read_by_load_config(tmp_path: Path, seed_config) -> None:
    seed_config(tmp_path, github_repo="user/repo")
    cfg = load_config(tmp_path)
    assert cfg.github_repo == "user/repo"


def test_nested_kwarg_read_by_load_config(tmp_path: Path, seed_config) -> None:
    seed_config(tmp_path, github_repo="user/repo", pipeline={"auto_handoff": False})
    cfg = load_config(tmp_path)
    assert cfg.pipeline.auto_handoff is False


def test_flat_dotted_key_dict(tmp_path: Path, seed_config) -> None:
    seed_config(tmp_path, {"pipeline.auto_handoff": False, "base_branch": "develop"})
    cfg = load_config(tmp_path)
    assert cfg.pipeline.auto_handoff is False
    assert cfg.base_branch == "develop"


def test_values_stored_as_json(tmp_path: Path, seed_config) -> None:
    seed_config(
        tmp_path,
        github_repo="user/repo",
        ci={"flaky_checks": ["a", "b"]},
        pipeline={"max_address_review_cycles": 5},
    )
    assert _read_setting(tmp_path, "github_repo") == "user/repo"
    assert _read_setting(tmp_path, "ci.flaky_checks") == ["a", "b"]
    assert _read_setting(tmp_path, "pipeline.max_address_review_cycles") == 5


def test_list_and_int_types_round_trip(tmp_path: Path, seed_config) -> None:
    seed_config(tmp_path, ci={"flaky_checks": ["x"]}, pipeline={"max_address_review_cycles": 7})
    cfg = load_config(tmp_path)
    assert cfg.ci.flaky_checks == ["x"]
    assert cfg.pipeline.max_address_review_cycles == 7


def test_empty_seed_creates_table_and_defaults(tmp_path: Path, seed_config) -> None:
    seed_config(tmp_path)
    assert (tmp_path / ".claude" / "sova.db").exists()
    cfg = load_config(tmp_path)
    # No rows seeded -> defaults apply.
    assert cfg.github_repo == ""
    assert cfg.base_branch == "main"


def test_upsert_updates_existing_key(tmp_path: Path, seed_config) -> None:
    seed_config(tmp_path, github_repo="old/repo")
    seed_config(tmp_path, github_repo="new/repo")
    cfg = load_config(tmp_path)
    assert cfg.github_repo == "new/repo"


def test_mapping_and_kwargs_merge(tmp_path: Path, seed_config) -> None:
    seed_config(tmp_path, {"base_branch": "develop"}, github_repo="user/repo")
    cfg = load_config(tmp_path)
    assert cfg.base_branch == "develop"
    assert cfg.github_repo == "user/repo"


def test_env_var_takes_precedence(tmp_path: Path, seed_config, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_config(tmp_path, github_repo="db/repo")
    monkeypatch.setenv("SOVA_GITHUB_REPO", "env/repo")
    cfg = load_config(tmp_path)
    assert cfg.github_repo == "env/repo"


def test_accepts_string_path(tmp_path: Path, seed_config) -> None:
    seed_config(str(tmp_path), github_repo="user/repo")
    cfg = load_config(tmp_path)
    assert cfg.github_repo == "user/repo"


@pytest.mark.asyncio
async def test_works_in_async_test(tmp_path: Path, seed_config) -> None:
    """The fixture is synchronous and usable from async tests (no event loop clash)."""
    seed_config(tmp_path, github_repo="user/repo")
    cfg = load_config(tmp_path)
    assert cfg.github_repo == "user/repo"
