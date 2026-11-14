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
    TaskQueueConflictError,
    _save_config_to_db_sync,
    _save_with_immediate_lock,
    _save_within_transaction,
    _set_nested,
    _try_load_from_db,
    get_setting,
    load_hidden_issues,
    load_task_queue,
    merge_task_queue,
    remove_from_task_queue,
    save_setting,
    save_task_queue,
    set_issue_hidden,
)
from sova.config.loader import _deep_merge
from sova.db.models import Base, ProjectSetting
from sova.db.session import get_session


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


class TestSyncUrlResolution:
    """``_sync_url_for`` + ``_sqlite_file``: the one resolution shared by reads and writes."""

    @staticmethod
    def _file(project_dir):
        from sova.config.db_loader import _sqlite_file, _sync_url_for

        return _sqlite_file(_sync_url_for(project_dir))

    def test_non_sqlite_env_url_names_no_file(self, monkeypatch) -> None:
        monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
        monkeypatch.setenv("SOVA_DATABASE_URL", "postgresql://localhost/sova")
        assert self._file(Path("/some/dir")) is None

    def test_in_memory_env_url_names_no_file(self, monkeypatch) -> None:
        monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
        assert self._file(Path("/some/dir")) is None

    def test_sqlite_file_env_url_wins_over_project_dir(self, monkeypatch, tmp_path) -> None:
        """Regression: a file URL was ignored, so load_config() lost every stored setting."""
        db_file = tmp_path / "elsewhere.db"
        monkeypatch.setenv("SOVA_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
        assert self._file(tmp_path / "project") == db_file

    def test_uses_home_config_when_no_project_dir(self, monkeypatch, tmp_path) -> None:
        """project_dir=None falls back to ~/.config/sova/sova.db."""
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        monkeypatch.setattr("sova.config.db_loader.Path.home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr("sova.db.session.Path.home", staticmethod(lambda: tmp_path))
        assert self._file(None) == tmp_path / ".config" / "sova" / "sova.db"

    def test_resolution_creates_nothing(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("SOVA_DATABASE_URL", f"sqlite:///{tmp_path / 'absent' / 'x.db'}")
        self._file(tmp_path)
        assert not (tmp_path / "absent").exists()


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
        """The removed orphan-edge sweep's row, still in old databases, must never surface as user config."""
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
                    {"key": "db.orphan_edges_swept", "value": "true"},
                    {"key": "github_repo", "value": '"user/repo"'},
                ],
            )
        engine.dispose()

        result = _try_load_from_db(tmp_path)
        assert result == {"github_repo": "user/repo"}

    def test_removed_knowledge_keys_stored_in_the_db_do_not_fail_the_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sqlalchemy import create_engine

        from sova.config.loader import load_config

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        engine = create_engine(f"sqlite:///{claude_dir / 'sova.db'}")
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                ProjectSetting.__table__.insert(),
                [
                    {"key": "github_repo", "value": '"user/repo"'},
                    {"key": "shared_knowledge_dir", "value": '"~/elsewhere"'},
                    {"key": "shared_knowledge_categories", "value": '["review_pattern"]'},
                    {"key": "ignored_shared_hashes", "value": '["abc"]'},
                    {"key": "knowledge.max_context_tokens", "value": "500"},
                ],
            )
        engine.dispose()

        assert load_config(tmp_path).github_repo == "user/repo"


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


class TestSaveTaskQueueInvalidation:
    @staticmethod
    def _session() -> MagicMock:
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=session)
        return session

    async def test_successful_write_invalidates_the_project(self, tmp_path: Path) -> None:
        with (
            patch("sova.db.session.get_session", AsyncMock(return_value=self._session())),
            patch("sova.config.db_loader.save_setting", AsyncMock()),
            patch("sova.config.loader.invalidate_config_cache") as invalidate,
        ):
            await save_task_queue(tmp_path, [1, 2])
        invalidate.assert_called_once_with(tmp_path)

    async def test_failed_write_does_not_invalidate(self, tmp_path: Path) -> None:
        with (
            patch("sova.db.session.get_session", AsyncMock(return_value=self._session())),
            patch("sova.config.db_loader.save_setting", AsyncMock(side_effect=RuntimeError("DB gone"))),
            patch("sova.config.loader.invalidate_config_cache") as invalidate,
            pytest.raises(RuntimeError),
        ):
            await save_task_queue(tmp_path, [1, 2])
        invalidate.assert_not_called()


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


