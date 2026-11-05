"""Tests for DB-backed configuration persistence (sova.config.db_loader)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sova.config.db_loader import (
    _save_config_to_db_sync,
    _save_with_immediate_lock,
    _save_within_transaction,
    _set_nested,
    _try_load_from_db,
    get_setting,
    load_task_queue,
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


class TestResolveDbPath:
    def test_returns_none_for_non_sqlite_env_url(self, monkeypatch) -> None:
        """The sync bridge has no PostgreSQL driver, so a server URL yields no file path."""
        from sova.config.db_loader import _resolve_db_path

        monkeypatch.setenv("SOVA_DATABASE_URL", "postgresql://localhost/sova")
        result = _resolve_db_path(Path("/some/dir"))
        assert result is None

    def test_returns_none_for_in_memory_env_url(self, monkeypatch) -> None:
        from sova.config.db_loader import _resolve_db_path

        monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
        assert _resolve_db_path(Path("/some/dir")) is None

    def test_sqlite_file_env_url_wins_over_project_dir(self, monkeypatch, tmp_path) -> None:
        """Regression: a file URL was ignored, so load_config() lost every stored setting."""
        from sova.config.db_loader import _resolve_db_path

        db_file = tmp_path / "elsewhere.db"
        db_file.touch()
        monkeypatch.setenv("SOVA_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
        assert _resolve_db_path(tmp_path / "project") == db_file

    def test_missing_sqlite_file_env_url_is_none(self, monkeypatch, tmp_path) -> None:
        from sova.config.db_loader import _resolve_db_path

        monkeypatch.setenv("SOVA_DATABASE_URL", f"sqlite:///{tmp_path / 'absent.db'}")
        assert _resolve_db_path(tmp_path) is None

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

    def test_excludes_internal_orphan_sweep_marker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The orphan-edge sweep's own bookkeeping row must never surface as user config."""
        from sqlalchemy import create_engine

        from sova.db.session import _ORPHAN_SWEEP_MARKER_KEY

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
                    {"key": _ORPHAN_SWEEP_MARKER_KEY, "value": "true"},
                    {"key": "github_repo", "value": '"user/repo"'},
                ],
            )
        engine.dispose()

        result = _try_load_from_db(tmp_path)
        assert result == {"github_repo": "user/repo"}


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
    """A set ``SOVA_<SECTION>_<FIELD>`` beats a merged TOML/DB value for every section."""

    @staticmethod
    def _resolve(merged: dict[str, Any]) -> Any:
        from sova.config.loader import _apply_env_overrides
        from sova.config.models import ProjectConfig

        _apply_env_overrides(merged)
        return ProjectConfig(**merged)

    def test_telemetry_env_vars_override(self, monkeypatch) -> None:
        monkeypatch.setenv("SOVA_TELEMETRY_HUB_URL", "https://env.example.com")
        monkeypatch.setenv("SOVA_TELEMETRY_HUB_TOKEN", "env-token")

        cfg = self._resolve({"telemetry": {"hub_url": "https://toml.example.com"}})
        assert cfg.telemetry.hub_url == "https://env.example.com"
        assert cfg.telemetry.hub_token == "env-token"

    def test_env_applies_when_section_absent(self, monkeypatch) -> None:
        monkeypatch.setenv("SOVA_TELEMETRY_HUB_URL", "https://env.example.com")

        cfg = self._resolve({"github_repo": "user/repo"})
        assert cfg.telemetry.hub_url == "https://env.example.com"

    def test_noop_when_section_not_dict(self, monkeypatch) -> None:
        from sova.config.loader import _apply_env_overrides

        monkeypatch.setenv("SOVA_TELEMETRY_HUB_URL", "https://env.example.com")
        merged: dict[str, Any] = {"telemetry": "not-a-dict"}
        _apply_env_overrides(merged)
        assert merged["telemetry"] == "not-a-dict"

    def test_stored_value_kept_when_env_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("SOVA_TELEMETRY_HUB_URL", raising=False)
        monkeypatch.delenv("SOVA_AGENT_MODEL", raising=False)

        cfg = self._resolve({"telemetry": {"hub_url": "https://toml.example.com"}, "agent": {"model": "stored-model"}})
        assert cfg.telemetry.hub_url == "https://toml.example.com"
        assert cfg.agent.model == "stored-model"

    def test_empty_env_value_keeps_stored_value(self, monkeypatch) -> None:
        """A blank SOVA_AGENT_MODEL counts as unset: it must not discard the stored model."""
        monkeypatch.setenv("SOVA_AGENT_MODEL", "")

        cfg = self._resolve({"agent": {"model": "stored-model"}})
        assert cfg.agent.model == "stored-model"

    def test_unlisted_section_field_beats_stored_value(self, monkeypatch) -> None:
        """Regression: SOVA_AGENT_MODEL was ignored whenever agent.model was stored."""
        monkeypatch.setenv("SOVA_AGENT_MODEL", "openai/gpt-6.1-sol")

        cfg = self._resolve({"agent": {"model": "claude-sonnet-5-5", "max_budget": 7}})
        assert cfg.agent.model == "openai/gpt-6.1-sol"
        assert cfg.agent.max_budget == 7

    def test_json_dict_and_list_fields_parse_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("SOVA_LLM_ROUTING", json.dumps({"review": "openai/gpt-6.1-sol"}))
        monkeypatch.setenv("SOVA_AGENT_FALLBACK_MODELS", json.dumps(["openai/gpt-6-sol"]))

        cfg = self._resolve(
            {
                "llm": {"routing": {"review": "claude-opus-5-5", "triage": "claude-haiku-5-5"}},
                "agent": {"fallback_models": ["claude-sonnet-5-5"]},
            }
        )
        assert cfg.llm.routing == {"review": "openai/gpt-6.1-sol"}
        assert cfg.agent.fallback_models == ["openai/gpt-6-sol"]

    def test_nested_sub_section_field_beats_stored_value(self, monkeypatch) -> None:
        """review.panel and mcp.atlassian are nested settings models with their own env prefix."""
        monkeypatch.setenv("SOVA_REVIEW_PANEL_ENABLED", "true")
        monkeypatch.setenv("SOVA_MCP_ATLASSIAN_JIRA_URL", "https://env.example.com")

        cfg = self._resolve(
            {
                "review": {"panel": {"enabled": False, "line_proximity": 5}},
                "mcp": {"atlassian": {"jira_url": "https://stored.example.com"}},
            }
        )
        assert cfg.review.panel.enabled is True
        assert cfg.review.panel.line_proximity == 5
        assert cfg.mcp.atlassian.jira_url == "https://env.example.com"

    def test_does_not_mutate_a_nested_stored_dict(self, monkeypatch) -> None:
        from sova.config.loader import _apply_env_overrides

        monkeypatch.setenv("SOVA_REVIEW_PANEL_ENABLED", "true")
        panel = {"enabled": False, "line_proximity": 5}
        review = {"panel": panel}
        merged: dict[str, Any] = {"review": review}
        _apply_env_overrides(merged)
        assert panel == {"enabled": False, "line_proximity": 5}
        assert review == {"panel": panel}
        assert merged["review"]["panel"] == {"line_proximity": 5}

    def test_does_not_mutate_the_stored_section_dict(self, monkeypatch) -> None:
        """The section dict may be shared with the TOML layer, so it is replaced, not edited."""
        from sova.config.loader import _apply_env_overrides

        monkeypatch.setenv("SOVA_AGENT_MODEL", "env-model")
        stored = {"model": "stored-model"}
        merged: dict[str, Any] = {"agent": stored}
        _apply_env_overrides(merged)
        assert stored == {"model": "stored-model"}
        assert "model" not in merged["agent"]


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

    def test_drops_dead_pipeline_retry_fields(self) -> None:
        """pipeline.max_retries/retry_delay_seconds are silently dropped (auto-retry system removed)."""
        from sova.config.loader import _migrate_deprecated_keys

        flat: dict[str, Any] = {"pipeline": {"max_retries": 3, "retry_delay_seconds": 10, "auto_handoff": False}}
        _migrate_deprecated_keys(flat)
        assert flat == {"pipeline": {"auto_handoff": False}}

    def test_noop_when_no_pipeline_section(self) -> None:
        from sova.config.loader import _migrate_deprecated_keys

        flat: dict[str, Any] = {"github_repo": "user/repo"}
        _migrate_deprecated_keys(flat)
        assert flat == {"github_repo": "user/repo"}


