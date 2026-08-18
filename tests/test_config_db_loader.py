"""Tests for DB-backed configuration persistence (sova.config.db_loader)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sova.config.db_loader import (
    _set_nested,
    _try_load_from_db,
    delete_setting,
    get_setting,
    load_config_from_db,
    save_setting,
)
from sova.config.loader import _deep_merge
from sova.db.models import Base, ProjectSetting


@pytest.fixture
async def db_session():
    """In-memory SQLite session with project_settings table."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


class TestSetNested:
    def test_single_level(self) -> None:
        d: dict[str, Any] = {}
        _set_nested(d, "github_repo", "user/repo")
        assert d == {"github_repo": "user/repo"}

    def test_two_levels(self) -> None:
        d: dict[str, Any] = {}
        _set_nested(d, "pipeline.auto_handoff", True)
        assert d == {"pipeline": {"auto_handoff": True}}

    def test_three_levels(self) -> None:
        d: dict[str, Any] = {}
        _set_nested(d, "external_reviews.sonarcloud.project_key", "my-key")
        assert d == {"external_reviews": {"sonarcloud": {"project_key": "my-key"}}}

    def test_preserves_existing(self) -> None:
        d: dict[str, Any] = {"pipeline": {"auto_handoff": True}}
        _set_nested(d, "pipeline.auto_address_review", False)
        assert d == {"pipeline": {"auto_handoff": True, "auto_address_review": False}}

    def test_overwrites_non_dict(self) -> None:
        d: dict[str, Any] = {"pipeline": "old_value"}
        _set_nested(d, "pipeline.auto_handoff", True)
        assert d == {"pipeline": {"auto_handoff": True}}