@pytest.mark.usefixtures("reset_db_rows")
class TestRemoveFromTaskQueue:
    """Pruning removes only what it is told to, keeps the order, and writes only on a change."""

    async def test_removes_listed_ids_and_keeps_order(self, tmp_path: Path) -> None:
        await save_task_queue(tmp_path, [30, 10, 20, 40])

        removed = await remove_from_task_queue(tmp_path, [10, 40, 99])

        assert removed == [10, 40]
        assert await load_task_queue(tmp_path) == [30, 20]

    async def test_issue_never_in_the_queue_leaves_it_untouched_and_unsaved(self, tmp_path: Path) -> None:
        await save_task_queue(tmp_path, [1, 2])
        with patch("sova.config.db_loader.save_task_queue", AsyncMock()) as save:
            assert await remove_from_task_queue(tmp_path, [99]) == []
        save.assert_not_awaited()
        assert await load_task_queue(tmp_path) == [1, 2]

    async def test_malformed_entry_does_not_crash_and_is_kept(self, tmp_path: Path) -> None:
        await save_task_queue(tmp_path, [1, "oops", None, 2])  # type: ignore[list-item]

        assert await remove_from_task_queue(tmp_path, [2]) == [2]

        async with await get_session(project_dir=tmp_path) as session:
            assert await get_setting(session, "supervisor.task_queue") == [1, "oops", None]

    async def test_unset_queue_is_a_noop(self, tmp_path: Path) -> None:
        assert await remove_from_task_queue(tmp_path, [1]) == []