class TestLoadTaskQueue:
    @staticmethod
    async def _load(stored: Any) -> list[int]:
        with (
            patch("sova.db.session.get_session", new=AsyncMock(return_value=MagicMock())),
            patch("sova.config.db_loader.get_setting", new=AsyncMock(return_value=stored)),
        ):
            return await load_task_queue(None)

    async def test_valid_queue_is_returned(self) -> None:
        assert await self._load([3, 1, 2]) == [3, 1, 2]

    @pytest.mark.parametrize(
        "stored",
        [
            pytest.param(None, id="unset"),
            pytest.param("not a list", id="not a list"),
            pytest.param([1, -2], id="negative"),
            pytest.param([0], id="zero"),
            pytest.param([True, 2], id="bool is not an issue number"),
        ],
    )
    async def test_malformed_queue_is_empty(self, stored: Any) -> None:
        assert await self._load(stored) == []


class TestSyncSaveWithoutSettingsTable:
    """A database that predates project_settings is skipped, not failed."""

    @pytest.fixture
    def bare_engine(self, tmp_path: Path):
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{tmp_path / 'bare.db'}")
        yield engine
        engine.dispose()

    def test_immediate_lock_save_returns_when_table_missing(self, bare_engine: Any) -> None:
        assert _save_with_immediate_lock(bare_engine, {}, {"github_repo": "a/b"}) is None

    def test_transaction_save_returns_when_table_missing(self, bare_engine: Any) -> None:
        assert _save_within_transaction(bare_engine, {}, {"github_repo": "a/b"}) is None

    def test_sync_save_swallows_an_engine_failure(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "sova.db").write_bytes(b"")
        with patch("sqlalchemy.create_engine", side_effect=RuntimeError("boom")):
            assert _save_config_to_db_sync(tmp_path, {"github_repo": "a/b"}) is None