class TestLoadConfigFromDB:
    @pytest.mark.asyncio
    async def test_returns_none_on_empty_table(self, db_session: AsyncSession) -> None:
        result = await load_config_from_db(db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_loads_flat_settings(self, db_session: AsyncSession) -> None:
        db_session.add(ProjectSetting(key="github_repo", value='"user/repo"'))
        db_session.add(ProjectSetting(key="base_branch", value='"develop"'))
        await db_session.flush()

        result = await load_config_from_db(db_session)
        assert result == {"github_repo": "user/repo", "base_branch": "develop"}

    @pytest.mark.asyncio
    async def test_loads_nested_settings(self, db_session: AsyncSession) -> None:
        db_session.add(ProjectSetting(key="pipeline.auto_handoff", value="false"))
        db_session.add(ProjectSetting(key="agent.max_budget", value="5.0"))
        await db_session.flush()

        result = await load_config_from_db(db_session)
        assert result == {
            "pipeline": {"auto_handoff": False},
            "agent": {"max_budget": 5.0},
        }

    @pytest.mark.asyncio
    async def test_loads_deeply_nested(self, db_session: AsyncSession) -> None:
        db_session.add(ProjectSetting(key="external_reviews.sonarcloud.project_key", value='"my-key"'))
        await db_session.flush()

        result = await load_config_from_db(db_session)
        assert result == {"external_reviews": {"sonarcloud": {"project_key": "my-key"}}}

    @pytest.mark.asyncio
    async def test_skips_invalid_json(self, db_session: AsyncSession) -> None:
        db_session.add(ProjectSetting(key="good_key", value='"good_value"'))
        db_session.add(ProjectSetting(key="bad_key", value="not valid json {{{"))
        await db_session.flush()

        result = await load_config_from_db(db_session)
        assert result == {"good_key": "good_value"}

    @pytest.mark.asyncio
    async def test_returns_none_when_all_invalid_json(self, db_session: AsyncSession) -> None:
        db_session.add(ProjectSetting(key="bad", value="not json"))
        await db_session.flush()

        result = await load_config_from_db(db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_handles_list_values(self, db_session: AsyncSession) -> None:
        db_session.add(ProjectSetting(key="ci.flaky_checks", value='["check1", "check2"]'))
        await db_session.flush()

        result = await load_config_from_db(db_session)
        assert result == {"ci": {"flaky_checks": ["check1", "check2"]}}

    @pytest.mark.asyncio
    async def test_handles_boolean_values(self, db_session: AsyncSession) -> None:
        db_session.add(ProjectSetting(key="review.enabled", value="true"))
        db_session.add(ProjectSetting(key="pipeline.auto_handoff", value="false"))
        await db_session.flush()

        result = await load_config_from_db(db_session)
        assert result == {
            "review": {"enabled": True},
            "pipeline": {"auto_handoff": False},
        }

    @pytest.mark.asyncio
    async def test_returns_none_on_missing_table(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            result = await load_config_from_db(session)
        await engine.dispose()
        assert result is None


class TestSaveSetting:
    @pytest.mark.asyncio
    async def test_insert_new(self, db_session: AsyncSession) -> None:
        await save_setting(db_session, "pipeline.auto_handoff", False)
        await db_session.commit()

        row = await db_session.execute(
            text("SELECT key, value FROM project_settings WHERE key = 'pipeline.auto_handoff'")
        )
        result = row.one()
        assert result.key == "pipeline.auto_handoff"
        assert json.loads(result.value) is False

    @pytest.mark.asyncio
    async def test_upsert_existing(self, db_session: AsyncSession) -> None:
        await save_setting(db_session, "github_repo", "old/repo")
        await db_session.commit()

        await save_setting(db_session, "github_repo", "new/repo")
        await db_session.commit()

        val = await get_setting(db_session, "github_repo")
        assert val == "new/repo"

    @pytest.mark.asyncio
    async def test_save_list(self, db_session: AsyncSession) -> None:
        await save_setting(db_session, "ci.flaky_checks", ["a", "b"])
        await db_session.commit()

        val = await get_setting(db_session, "ci.flaky_checks")
        assert val == ["a", "b"]

    @pytest.mark.asyncio
    async def test_save_integer(self, db_session: AsyncSession) -> None:
        await save_setting(db_session, "ci.poll_interval", 120)
        await db_session.commit()

        val = await get_setting(db_session, "ci.poll_interval")
        assert val == 120

    @pytest.mark.asyncio
    async def test_save_string(self, db_session: AsyncSession) -> None:
        await save_setting(db_session, "github_repo", "user/repo")
        await db_session.commit()

        val = await get_setting(db_session, "github_repo")
        assert val == "user/repo"


class TestDeleteSetting:
    @pytest.mark.asyncio
    async def test_delete_existing(self, db_session: AsyncSession) -> None:
        await save_setting(db_session, "github_repo", "user/repo")
        await db_session.commit()

        result = await delete_setting(db_session, "github_repo")
        assert result is True

        val = await get_setting(db_session, "github_repo")
        assert val is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, db_session: AsyncSession) -> None:
        result = await delete_setting(db_session, "nonexistent")
        assert result is False


class TestGetSetting:
    @pytest.mark.asyncio
    async def test_get_existing(self, db_session: AsyncSession) -> None:
        db_session.add(ProjectSetting(key="agent.model", value='"sonnet"'))
        await db_session.flush()

        val = await get_setting(db_session, "agent.model")
        assert val == "sonnet"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, db_session: AsyncSession) -> None:
        val = await get_setting(db_session, "nonexistent")
        assert val is None

    @pytest.mark.asyncio
    async def test_get_invalid_json(self, db_session: AsyncSession) -> None:
        db_session.add(ProjectSetting(key="bad", value="not json"))
        await db_session.flush()

        val = await get_setting(db_session, "bad")
        assert val is None

    @pytest.mark.asyncio
    async def test_get_on_missing_table(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            val = await get_setting(session, "key")
        await engine.dispose()
        assert val is None


class TestTryLoadFromDB:
    def test_returns_none_on_missing_db(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        result = _try_load_from_db(tmp_path / "nonexistent")
        assert result is None

    def test_returns_none_when_no_db_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        result = _try_load_from_db(tmp_path)
        assert result is None


class TestDeepMerge:
    def test_basic_merge(self) -> None:
        base = {"a": 1, "b": 2}
        overrides = {"b": 3, "c": 4}
        assert _deep_merge(base, overrides) == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self) -> None:
        base = {"pipeline": {"auto_handoff": True, "max_address_review_cycles": 2}}
        overrides = {"pipeline": {"auto_handoff": False}}
        result = _deep_merge(base, overrides)
        assert result == {"pipeline": {"auto_handoff": False, "max_address_review_cycles": 2}}

    def test_list_replaced_wholesale(self) -> None:
        base = {"ci": {"flaky_checks": ["a", "b"]}}
        overrides = {"ci": {"flaky_checks": ["c"]}}
        result = _deep_merge(base, overrides)
        assert result == {"ci": {"flaky_checks": ["c"]}}

    def test_override_adds_new_section(self) -> None:
        base = {"github_repo": "user/repo"}
        overrides = {"pipeline": {"auto_handoff": False}}
        result = _deep_merge(base, overrides)
        assert result == {"github_repo": "user/repo", "pipeline": {"auto_handoff": False}}

    def test_does_not_mutate_base(self) -> None:
        base = {"a": {"b": 1}}
        overrides = {"a": {"c": 2}}
        _deep_merge(base, overrides)
        assert base == {"a": {"b": 1}}


class TestLoadConfigWithDB:
    @pytest.mark.asyncio
    async def test_db_overrides_toml(self, tmp_path) -> None:
        toml_content = """
[project]
github_repo = "user/repo"
base_branch = "main"

[pipeline]
auto_handoff = true
"""
        (tmp_path / "sova.toml").write_text(toml_content)

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            await save_setting(session, "pipeline.auto_handoff", False)
            await save_setting(session, "base_branch", "develop")
            await session.commit()

            result = await load_config_from_db(session)

        await engine.dispose()

        assert result is not None
        assert result["pipeline"]["auto_handoff"] is False
        assert result["base_branch"] == "develop"

    def test_load_config_without_db_falls_back_to_toml(self, tmp_path, monkeypatch) -> None:
        toml_content = """
[project]
github_repo = "user/repo"
"""
        (tmp_path / "sova.toml").write_text(toml_content)

        from sova.config import db_loader

        monkeypatch.setattr(db_loader, "_try_load_from_db", lambda _pd: None)

        from sova.config.loader import load_config

        cfg = load_config(tmp_path)
        assert cfg.github_repo == "user/repo"

    def test_load_config_db_overrides_toml_values(self, tmp_path, monkeypatch) -> None:
        toml_content = """
[project]
github_repo = "user/repo"
base_branch = "main"

[pipeline]
auto_handoff = true
auto_address_review = true
"""
        (tmp_path / "sova.toml").write_text(toml_content)

        from sova.config import db_loader

        monkeypatch.setattr(
            db_loader,
            "_try_load_from_db",
            lambda _pd: {"pipeline": {"auto_handoff": False}, "base_branch": "develop"},
        )

        from sova.config.loader import load_config

        cfg = load_config(tmp_path)
        assert cfg.github_repo == "user/repo"
        assert cfg.base_branch == "develop"
        assert cfg.pipeline.auto_handoff is False
        assert cfg.pipeline.auto_address_review is True

    def test_load_config_no_toml_with_db(self, tmp_path, monkeypatch) -> None:
        from sova.config import db_loader

        monkeypatch.setattr(
            db_loader,
            "_try_load_from_db",
            lambda _pd: {"github_repo": "db/repo", "pipeline": {"auto_handoff": False}},
        )

        from sova.config.loader import load_config

        cfg = load_config(tmp_path)
        assert cfg.github_repo == "db/repo"
        assert cfg.pipeline.auto_handoff is False

    def test_load_config_no_toml_no_db(self, tmp_path, monkeypatch) -> None:
        from sova.config import db_loader

        monkeypatch.setattr(db_loader, "_try_load_from_db", lambda _pd: None)

        from sova.config.loader import load_config

        cfg = load_config(tmp_path)
        assert cfg.github_repo == ""
        assert cfg.base_branch == "main"


class TestLoadConfigFromDBExceptionPath:
    @pytest.mark.asyncio
    async def test_returns_none_on_generic_exception(self) -> None:
        """Generic exception path in load_config_from_db."""
        from unittest.mock import AsyncMock, MagicMock

        session = MagicMock(spec=AsyncSession)
        session.execute = AsyncMock(side_effect=RuntimeError("unexpected"))
        session.rollback = AsyncMock()

        result = await load_config_from_db(session)
        assert result is None
        session.rollback.assert_awaited_once()


class TestResolveDbPath:
    def test_returns_none_when_env_var_set(self, monkeypatch) -> None:
        """SOVA_DATABASE_URL env var skips file-based path."""
        from sova.config.db_loader import _resolve_db_path

        monkeypatch.setenv("SOVA_DATABASE_URL", "postgresql://localhost/sova")
        result = _resolve_db_path(Path("/some/dir"))
        assert result is None

    def test_uses_home_config_when_no_project_dir(self, monkeypatch, tmp_path) -> None:
        """project_dir=None falls back to ~/.config/sova/sova.db."""
        from sova.config.db_loader import _resolve_db_path

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        config_dir = tmp_path / ".config" / "sova"
        config_dir.mkdir(parents=True)
        db_file = config_dir / "sova.db"
        db_file.touch()

        monkeypatch.setattr("sova.config.db_loader.Path.home", staticmethod(lambda: tmp_path))
        result = _resolve_db_path(None)
        assert result == db_file

    def test_returns_none_when_home_db_missing(self, monkeypatch, tmp_path) -> None:
        from sova.config.db_loader import _resolve_db_path

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        monkeypatch.setattr("sova.config.db_loader.Path.home", staticmethod(lambda: tmp_path))
        result = _resolve_db_path(None)
        assert result is None


class TestTryLoadFromDBSyncBridge:
    def test_loads_settings_from_real_db(self, tmp_path, monkeypatch) -> None:
        """Sync bridge happy path with a real SQLite DB."""
        from sqlalchemy import create_engine

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db_path = claude_dir / "sova.db"

        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                ProjectSetting.__table__.insert(),
                [
                    {"key": "github_repo", "value": '"test/repo"'},
                    {"key": "pipeline.auto_handoff", "value": "false"},
                ],
            )
        engine.dispose()

        result = _try_load_from_db(tmp_path)
        assert result is not None
        assert result["github_repo"] == "test/repo"
        assert result["pipeline"]["auto_handoff"] is False

    def test_returns_none_when_table_missing(self, tmp_path, monkeypatch) -> None:
        """Table probe fails, returns None."""
        from sqlalchemy import create_engine

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db_path = claude_dir / "sova.db"

        engine = create_engine(f"sqlite:///{db_path}")
        engine.dispose()
        db_path.touch()

        result = _try_load_from_db(tmp_path)
        assert result is None

    def test_returns_none_when_rows_empty(self, tmp_path, monkeypatch) -> None:
        """Table exists but empty."""
        from sqlalchemy import create_engine

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db_path = claude_dir / "sova.db"

        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        engine.dispose()

        result = _try_load_from_db(tmp_path)
        assert result is None

    def test_skips_invalid_json_in_sync_path(self, tmp_path, monkeypatch) -> None:
        """Invalid JSON rows skipped, valid ones kept."""
        from sqlalchemy import create_engine

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db_path = claude_dir / "sova.db"

        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                ProjectSetting.__table__.insert(),
                [
                    {"key": "good_key", "value": '"good_value"'},
                    {"key": "bad_key", "value": "not valid json {{{"},
                ],
            )
        engine.dispose()

        result = _try_load_from_db(tmp_path)
        assert result == {"good_key": "good_value"}

    def test_returns_none_when_all_json_invalid(self, tmp_path, monkeypatch) -> None:
        """All rows have invalid JSON, returns None."""
        from sqlalchemy import create_engine

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db_path = claude_dir / "sova.db"

        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                ProjectSetting.__table__.insert(),
                [{"key": "bad", "value": "not json"}],
            )
        engine.dispose()

        result = _try_load_from_db(tmp_path)
        assert result is None

    def test_returns_none_on_generic_exception(self, tmp_path, monkeypatch) -> None:
        """Outer except catches unexpected errors."""
        from unittest.mock import patch

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "sova.db").touch()

        with patch("sqlalchemy.create_engine", side_effect=RuntimeError("boom")):
            result = _try_load_from_db(tmp_path)
        assert result is None


