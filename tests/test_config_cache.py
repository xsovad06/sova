"""Tests for the load_config() cache and the sync database URL derivation (issue #1380)."""

from __future__ import annotations

import ast
import copy
import importlib.util
import sqlite3
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from sova.config import db_loader, loader
from sova.config.db_loader import (
    UnsupportedDatabaseError,
    _async_to_sync_url,
    _try_load_from_db,
    config_db_unreadable_reason,
    config_fingerprint,
    touch_settings_stamp,
)
from sova.config.loader import invalidate_config_cache, load_config
from sova.db.models import Base, ProjectSetting

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET = "hunter2-secret"


def _make_db(db_path: Path, rows: dict[str, str]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(ProjectSetting.__table__.delete())
        conn.execute(ProjectSetting.__table__.insert(), [{"key": k, "value": v} for k, v in rows.items()])
    engine.dispose()


class TestAsyncToSyncUrl:
    @pytest.mark.parametrize(
        ("async_url", "expected_driver"),
        [
            ("sqlite+aiosqlite:///tmp/x.db", "sqlite"),
            ("sqlite:///tmp/x.db", "sqlite"),
            ("postgresql+asyncpg://u:pw@host/db", "postgresql+psycopg"),
            ("postgresql://u:pw@host/db", "postgresql+psycopg"),
            ("postgresql+psycopg://u:pw@host/db", "postgresql+psycopg"),
        ],
    )
    def test_maps_each_scheme(self, async_url: str, expected_driver: str, monkeypatch) -> None:
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
        assert _async_to_sync_url(async_url).drivername == expected_driver

    def test_keeps_host_database_and_password(self, monkeypatch) -> None:
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
        url = _async_to_sync_url("postgresql+asyncpg://user:pw@db.example:5433/sova")
        assert (url.username, url.password, url.host, url.port, url.database) == (
            "user",
            "pw",
            "db.example",
            5433,
            "sova",
        )

    def test_unsupported_dialect_names_dialect_not_password(self) -> None:
        with pytest.raises(UnsupportedDatabaseError) as exc_info:
            _async_to_sync_url(f"mysql+aiomysql://user:{SECRET}@host/db")
        assert "mysql+aiomysql" in str(exc_info.value)
        assert SECRET not in str(exc_info.value)

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("ssl=require", {"sslmode": "require"}),
            ("ssl=true", {"sslmode": "require"}),
            ("ssl=false", {"sslmode": "disable"}),
            ("ssl=verify-full", {"sslmode": "verify-full"}),
            ("ssl=require&sslmode=verify-ca", {"sslmode": "verify-ca"}),
            ("prepared_statement_cache_size=0", {}),
            ("prepared_statement_name_func=f&statement_cache_size=0", {}),
            (
                "ssl=require&prepared_statement_cache_size=0&application_name=sova",
                {"sslmode": "require", "application_name": "sova"},
            ),
            ("sslmode=require&connect_timeout=5", {"sslmode": "require", "connect_timeout": "5"}),
        ],
    )
    def test_asyncpg_query_is_rewritten_for_libpq(self, query: str, expected: dict, monkeypatch) -> None:
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
        url = _async_to_sync_url(f"postgresql+asyncpg://u:pw@host/db?{query}")
        assert dict(url.query) == expected

    def test_untranslatable_ssl_value_names_parameter_not_url(self, monkeypatch) -> None:
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
        with pytest.raises(UnsupportedDatabaseError) as exc_info:
            _async_to_sync_url(f"postgresql+asyncpg://u:{SECRET}@host/db?ssl=%2Fpath%2Fto%2Fctx")
        assert "'ssl'" in str(exc_info.value)
        assert SECRET not in str(exc_info.value)
        assert "/path/to/ctx" not in str(exc_info.value)

    def test_sqlite_query_is_left_alone(self) -> None:
        assert dict(_async_to_sync_url("sqlite+aiosqlite:///tmp/x.db?ssl=require").query) == {"ssl": "require"}

    def test_missing_psycopg_names_the_extra(self, monkeypatch) -> None:
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        with pytest.raises(UnsupportedDatabaseError) as exc_info:
            _async_to_sync_url(f"postgresql+asyncpg://user:{SECRET}@host/db")
        assert "sova[postgres]" in str(exc_info.value)
        assert SECRET not in str(exc_info.value)

    def test_invalid_url_does_not_leak_it(self) -> None:
        with pytest.raises(UnsupportedDatabaseError) as exc_info:
            _async_to_sync_url(f"not a url {SECRET}")
        assert SECRET not in str(exc_info.value)


