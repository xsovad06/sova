"""Tests for sova migrate-config CLI command and auto-migration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text

from sova.db.models import Base


def _create_db(tmp_path: Path) -> Path:
    """Create a SQLite DB with the project_settings table."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(exist_ok=True)
    db_path = claude_dir / "sova.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return db_path


def _write_toml(tmp_path: Path, content: str) -> Path:
    """Write a sova.toml file."""
    toml_path = tmp_path / "sova.toml"
    toml_path.write_text(content)
    return toml_path


def _read_db_settings(db_path: Path) -> dict[str, Any]:
    """Read all ProjectSetting rows from the DB as a flat dict."""
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT key, value FROM project_settings ORDER BY key")).fetchall()
    engine.dispose()
    return {row[0]: json.loads(row[1]) for row in rows}


def _seed_db_settings(db_path: Path, settings: dict[str, Any]) -> None:
    """Write settings directly into the DB."""
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        for key, value in settings.items():
            conn.execute(
                text("INSERT INTO project_settings (key, value) VALUES (:key, :value)"),
                {"key": key, "value": json.dumps(value)},
            )
    engine.dispose()


class TestMigrateConfigCLI:
    def test_basic_migration(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        db_path = _create_db(tmp_path)
        _write_toml(tmp_path, '[project]\ngithub_repo = "user/repo"\ngithub_user = "testuser"\n')

        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["migrate-config", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "Migrated" in result.output

        settings = _read_db_settings(db_path)
        assert settings["github_repo"] == "user/repo"
        assert settings["github_user"] == "testuser"

    def test_nested_sections(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        _create_db(tmp_path)
        _write_toml(
            tmp_path,
            '[project]\ngithub_repo = "user/repo"\n\n'
            "[pipeline]\nauto_handoff = true\n\n"
            "[supervisor]\nenable = true\npoll_interval_seconds = 120\n",
        )

        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["migrate-config", str(tmp_path)])

        assert result.exit_code == 0, result.output
        db_path = tmp_path / ".claude" / "sova.db"
        settings = _read_db_settings(db_path)
        assert settings["pipeline.auto_handoff"] is True
        assert settings["supervisor.enable"] is True
        assert settings["supervisor.poll_interval_seconds"] == 120

    def test_partial_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        db_path = _create_db(tmp_path)
        _write_toml(tmp_path, 'github_repo = "user/repo"\n')

        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["migrate-config", str(tmp_path)])

        assert result.exit_code == 0, result.output
        settings = _read_db_settings(db_path)
        assert settings["github_repo"] == "user/repo"
        assert len(settings) == 1

    def test_empty_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        _create_db(tmp_path)
        _write_toml(tmp_path, "# empty config\n")

        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["migrate-config", str(tmp_path)])

        assert result.exit_code == 0
        assert "nothing to migrate" in result.output.lower()

    def test_dry_run_no_writes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        db_path = _create_db(tmp_path)
        _write_toml(tmp_path, '[project]\ngithub_repo = "user/repo"\n')

        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["migrate-config", str(tmp_path), "--dry-run"])

        assert result.exit_code == 0
        assert "Would migrate" in result.output
        assert "github_repo" in result.output

        settings = _read_db_settings(db_path)
        assert len(settings) == 0

    def test_remove_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        _create_db(tmp_path)
        toml_path = _write_toml(tmp_path, '[project]\ngithub_repo = "user/repo"\n')

        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["migrate-config", str(tmp_path), "--remove-toml"])

        assert result.exit_code == 0, result.output
        assert "Removed sova.toml" in result.output
        assert not toml_path.exists()

    def test_remove_toml_on_write_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        _create_db(tmp_path)
        toml_path = _write_toml(tmp_path, '[project]\ngithub_repo = "user/repo"\n')

        from typer.testing import CliRunner

        from sova.cli.app import app

        with patch("sova.config.db_loader._save_config_to_db_sync", side_effect=RuntimeError("boom")):
            runner = CliRunner()
            result = runner.invoke(app, ["migrate-config", str(tmp_path), "--remove-toml"])

        assert result.exit_code == 1
        assert toml_path.exists()

    def test_existing_db_without_force(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        db_path = _create_db(tmp_path)
        _seed_db_settings(db_path, {"github_repo": "existing/repo"})
        _write_toml(tmp_path, '[project]\ngithub_repo = "new/repo"\n')

        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["migrate-config", str(tmp_path)])

        assert result.exit_code == 1
        assert "--force" in result.output

        settings = _read_db_settings(db_path)
        assert settings["github_repo"] == "existing/repo"

    def test_existing_db_with_force(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        db_path = _create_db(tmp_path)
        _seed_db_settings(db_path, {"github_repo": "existing/repo"})
        _write_toml(tmp_path, '[project]\ngithub_repo = "new/repo"\n')

        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["migrate-config", str(tmp_path), "--force"])

        assert result.exit_code == 0, result.output

        settings = _read_db_settings(db_path)
        assert settings["github_repo"] == "new/repo"

    def test_missing_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        _create_db(tmp_path)

        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["migrate-config", str(tmp_path)])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_missing_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        _write_toml(tmp_path, '[project]\ngithub_repo = "user/repo"\n')

        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["migrate-config", str(tmp_path)])

        assert result.exit_code == 1
        assert "init-db" in result.output.lower()

    def test_force_preserves_absent_keys(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--force merges: existing DB keys absent from TOML are preserved."""
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        db_path = _create_db(tmp_path)
        _seed_db_settings(
            db_path,
            {
                "pipeline.auto_handoff": True,
                "pipeline.max_retries": 3,
                "supervisor.enable": True,
            },
        )
        _write_toml(tmp_path, "[pipeline]\nauto_handoff = false\n")

        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["migrate-config", str(tmp_path), "--force"])

        assert result.exit_code == 0, result.output

        settings = _read_db_settings(db_path)
        assert settings["pipeline.auto_handoff"] is False
        assert settings["pipeline.max_retries"] == 3
        assert settings["supervisor.enable"] is True

    def test_verification_failure_blocks_toml_removal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verification failure prevents TOML deletion and exits with error."""
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        _create_db(tmp_path)
        toml_path = _write_toml(tmp_path, '[project]\ngithub_repo = "user/repo"\n')

        from typer.testing import CliRunner

        from sova.cli.app import app

        with patch("sova.config.db_loader._save_config_to_db_sync"):
            runner = CliRunner()
            result = runner.invoke(app, ["migrate-config", str(tmp_path), "--remove-toml"])

        assert result.exit_code == 1
        assert "verification failed" in result.output.lower()
        assert toml_path.exists()

    def test_deprecated_key_migration(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        db_path = _create_db(tmp_path)
        _write_toml(tmp_path, "[commit]\nno_ai_coauthor = true\n")

        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["migrate-config", str(tmp_path)])

        assert result.exit_code == 0, result.output

        settings = _read_db_settings(db_path)
        assert settings.get("commit.ai_coauthor") is False
        assert "commit.no_ai_coauthor" not in settings


class TestAutoMigration:
    def test_triggers_on_first_load(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        db_path = _create_db(tmp_path)
        _write_toml(
            tmp_path,
            '[project]\ngithub_repo = "user/repo"\n\n[pipeline]\nauto_handoff = true\n',
        )

        from sova.config.loader import load_config

        cfg = load_config(tmp_path)

        assert cfg.github_repo == "user/repo"
        assert cfg.pipeline.auto_handoff is True

        settings = _read_db_settings(db_path)
        assert settings["github_repo"] == "user/repo"
        assert settings["pipeline.auto_handoff"] is True

    def test_idempotent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        _create_db(tmp_path)
        _write_toml(tmp_path, '[project]\ngithub_repo = "user/repo"\n')

        from sova.config.loader import load_config

        cfg1 = load_config(tmp_path)
        cfg2 = load_config(tmp_path)

        assert cfg1.github_repo == cfg2.github_repo == "user/repo"

    def test_skips_when_db_populated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        db_path = _create_db(tmp_path)
        _seed_db_settings(db_path, {"github_repo": "db/repo"})
        _write_toml(tmp_path, '[project]\ngithub_repo = "toml/repo"\n')

        from sova.config.loader import load_config

        cfg = load_config(tmp_path)

        assert cfg.github_repo == "db/repo"

    def test_skips_when_no_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        _create_db(tmp_path)

        from sova.config import db_loader

        monkeypatch.setattr(db_loader, "_try_load_from_db", lambda _pd: None)

        from sova.config.loader import load_config

        cfg = load_config(tmp_path)
        assert cfg.github_repo == ""

    def test_non_fatal_on_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        _create_db(tmp_path)
        _write_toml(tmp_path, '[project]\ngithub_repo = "user/repo"\n')

        from sova.config import db_loader

        monkeypatch.setattr(db_loader, "_try_load_from_db", lambda _pd: None)

        with patch(
            "sova.config.db_loader._save_config_to_db_sync",
            side_effect=RuntimeError("boom"),
        ) as mock_save:
            from sova.config.loader import load_config

            cfg = load_config(tmp_path)

        mock_save.assert_called_once()
        assert cfg.github_repo == "user/repo"

    def test_no_migration_on_db_read_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A DB read failure must not trigger auto-migration (fail-closed)."""
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        db_path = _create_db(tmp_path)
        _seed_db_settings(db_path, {"github_repo": "db/repo"})
        _write_toml(tmp_path, '[project]\ngithub_repo = "toml/repo"\n')

        from sova.config import db_loader

        def _failing_load(_pd: Path | str | None) -> None:
            return None

        monkeypatch.setattr(db_loader, "_try_load_from_db", _failing_load)
        monkeypatch.setattr(db_loader, "_is_db_confirmed_empty", lambda _pd: False)

        with patch("sova.config.db_loader._save_config_to_db_sync") as mock_save:
            from sova.config.loader import load_config

            cfg = load_config(tmp_path)

        mock_save.assert_not_called()
        assert cfg.github_repo == "toml/repo"

    def test_skips_when_db_file_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        _write_toml(tmp_path, '[project]\ngithub_repo = "user/repo"\n')

        from sova.config import db_loader

        monkeypatch.setattr(db_loader, "_try_load_from_db", lambda _pd: None)

        from sova.config.loader import load_config

        cfg = load_config(tmp_path)
        assert cfg.github_repo == "user/repo"