class TestFlattenTomlResourcesMigration:
    def test_migrates_resources_to_memory_guard(self) -> None:
        """Deprecated [resources] section migrated to memory_guard."""
        from sova.config.loader import _flatten_toml

        data = {
            "resources": {
                "memory_block_threshold_gb": 2,
                "memory_warn_threshold_gb": 4,
            }
        }
        result = _flatten_toml(data)
        assert result["memory_guard"]["block_threshold_gb"] == 2
        assert result["memory_guard"]["warn_threshold_gb"] == 4

    def test_does_not_overwrite_existing_guard_keys(self) -> None:
        from sova.config.loader import _flatten_toml

        data = {
            "memory_guard": {"block_threshold_gb": 10},
            "resources": {"memory_block_threshold_gb": 2},
        }
        result = _flatten_toml(data)
        assert result["memory_guard"]["block_threshold_gb"] == 10


class TestFlattenTomlRootLevelKeys:
    def test_picks_up_known_root_fields(self) -> None:
        """Root-level known fields passed through."""
        from sova.config.loader import _flatten_toml

        data = {"github_repo": "user/repo", "base_branch": "develop"}
        result = _flatten_toml(data)
        assert result["github_repo"] == "user/repo"
        assert result["base_branch"] == "develop"

    def test_filters_unknown_root_keys(self) -> None:
        from sova.config.loader import _flatten_toml

        data = {"github_repo": "user/repo", "unknown_custom_key": "ignored"}
        result = _flatten_toml(data)
        assert "unknown_custom_key" not in result