def _racing_get_session(concurrent_queues: list[list[int]]) -> Any:
    """A ``get_session`` that saves the next of ``concurrent_queues`` just before each queue write session.

    ``_swap_task_queue()`` opens a read session, then a write session; a save landing before the write is what
    another process writing between the two looks like.
    """
    calls = 0

    async def racing(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls % 2 == 0 and concurrent_queues:
            async with await get_session(**kwargs) as session, session.begin():
                await save_setting(session, "supervisor.task_queue", concurrent_queues.pop(0))
        return await get_session(**kwargs)

    return racing


@pytest.mark.usefixtures("reset_db_rows")
class TestTaskQueueCompareAndSwap:
    """A queue write computed from a stale read is rebuilt on the stored queue, never saved over it."""

    async def test_prune_keeps_an_entry_another_process_added_meanwhile(self, tmp_path: Path) -> None:
        await save_task_queue(tmp_path, [42, 43])

        with patch("sova.db.session.get_session", _racing_get_session([[42, 43, 44]])):
            removed = await remove_from_task_queue(tmp_path, [42])

        assert removed == [42]
        assert await load_task_queue(tmp_path) == [43, 44]

    async def test_based_on_save_keeps_a_concurrent_removal_and_addition(self, tmp_path: Path) -> None:
        await save_task_queue(tmp_path, [1, 2, 3])

        saved = await save_task_queue(tmp_path, [3, 2, 1, 9], based_on=[1, 2, 3, 4])

        # 4 was removed by another writer after the base was read: it stays removed and the rest keeps this order.
        assert saved == [3, 2, 1, 9]
        assert await load_task_queue(tmp_path) == [3, 2, 1, 9]

    async def test_based_on_save_with_no_stored_queue_keeps_the_writers_queue(self, tmp_path: Path) -> None:
        # No row yet (the queue came from sova.toml): nothing was removed by another writer, so nothing is dropped.
        saved = await save_task_queue(tmp_path, [20], based_on=[10, 20])

        assert saved == [20]
        assert await load_task_queue(tmp_path) == [20]

    async def test_based_on_save_rebased_after_a_lost_swap(self, tmp_path: Path) -> None:
        await save_task_queue(tmp_path, [1, 2])

        with patch("sova.db.session.get_session", _racing_get_session([[2, 7]])):
            saved = await save_task_queue(tmp_path, [2, 5], based_on=[1, 2])

        assert saved == [2, 5, 7]
        assert await load_task_queue(tmp_path) == [2, 5, 7]

    async def test_based_on_save_creates_an_unset_queue(self, tmp_path: Path) -> None:
        assert await save_task_queue(tmp_path, [5], based_on=[]) == [5]
        assert await load_task_queue(tmp_path) == [5]

    async def test_a_concurrent_first_write_of_an_unset_queue_is_kept(self, tmp_path: Path) -> None:
        with patch("sova.db.session.get_session", _racing_get_session([[9]])):
            saved = await save_task_queue(tmp_path, [5], based_on=[])

        assert saved == [5, 9]
        assert await load_task_queue(tmp_path) == [5, 9]

    async def test_no_issue_id_removes_nothing_and_reads_nothing(self, tmp_path: Path) -> None:
        with patch("sova.db.session.get_session", AsyncMock()) as session:
            assert await remove_from_task_queue(tmp_path, ["7"]) == []  # type: ignore[list-item]
        session.assert_not_awaited()

    async def test_every_swap_lost_raises_and_saves_nothing_of_its_own(self, tmp_path: Path) -> None:
        await save_task_queue(tmp_path, [1, 2])
        concurrent = [[1, 2, 100 + n] for n in range(8)]

        with (
            patch("sova.db.session.get_session", _racing_get_session(concurrent)),
            pytest.raises(TaskQueueConflictError),
        ):
            await remove_from_task_queue(tmp_path, [1])

        assert await load_task_queue(tmp_path) == [1, 2, 107]

    async def test_unreadable_stored_queue_is_replaced_by_a_based_on_save(self, tmp_path: Path) -> None:
        async with await get_session(project_dir=tmp_path) as session, session.begin():
            session.add(ProjectSetting(key="supervisor.task_queue", value="not json"))

        assert await save_task_queue(tmp_path, [3], based_on=[]) == [3]
        assert await load_task_queue(tmp_path) == [3]


class TestMergeTaskQueue:
    def test_unchanged_store_takes_the_writers_queue(self) -> None:
        assert merge_task_queue([1, 2, 3], [1, 2, 3], [3, 1, 4]) == [3, 1, 4]

    def test_concurrent_addition_is_appended(self) -> None:
        assert merge_task_queue([1, 2, 8], [1, 2], [2, 1]) == [2, 1, 8]

    def test_concurrent_removal_stays_removed(self) -> None:
        assert merge_task_queue([2], [1, 2], [1, 2, 5]) == [2, 5]

    def test_an_entry_both_writers_added_appears_once(self) -> None:
        assert merge_task_queue([1, 5], [1], [1, 5]) == [1, 5]


@pytest.mark.usefixtures("reset_db_rows")
class TestHiddenIssues:
    @staticmethod
    async def _stored(project_dir: Path) -> Any:
        async with await get_session(project_dir=project_dir) as session:
            return await get_setting(session, "dashboard.hidden_issues")

    async def test_hide_and_unhide_are_idempotent_and_persist(self, tmp_path: Path) -> None:
        assert await set_issue_hidden(tmp_path, 5, hidden=True) == [5]
        assert await set_issue_hidden(tmp_path, 5, hidden=True) == [5]
        assert await set_issue_hidden(tmp_path, 7, hidden=False) == [5]
        assert await self._stored(tmp_path) == [5]
        assert await set_issue_hidden(tmp_path, 5, hidden=False) == []
        assert await self._stored(tmp_path) == []

    async def test_unhide_of_a_visible_issue_does_not_write(self, tmp_path: Path) -> None:
        with patch("sova.config.db_loader.save_setting", AsyncMock()) as save:
            assert await set_issue_hidden(tmp_path, 7, hidden=False) == []
        save.assert_not_awaited()

    async def test_failed_read_raises_and_keeps_the_stored_list(self, tmp_path: Path) -> None:
        from sqlalchemy.exc import OperationalError

        await set_issue_hidden(tmp_path, 5, hidden=True)
        real_get_session = get_session

        async def failing_get_session(**kwargs: Any) -> Any:
            session = await real_get_session(**kwargs)
            session.execute = AsyncMock(side_effect=OperationalError("SELECT", {}, Exception("locked")))
            return session

        with (
            patch("sova.db.session.get_session", failing_get_session),
            patch("sova.config.db_loader.save_setting", AsyncMock()) as save,
            pytest.raises(OperationalError),
        ):
            await set_issue_hidden(tmp_path, 9, hidden=True)

        save.assert_not_awaited()
        assert await self._stored(tmp_path) == [5]

    async def test_unreadable_stored_value_is_replaced(self, tmp_path: Path) -> None:
        async with await get_session(project_dir=tmp_path) as session, session.begin():
            session.add(ProjectSetting(key="dashboard.hidden_issues", value="not json"))

        assert await set_issue_hidden(tmp_path, 4, hidden=True) == [4]

    async def test_malformed_entries_are_dropped_and_duplicates_collapsed(self) -> None:
        config = MagicMock()
        config.dashboard.hidden_issues = [3, "x", -1, True, 3, 4]
        with patch("sova.config.loader.load_config", return_value=config):
            assert await load_hidden_issues(None) == [3, 4]

    async def test_a_list_set_in_toml_is_read_when_the_database_has_none(self, tmp_path: Path) -> None:
        (tmp_path / "sova.toml").write_text("[dashboard]\nhidden_issues = [12, 13]\n", encoding="utf-8")

        assert await load_hidden_issues(tmp_path) == [12, 13]