class TestEnvUrlReadsSameDatabaseAsWrites:
    def test_env_sqlite_url_overrides_project_db(self, tmp_path, monkeypatch) -> None:
        env_db = tmp_path / "elsewhere.db"
        _make_db(env_db, {"base_branch": '"from-env-db"'})
        _make_db(tmp_path / "proj" / ".claude" / "sova.db", {"base_branch": '"from-project-db"'})
        monkeypatch.setenv("SOVA_DATABASE_URL", f"sqlite+aiosqlite:///{env_db}")
        assert load_config(tmp_path / "proj").base_branch == "from-env-db"

    def test_missing_env_file_is_not_created(self, tmp_path, monkeypatch) -> None:
        env_db = tmp_path / "absent" / "new.db"
        monkeypatch.setenv("SOVA_DATABASE_URL", f"sqlite+aiosqlite:///{env_db}")
        assert _try_load_from_db(tmp_path) is None
        assert not env_db.exists()
        assert not env_db.parent.exists()

    async def test_config_set_then_load_round_trip(self, tmp_path, monkeypatch) -> None:
        """A write through the settings service is read back by load_config() from the env-URL file."""
        from sova.dashboard.services.settings_service import update_config

        env_db = tmp_path / "round-trip.db"
        _make_db(env_db, {"github_user": '"someone"'})
        monkeypatch.setenv("SOVA_DATABASE_URL", f"sqlite+aiosqlite:///{env_db}")
        project = tmp_path / "proj"
        project.mkdir()

        assert load_config(project).base_branch == "main"
        result = await update_config(project, key="base_branch", value="develop")
        assert result.get("status") == "ok", result
        assert load_config(project).base_branch == "develop"
        assert not (project / ".claude" / "sova.db").exists()

    def test_unsupported_dialect_warns_once_and_falls_back(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("SOVA_DATABASE_URL", f"mysql+aiomysql://user:{SECRET}@host/db")
        messages: list[str] = []
        monkeypatch.setattr(db_loader.logger, "error", messages.append)
        assert _try_load_from_db(tmp_path) is None
        assert _try_load_from_db(tmp_path) is None
        assert len(messages) == 1
        assert SECRET not in messages[0]
        assert load_config(tmp_path).base_branch == "main"

    def test_unreadable_reason_set_for_unsupported_dialect_only(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("SOVA_DATABASE_URL", f"mysql+aiomysql://user:{SECRET}@host/db")
        reason = config_db_unreadable_reason(tmp_path)
        assert reason is not None
        assert SECRET not in reason
        monkeypatch.setenv("SOVA_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
        assert config_db_unreadable_reason(tmp_path) is None

    async def test_update_config_refuses_unreadable_database(self, tmp_path, monkeypatch) -> None:
        from sova.dashboard.services.settings_service import update_config, update_config_many

        monkeypatch.setenv("SOVA_DATABASE_URL", f"mysql+aiomysql://user:{SECRET}@host/db")
        single = await update_config(tmp_path, key="base_branch", value="develop")
        many = await update_config_many(tmp_path, {"base_branch": "develop"})
        for result in (single, many):
            assert result["unreadable_database"] is True
            assert SECRET not in result["error"]

    def test_cli_config_set_exits_1_for_unsupported_dialect(self, tmp_path, monkeypatch) -> None:
        from typer.testing import CliRunner

        from sova.cli.commands.config import config_app

        monkeypatch.setenv("SOVA_DATABASE_URL", f"mysql+aiomysql://user:{SECRET}@host/db")
        result = CliRunner().invoke(config_app, ["set", "base_branch", "develop", "--project", str(tmp_path)])
        assert result.exit_code == 1
        assert SECRET not in result.output


class TestEngineKwargs:
    def test_postgres_gets_the_short_default_connect_timeout(self) -> None:
        kwargs = db_loader._engine_kwargs(db_loader.make_url("postgresql+psycopg://u:pw@host/db"))
        assert kwargs == {"connect_args": {"connect_timeout": db_loader.PG_CONNECT_TIMEOUT_S}}

    def test_explicit_connect_timeout_in_the_url_is_not_replaced(self) -> None:
        kwargs = db_loader._engine_kwargs(db_loader.make_url("postgresql+psycopg://u:pw@host/db?connect_timeout=30"))
        assert "connect_timeout" not in kwargs.get("connect_args", {})

    def test_sqlite_keeps_its_thread_and_lock_settings(self) -> None:
        kwargs = db_loader._engine_kwargs(db_loader.make_url("sqlite:///x.db"))
        assert kwargs == {"connect_args": {"check_same_thread": False, "timeout": 5}}


class TestPsycopgAvailabilityCache:
    def test_driver_lookup_runs_once_per_process(self, monkeypatch) -> None:
        calls: list[str] = []

        def fake_find_spec(name: str):
            calls.append(name)
            return None

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
        for _ in range(5):
            assert db_loader._psycopg_available() is False
        assert calls == ["psycopg"]


class TestConfigCache:
    def _project(self, tmp_path: Path, monkeypatch, rows: dict[str, str] | None = None) -> Path:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        project = tmp_path / "proj"
        _make_db(project / ".claude" / "sova.db", rows or {"base_branch": '"one"'})
        return project

    def test_second_load_skips_the_build(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        builds: list[Path] = []
        real = loader._build_config
        monkeypatch.setattr(loader, "_build_config", lambda p: builds.append(p) or real(p))
        load_config(project)
        load_config(project)
        assert len(builds) == 1

    def test_returned_config_is_a_private_copy(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        first = load_config(project)
        first.base_branch = "mutated"
        first.agent.model = "mutated-model"
        second = load_config(project)
        assert second.base_branch == "one"
        assert second.agent.model != "mutated-model"
        assert second is not first

    def test_write_from_another_process_is_seen(self, tmp_path, monkeypatch) -> None:
        """A settings writer touches the stamp after committing; a running server's cache notices it."""
        project = self._project(tmp_path, monkeypatch)
        assert load_config(project).base_branch == "one"
        _make_db(project / ".claude" / "sova.db", {"base_branch": '"two"'})
        touch_settings_stamp(project.resolve())
        assert load_config(project).base_branch == "two"

    def test_invalidate_touches_the_stamp_for_other_processes(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        before = config_fingerprint(project.resolve())
        invalidate_config_cache(project)
        assert config_fingerprint(project.resolve()) != before
        assert (project / ".claude" / "sova.db.settings-stamp").is_file()

    def test_unrelated_database_writes_do_not_change_the_fingerprint(self, tmp_path, monkeypatch) -> None:
        """Run output, TaskRun and cost writes share the WAL database but are not a settings change."""
        project = self._project(tmp_path, monkeypatch)
        touch_settings_stamp(project.resolve())
        before = config_fingerprint(project.resolve())
        db_file = project / ".claude" / "sova.db"
        conn = sqlite3.connect(db_file)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS unrelated (x TEXT)")
        conn.execute("INSERT INTO unrelated VALUES ('run output')")
        conn.commit()
        conn.close()
        (project / ".claude" / "sova.db-wal").write_bytes(b"x")
        assert config_fingerprint(project.resolve()) == before

    def test_unrelated_table_write_between_loads_costs_one_build(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        builds: list[Path] = []
        real = loader._build_config
        monkeypatch.setattr(loader, "_build_config", lambda p: builds.append(p) or real(p))
        load_config(project)
        conn = sqlite3.connect(project / ".claude" / "sova.db")
        conn.execute("CREATE TABLE IF NOT EXISTS unrelated (x TEXT)")
        conn.execute("INSERT INTO unrelated VALUES ('cost record')")
        conn.commit()
        conn.close()
        load_config(project)
        assert len(builds) == 1

    def test_replaced_database_file_changes_fingerprint(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        before = config_fingerprint(project.resolve())
        db_file = project / ".claude" / "sova.db"
        replacement = project / ".claude" / "replacement.db"
        _make_db(replacement, {"base_branch": '"other"'})
        db_file.unlink()
        replacement.rename(db_file)
        assert config_fingerprint(project.resolve()) != before

    def test_toml_edit_is_seen(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        (tmp_path / "sova.toml").write_text('base_branch = "a"\n', encoding="utf-8")
        assert load_config(tmp_path).base_branch == "a"
        (tmp_path / "sova.toml").write_text('base_branch = "bb"\n', encoding="utf-8")
        assert load_config(tmp_path).base_branch == "bb"

    def test_env_change_is_seen(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        assert load_config(project).base_branch == "one"
        monkeypatch.setenv("SOVA_BASE_BRANCH", "from-env")
        assert load_config(project).base_branch == "from-env"

    def test_projects_have_separate_entries(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        a, b = tmp_path / "a", tmp_path / "b"
        _make_db(a / ".claude" / "sova.db", {"base_branch": '"alpha"'})
        _make_db(b / ".claude" / "sova.db", {"base_branch": '"beta"'})
        assert load_config(a).base_branch == "alpha"
        assert load_config(b).base_branch == "beta"
        assert load_config(a).base_branch == "alpha"

    def test_invalidate_forces_a_rebuild(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        builds: list[Path] = []
        real = loader._build_config
        monkeypatch.setattr(loader, "_build_config", lambda p: builds.append(p) or real(p))
        load_config(project)
        invalidate_config_cache(project)
        load_config(project)
        assert len(builds) == 2

    def test_invalidation_during_rebuild_is_not_overwritten(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        real = loader._build_config

        def build_then_invalidate(p: Path):
            config = real(p)
            invalidate_config_cache(p)  # a writer commits while this rebuild is running
            return config

        monkeypatch.setattr(loader, "_build_config", build_then_invalidate)
        load_config(project)
        assert loader._cache == {}

    def test_locked_database_serves_last_good_entry(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        assert load_config(project).base_branch == "one"
        _make_db(project / ".claude" / "sova.db", {"base_branch": '"changed-value"'})
        touch_settings_stamp(project.resolve())

        def locked(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr("sqlalchemy.create_engine", locked)
        warnings: list[str] = []
        monkeypatch.setattr(loader.logger, "warning", lambda event, **kw: warnings.append(event))
        assert load_config(project).base_branch == "one"
        assert warnings == ["loader.db_unreachable_serving_last_good_config"]

    def test_locked_table_probe_is_a_failure_not_an_empty_database(self, tmp_path, monkeypatch) -> None:
        """A lock hit while checking for the table must serve the last good entry, not cache defaults."""
        from sqlalchemy.engine.reflection import Inspector
        from sqlalchemy.exc import OperationalError

        project = self._project(tmp_path, monkeypatch)
        assert load_config(project).base_branch == "one"
        _make_db(project / ".claude" / "sova.db", {"base_branch": '"changed-value"'})
        touch_settings_stamp(project.resolve())

        def locked(*args, **kwargs):
            raise OperationalError("PRAGMA table_info", {}, Exception("database is locked"))

        monkeypatch.setattr(Inspector, "has_table", locked)
        assert load_config(project).base_branch == "one"
        assert db_loader.db_read_failed()

    def test_unreachable_database_without_entry_serves_non_db_layers_and_backs_off(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        engines: list[int] = []

        def locked(*args, **kwargs):
            engines.append(1)
            raise RuntimeError("database is locked")

        monkeypatch.setattr("sqlalchemy.create_engine", locked)
        for _ in range(5):
            assert load_config(project).base_branch == "main"
        assert len(engines) == 1  # one failed read per backoff window, not one per call

    def test_failed_read_with_entry_retries_once_per_window(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        assert load_config(project).base_branch == "one"
        _make_db(project / ".claude" / "sova.db", {"base_branch": '"changed-value"'})
        touch_settings_stamp(project.resolve())
        engines: list[int] = []

        def locked(*args, **kwargs):
            engines.append(1)
            raise RuntimeError("database is locked")

        clock = [1000.0]
        monkeypatch.setattr(loader.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr("sqlalchemy.create_engine", locked)
        warnings: list[str] = []
        monkeypatch.setattr(loader.logger, "warning", lambda event, **kw: warnings.append(event))
        for _ in range(4):
            assert load_config(project).base_branch == "one"
        assert len(engines) == 1
        assert len(warnings) == 1
        clock[0] += db_loader.DB_FAILURE_BACKOFF_S + 1
        assert load_config(project).base_branch == "one"
        assert len(engines) == 2

    def test_toml_edit_during_outage_is_not_frozen_out(self, tmp_path, monkeypatch) -> None:
        """A degraded window serves the last good config only while env and TOML are unchanged."""
        project = self._project(tmp_path, monkeypatch)
        assert load_config(project).base_branch == "one"

        def locked(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr("sqlalchemy.create_engine", locked)
        (project / "sova.toml").write_text('[project]\ngithub_repo = "edited/repo"\n', encoding="utf-8")
        cfg = load_config(project)
        assert cfg.github_repo == "edited/repo"
        assert load_config(project).github_repo == "edited/repo"  # and the backoff window still holds

    def test_toml_edit_after_outage_starts_is_not_frozen_out(self, tmp_path, monkeypatch) -> None:
        """An edit made inside an open backoff window ends it (it may be the fix for the outage)."""
        project = self._project(tmp_path, monkeypatch)
        assert load_config(project).base_branch == "one"

        def locked(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr("sqlalchemy.create_engine", locked)
        assert load_config(project).github_repo != "edited/repo"  # degraded entry stored
        (project / "sova.toml").write_text('[project]\ngithub_repo = "edited/repo"\n', encoding="utf-8")
        assert load_config(project).github_repo == "edited/repo"

    def test_second_failed_window_does_not_claim_last_good_config(self, tmp_path, monkeypatch) -> None:
        """A degraded entry built without the DB layer is not a last good config."""
        monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite:///" + str(tmp_path / "db" / "sova.db"))
        invalidate_config_cache()
        _make_db(tmp_path / "db" / "sova.db", {"base_branch": '"one"'})

        def locked(*args, **kwargs):
            raise RuntimeError("database is locked")

        clock = [1000.0]
        monkeypatch.setattr(loader.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr("sqlalchemy.create_engine", locked)
        events: list[str] = []
        monkeypatch.setattr(loader.logger, "warning", lambda event, **kw: events.append(event))
        assert load_config(tmp_path).base_branch == "main"  # no entry: non-DB layers
        clock[0] += db_loader.DB_FAILURE_BACKOFF_S + 1
        assert load_config(tmp_path).base_branch == "main"
        assert events == ["loader.db_unreachable_settings_not_applied"] * 2

    def test_remote_database_with_toml_costs_one_engine_per_miss(self, tmp_path, monkeypatch) -> None:
        """The SQLite-only auto-migration must not open extra connections on PostgreSQL."""
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
        monkeypatch.setenv("SOVA_DATABASE_URL", "postgresql+asyncpg://u:pw@127.0.0.1:1/db")
        monkeypatch.setattr(db_loader, "_warned_once", set())
        invalidate_config_cache()
        (tmp_path / "sova.toml").write_text('[project]\ngithub_repo = "a/b"\n', encoding="utf-8")
        engines: list[int] = []

        def counting(*args, **kwargs):
            engines.append(1)
            raise RuntimeError("unreachable")

        monkeypatch.setattr("sqlalchemy.create_engine", counting)
        assert load_config(tmp_path).github_repo == "a/b"
        assert len(engines) == 1

    def test_backoff_ends_and_database_is_read_again(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        real_create_engine = create_engine
        clock = [1000.0]
        monkeypatch.setattr(loader.time, "monotonic", lambda: clock[0])

        def locked(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr("sqlalchemy.create_engine", locked)
        assert load_config(project).base_branch == "main"
        monkeypatch.setattr("sqlalchemy.create_engine", real_create_engine)
        assert load_config(project).base_branch == "main"  # still inside the window
        clock[0] += db_loader.DB_FAILURE_BACKOFF_S + 1
        assert load_config(project).base_branch == "one"

    def test_backoff_window_grows_with_consecutive_failures_and_is_capped(self) -> None:
        windows = [db_loader.db_failure_backoff(n) for n in range(1, 8)]
        assert windows[0] == db_loader.DB_FAILURE_BACKOFF_S
        assert windows[1] == 2 * windows[0]
        assert windows == sorted(windows)
        assert max(windows) == db_loader.DB_FAILURE_BACKOFF_MAX_S
        assert db_loader.DB_FAILURE_BACKOFF_S > db_loader.PG_CONNECT_TIMEOUT_S

    def test_outage_blocks_for_a_small_fraction_of_the_elapsed_time(self, tmp_path, monkeypatch) -> None:
        """Across a persistent failure, the connect attempts (each one blocks the caller) thin out."""
        project = self._project(tmp_path, monkeypatch)
        clock = [1000.0]
        monkeypatch.setattr(loader.time, "monotonic", lambda: clock[0])
        attempts: list[float] = []

        def unreachable(*args, **kwargs):
            attempts.append(clock[0])
            raise RuntimeError("connection timed out")

        monkeypatch.setattr("sqlalchemy.create_engine", unreachable)
        elapsed = 600
        for _ in range(elapsed):
            load_config(project)
            clock[0] += 1.0
        blocked = len(attempts) * db_loader.PG_CONNECT_TIMEOUT_S
        assert blocked / elapsed < 0.1
        gaps = [later - earlier for earlier, later in zip(attempts, attempts[1:])]
        assert gaps == sorted(gaps)

    def test_backoff_resets_after_a_successful_read(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        real_create_engine = create_engine
        clock = [1000.0]
        monkeypatch.setattr(loader.time, "monotonic", lambda: clock[0])

        def locked(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr("sqlalchemy.create_engine", locked)
        for _ in range(3):
            load_config(project)
            clock[0] += db_loader.DB_FAILURE_BACKOFF_MAX_S + 1
        assert loader._cache[project.resolve()].failures == 3
        monkeypatch.setattr("sqlalchemy.create_engine", real_create_engine)
        assert load_config(project).base_branch == "one"
        assert loader._cache[project.resolve()].failures == 0

    def test_remote_database_entry_expires(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
        monkeypatch.setenv("SOVA_DATABASE_URL", "postgresql+asyncpg://u:pw@127.0.0.1:1/db")
        _, ttl = config_fingerprint(tmp_path.resolve())
        assert ttl == db_loader.PG_CONFIG_CACHE_TTL_S

    def test_load_config_hits_inside_the_ttl_and_rebuilds_after_it(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch).resolve()
        fingerprint = (("remote-style",), db_loader.PG_CONFIG_CACHE_TTL_S)
        monkeypatch.setattr(db_loader, "config_fingerprint", lambda _pd: fingerprint)
        clock = [1000.0]
        monkeypatch.setattr(loader.time, "monotonic", lambda: clock[0])
        builds: list[Path] = []
        real = loader._build_config
        monkeypatch.setattr(loader, "_build_config", lambda p: builds.append(p) or real(p))
        load_config(project)
        clock[0] += db_loader.PG_CONFIG_CACHE_TTL_S - 0.1
        load_config(project)
        assert len(builds) == 1
        clock[0] += 0.2
        load_config(project)
        assert len(builds) == 2

    def test_sqlite_fingerprint_has_a_backstop_ttl(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        _, ttl = config_fingerprint(project.resolve())
        assert ttl == db_loader.SQLITE_CONFIG_CACHE_TTL_S

    def test_lowercase_sova_env_var_changes_fingerprint(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        before, _ = config_fingerprint(project.resolve())
        monkeypatch.setenv("sova_agent_model", "other-model")
        after, _ = config_fingerprint(project.resolve())
        assert before != after

    def test_invalidating_one_project_keeps_another_projects_rebuild(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        a, b = tmp_path / "a", tmp_path / "b"
        _make_db(a / ".claude" / "sova.db", {"base_branch": '"alpha"'})
        _make_db(b / ".claude" / "sova.db", {"base_branch": '"beta"'})
        real = loader._build_config

        def build_then_invalidate_other(p: Path):
            config = real(p)
            if p == b.resolve():
                invalidate_config_cache(a)
            return config

        monkeypatch.setattr(loader, "_build_config", build_then_invalidate_other)
        load_config(b)
        assert b.resolve() in loader._cache

    def test_auto_migration_on_postgres_warns_instead_of_silently_returning(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
        monkeypatch.setenv("SOVA_DATABASE_URL", "postgresql+asyncpg://u:pw@127.0.0.1:1/db")
        monkeypatch.setattr(db_loader, "_warned_once", set())
        errors: list[str] = []
        monkeypatch.setattr(db_loader.logger, "error", lambda message, **kw: errors.append(message))
        db_loader._save_config_to_db_sync(tmp_path, {"github_repo": "a/b"}, only_if_empty=True)
        assert len(errors) == 1
        assert "SQLite" in errors[0]
        assert SECRET not in errors[0]

    def test_concurrent_loads_and_invalidations(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        errors: list[BaseException] = []
        branches: list[str] = []

        def worker() -> None:
            try:
                for _ in range(20):
                    branches.append(load_config(project).base_branch)
                    invalidate_config_cache(project)
            except Exception as exc:  # noqa: BLE001 (collected and re-asserted on the main thread)
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert branches == ["one"] * 80

    def test_config_is_deep_copyable(self, tmp_path, monkeypatch) -> None:
        project = self._project(tmp_path, monkeypatch)
        assert copy.deepcopy(load_config(project)) == load_config(project)


def _engine_builders_in_async_defs(tree: ast.AST) -> list[tuple[int, str]]:
    """(line, callee) for each synchronous ``create_engine`` call directly inside an ``async def``.

    ``create_async_engine`` is not flagged: it is lazy and built once per URL by ``sova.db.session``.
    A nested synchronous ``def`` is skipped: it is the shape handed to ``asyncio.to_thread``.
    """
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, in_async: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                visit(child, True)
            elif isinstance(child, (ast.FunctionDef, ast.Lambda)):
                visit(child, False)
            else:
                if in_async and isinstance(child, ast.Call):
                    func = child.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if name == "create_engine":
                        found.append((child.lineno, name))
                visit(child, in_async)

    visit(tree, False)
    return found


class TestNoEngineBuiltInAsyncDef:
    def test_guard_detects_an_engine_built_in_async_def(self) -> None:
        tree = ast.parse("async def handler():\n    engine = create_engine('sqlite://')\n")
        assert _engine_builders_in_async_defs(tree) == [(2, "create_engine")]

    def test_guard_allows_a_sync_helper_nested_in_async_def(self) -> None:
        source = "async def handler():\n    def build():\n        return sa.create_engine('sqlite://')\n"
        assert _engine_builders_in_async_defs(ast.parse(source)) == []

    def test_no_request_path_builds_an_engine_in_async_def(self) -> None:
        offenders = []
        for path in sorted((REPO_ROOT / "sova").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            offenders += [
                f"{path.relative_to(REPO_ROOT)}:{line} {name}" for line, name in _engine_builders_in_async_defs(tree)
            ]
        assert offenders == [], (
            "An engine built inside an async def blocks the event loop on every call; "
            "read config through load_config() (cached) or build the engine in a sync helper:\n" + "\n".join(offenders)
        )