class TestApplyEnvOverrides:
    def test_telemetry_env_vars_override(self, monkeypatch) -> None:
        """Telemetry env vars win over merged config."""
        from sova.config.loader import _apply_env_overrides

        monkeypatch.setenv("SOVA_TELEMETRY_HUB_URL", "https://env.example.com")
        monkeypatch.setenv("SOVA_TELEMETRY_HUB_TOKEN", "env-token")

        merged: dict[str, Any] = {"telemetry": {"hub_url": "https://toml.example.com"}}
        _apply_env_overrides(merged)
        assert merged["telemetry"]["hub_url"] == "https://env.example.com"
        assert merged["telemetry"]["hub_token"] == "env-token"

    def test_creates_telemetry_section_when_env_set(self, monkeypatch) -> None:
        """Telemetry section created when env vars set but section absent."""
        from sova.config.loader import _apply_env_overrides

        monkeypatch.setenv("SOVA_TELEMETRY_HUB_URL", "https://env.example.com")
        merged: dict[str, Any] = {"github_repo": "user/repo"}
        _apply_env_overrides(merged)
        assert merged["telemetry"]["hub_url"] == "https://env.example.com"

    def test_noop_when_telemetry_not_dict(self, monkeypatch) -> None:
        from sova.config.loader import _apply_env_overrides

        monkeypatch.setenv("SOVA_TELEMETRY_HUB_URL", "https://env.example.com")
        merged: dict[str, Any] = {"telemetry": "not-a-dict"}
        _apply_env_overrides(merged)
        assert merged["telemetry"] == "not-a-dict"

    def test_no_change_when_env_vars_unset(self, monkeypatch) -> None:
        from sova.config.loader import _apply_env_overrides

        monkeypatch.delenv("SOVA_TELEMETRY_HUB_URL", raising=False)
        monkeypatch.delenv("SOVA_TELEMETRY_HUB_TOKEN", raising=False)
        monkeypatch.delenv("SOVA_TELEMETRY_MACHINE_ID", raising=False)

        merged: dict[str, Any] = {"telemetry": {"hub_url": "https://toml.example.com"}}
        _apply_env_overrides(merged)
        assert merged["telemetry"]["hub_url"] == "https://toml.example.com"


class TestMigrateDeprecatedKeys:
    def test_migrates_no_ai_coauthor(self) -> None:
        """commit.no_ai_coauthor migrated to commit.ai_coauthor (inverted)."""
        from sova.config.loader import _migrate_deprecated_keys

        flat: dict[str, Any] = {"commit": {"no_ai_coauthor": True}}
        _migrate_deprecated_keys(flat)
        assert flat["commit"]["ai_coauthor"] is False
        assert "no_ai_coauthor" not in flat["commit"]

    def test_does_not_overwrite_explicit_ai_coauthor(self) -> None:
        from sova.config.loader import _migrate_deprecated_keys

        flat: dict[str, Any] = {"commit": {"no_ai_coauthor": True, "ai_coauthor": True}}
        _migrate_deprecated_keys(flat)
        assert flat["commit"]["ai_coauthor"] is True

    def test_noop_when_no_commit_section(self) -> None:
        from sova.config.loader import _migrate_deprecated_keys

        flat: dict[str, Any] = {"github_repo": "user/repo"}
        _migrate_deprecated_keys(flat)
        assert flat == {"github_repo": "user/repo"}
