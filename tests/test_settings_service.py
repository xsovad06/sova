"""Tests for sova.dashboard.services.settings_service: config validation and casting."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sova.dashboard.services.settings_service import _cast_value, _is_masked_secret, _validate_value_type


class TestIsMaskedSecret:
    def test_bullets_only_is_masked(self) -> None:
        assert _is_masked_secret("•" * 12) is True

    def test_asterisks_only_is_masked(self) -> None:
        assert _is_masked_secret("****") is True

    def test_real_value_not_masked(self) -> None:
        assert _is_masked_secret("sk-ant-abc123") is False

    def test_empty_not_masked(self) -> None:
        assert _is_masked_secret("") is False

    def test_whitespace_only_not_masked(self) -> None:
        assert _is_masked_secret("   ") is False


class TestCastValue:
    def test_bool_true(self) -> None:
        assert _cast_value("true") is True

    def test_bool_false(self) -> None:
        assert _cast_value("false") is False

    def test_int(self) -> None:
        assert _cast_value("42") == 42
        assert isinstance(_cast_value("42"), int)

    def test_float(self) -> None:
        assert _cast_value("3.14") == pytest.approx(3.14)
        assert isinstance(_cast_value("3.14"), float)

    def test_string_passthrough(self) -> None:
        assert _cast_value("hello") == "hello"

    def test_empty_string(self) -> None:
        assert _cast_value("") == ""

    def test_list_from_comma_separated(self) -> None:
        assert _cast_value("sync, assess, develop", "list") == ["sync", "assess", "develop"]

    def test_list_from_json_array(self) -> None:
        assert _cast_value('["sync", "assess"]', "list") == ["sync", "assess"]

    def test_list_empty_is_empty_list(self) -> None:
        """Empty means 'use the default'; a bare string would break load_config()."""
        assert _cast_value("  ", "list") == []

    def test_list_single_value_is_wrapped(self) -> None:
        assert _cast_value("sync", "list") == ["sync"]

    def test_list_malformed_json_falls_back_to_split(self) -> None:
        assert _cast_value("[sync, assess", "list") == ["[sync", "assess"]


class TestValidateValueType:
    def test_number_accepts_int(self) -> None:
        assert _validate_value_type("agent.max_budget", "20") is None

    def test_number_accepts_float(self) -> None:
        assert _validate_value_type("agent.max_budget", "3.14") is None

    def test_number_rejects_text(self) -> None:
        result = _validate_value_type("agent.max_budget", "abc")
        assert result is not None
        assert "number" in result
        assert "abc" in result

    def test_number_rejects_unicode(self) -> None:
        result = _validate_value_type("agent.max_issue_budget", "ČŤ")
        assert result is not None
        assert "number" in result

    def test_boolean_accepts_true_false(self) -> None:
        assert _validate_value_type("review.enabled", "true") is None
        assert _validate_value_type("review.enabled", "false") is None

    def test_boolean_rejects_invalid(self) -> None:
        result = _validate_value_type("review.enabled", "yes")
        assert result is not None
        assert "true or false" in result

    def test_unknown_key_passes(self) -> None:
        assert _validate_value_type("nonexistent.key", "anything") is None

    def test_string_type_accepts_anything(self) -> None:
        assert _validate_value_type("project.github_repo", "any/value") is None

    def test_number_accepts_scientific(self) -> None:
        assert _validate_value_type("agent.max_budget", "1e3") is None

    def test_number_rejects_double_negative(self) -> None:
        result = _validate_value_type("agent.max_budget", "--1")
        assert result is not None
        assert "number" in result


class TestUpdateConfigIntegration:
    async def test_rejects_invalid_number_preserves_file(self, tmp_path, monkeypatch) -> None:
        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[agent]\nmax_budget = 10\n")
        original = toml_file.read_text()

        from sova.dashboard.services.settings_service import update_config

        result = await update_config(tmp_path, key="agent.max_budget", value="abc")
        assert "error" in result
        assert "number" in result["error"]
        assert toml_file.read_text() == original

    async def test_rejects_invalid_boolean_preserves_file(self, tmp_path, monkeypatch) -> None:
        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[review]\nenabled = true\n")
        original = toml_file.read_text()

        from sova.dashboard.services.settings_service import update_config

        result = await update_config(tmp_path, key="review.enabled", value="yes")
        assert "error" in result
        assert "true or false" in result["error"]
        assert toml_file.read_text() == original

    async def test_accepts_valid_number_writes_file(self, tmp_path) -> None:
        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[agent]\nmax_budget = 10\n")

        from sova.dashboard.services.settings_service import update_config

        result = await update_config(tmp_path, key="agent.max_budget", value="25")
        assert result.get("status") == "ok"
        assert "25" in toml_file.read_text()

        from sova.config.db_loader import get_setting
        from sova.db.session import get_session

        async with await get_session(project_dir=tmp_path) as session:
            db_value = await get_setting(session, "agent.max_budget")
        assert db_value == 25

    async def test_db_failure_falls_back_to_toml_only(self, tmp_path) -> None:
        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[agent]\nmax_budget = 10\n")

        from sova.dashboard.services.settings_service import update_config

        with patch("sova.dashboard.services.settings_service._save_setting_to_db", return_value=False):
            result = await update_config(tmp_path, key="agent.max_budget", value="25")
        assert result.get("status") == "ok"
        assert "25" in toml_file.read_text()

    async def test_toml_missing_falls_back_to_db_only(self, tmp_path) -> None:
        from sova.dashboard.services.settings_service import update_config

        result = await update_config(tmp_path, key="agent.max_budget", value="25")
        assert result.get("status") == "ok"

    async def test_both_persistence_fail_returns_error(self, tmp_path) -> None:
        from sova.dashboard.services.settings_service import update_config

        with patch("sova.dashboard.services.settings_service._save_setting_to_db", return_value=False):
            result = await update_config(tmp_path, key="agent.max_budget", value="25")
        assert "error" in result
        assert "Failed to persist" in result["error"]

    async def test_db_exception_returns_false(self, tmp_path) -> None:
        from sova.dashboard.services.settings_service import _save_setting_to_db

        with patch("sova.db.session.get_session", side_effect=RuntimeError("db down")):
            result = await _save_setting_to_db(tmp_path, "agent.max_budget", 25)
        assert result is False

    async def test_rejects_unregistered_key(self, tmp_path) -> None:
        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[agent]\nmax_budget = 10\n")
        original = toml_file.read_text()

        from sova.dashboard.services.settings_service import update_config

        result = await update_config(tmp_path, key="nonexistent.key", value="anything")
        assert "error" in result
        assert "Unknown setting" in result["error"]
        assert toml_file.read_text() == original


class TestCrossFieldValidation:
    """A save must never persist a value that makes load_config() raise.

    llm.provider is the concrete case: the settings dropdown offers
    openai/ollama/vertex, all of which require an explicit llm.model, and the
    per-field type check cannot see that cross-field rule.
    """

    async def test_rejects_provider_requiring_model(self, tmp_path) -> None:
        toml_file = tmp_path / "sova.toml"
        toml_file.write_text('[llm]\nprovider = "claude-code"\n')
        original = toml_file.read_text()

        from sova.dashboard.services.settings_service import update_config

        result = await update_config(tmp_path, key="llm.provider", value="ollama")
        assert "error" in result
        assert "llm.model" in result["error"]
        assert toml_file.read_text() == original

        from sova.config.loader import load_config

        # The project config is still loadable: nothing was persisted.
        assert load_config(tmp_path).llm.provider == "claude-code"

    async def test_accepts_provider_once_model_is_set(self, tmp_path) -> None:
        toml_file = tmp_path / "sova.toml"
        toml_file.write_text('[llm]\nprovider = "claude-code"\nmodel = "ollama/llama3.1"\n')

        from sova.dashboard.services.settings_service import update_config

        result = await update_config(tmp_path, key="llm.provider", value="ollama")
        assert result.get("status") == "ok"

    async def test_unrelated_section_error_does_not_block_save(self, tmp_path) -> None:
        """Only errors touching the edited section may block the save."""
        from sova.dashboard.services.settings_service import _validate_config_consistency

        with patch(
            "sova.config.loader.load_config",
            side_effect=RuntimeError("Invalid configuration"),
        ):
            assert _validate_config_consistency(tmp_path, "agent.max_budget", 25) is None

    async def test_unknown_nested_key_is_ignored(self, tmp_path) -> None:
        from sova.dashboard.services.settings_service import _validate_config_consistency

        assert _validate_config_consistency(tmp_path, "llm.not_a_field", "x") is None
        assert _validate_config_consistency(tmp_path, "not_a_section.field", "x") is None

    async def test_concurrent_updates_never_persist_an_unloadable_combination(self, tmp_path) -> None:
        """Two saves that are each valid against the old state must not both land.

        Without serializing validate-then-persist, setting llm.provider="ollama"
        (valid while llm.model is still set) and llm.model="" (valid while
        llm.provider is still "claude-code") can both validate against the same
        stale snapshot and both persist, leaving provider="ollama" with an empty
        model: unloadable. The lock in update_config() must force one to see the
        other's write and get rejected instead.
        """
        import asyncio

        toml_file = tmp_path / "sova.toml"
        toml_file.write_text('[llm]\nprovider = "claude-code"\nmodel = "ollama/llama3.1"\n')

        from sova.dashboard.services.settings_service import update_config

        results = await asyncio.gather(
            update_config(tmp_path, key="llm.provider", value="ollama"),
            update_config(tmp_path, key="llm.model", value=""),
        )

        from sova.config.loader import load_config

        # Regardless of interleaving, the persisted config must remain loadable.
        load_config(tmp_path)
        # At least one of the two racing updates must have been rejected.
        assert any("error" in r for r in results)


class TestSecretMaskRoundTrip:
    async def test_masked_secret_is_noop(self, tmp_path) -> None:
        from sova.config.db_loader import get_setting
        from sova.dashboard.services.settings_service import update_config
        from sova.db.session import get_session

        # Store a real key first.
        result = await update_config(tmp_path, key="llm.api_key", value="sk-ant-real-key")
        assert result.get("status") == "ok"

        # Submitting the masked placeholder must not overwrite the stored key.
        masked = await update_config(tmp_path, key="llm.api_key", value="•" * 15)
        assert masked.get("status") == "ok"
        assert masked.get("unchanged") is True

        async with await get_session(project_dir=tmp_path) as session:
            db_value = await get_setting(session, "llm.api_key")
        assert db_value == "sk-ant-real-key"

    async def test_real_secret_overwrites(self, tmp_path) -> None:
        from sova.config.db_loader import get_setting
        from sova.dashboard.services.settings_service import update_config
        from sova.db.session import get_session

        await update_config(tmp_path, key="llm.api_key", value="sk-ant-old")
        result = await update_config(tmp_path, key="llm.api_key", value="sk-ant-new")
        assert result.get("status") == "ok"
        assert result.get("unchanged") is not True

        async with await get_session(project_dir=tmp_path) as session:
            db_value = await get_setting(session, "llm.api_key")
        assert db_value == "sk-ant-new"

    async def test_secret_never_written_to_toml(self, tmp_path) -> None:
        from sova.dashboard.services.settings_service import update_config

        toml_file = tmp_path / "sova.toml"
        toml_file.write_text('[llm]\nprovider = "claude-code"\n')

        result = await update_config(tmp_path, key="llm.api_key", value="sk-ant-secret")
        assert result.get("status") == "ok"
        toml_content = toml_file.read_text()
        assert "sk-ant-secret" not in toml_content
        assert "api_key" not in toml_content


class TestSaveSettingToToml:
    """Tests for _save_setting_to_toml error paths."""

    def test_toml_file_missing(self, tmp_path) -> None:
        from sova.dashboard.services.settings_service import _save_setting_to_toml

        result = _save_setting_to_toml(tmp_path, "agent.max_budget", 25)
        assert result is False

    def test_tomlkit_import_error(self, tmp_path) -> None:
        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[agent]\nmax_budget = 10\n")

        from sova.dashboard.services.settings_service import _save_setting_to_toml

        with patch.dict("sys.modules", {"tomlkit": None}):
            result = _save_setting_to_toml(tmp_path, "agent.max_budget", 25)
        assert result is False

    def test_toml_write_exception(self, tmp_path) -> None:
        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[agent]\nmax_budget = 10\n")

        from sova.dashboard.services.settings_service import _save_setting_to_toml

        with (
            patch("sova.dashboard.services.settings_service.get_config_file_path", return_value=tmp_path / "sova.toml"),
            patch("tomlkit.parse", side_effect=RuntimeError("corrupt")),
        ):
            result = _save_setting_to_toml(tmp_path, "agent.max_budget", 25)
        assert result is False


class TestExtractValidationDetail:
    """Tests for _extract_validation_detail helper in the settings router."""

    def test_pydantic_validation_error(self) -> None:
        from pydantic import BaseModel, ValidationError

        from sova.dashboard.routers.settings import _extract_validation_detail

        class Dummy(BaseModel):
            count: int

        try:
            Dummy(count="not_a_number")  # type: ignore[arg-type]
        except ValidationError as ve:
            result = _extract_validation_detail(ve)

        assert "Invalid configuration" in result
        assert "count" in result

    def test_wrapped_validation_error(self) -> None:
        from pydantic import BaseModel, ValidationError

        from sova.dashboard.routers.settings import _extract_validation_detail

        class Dummy(BaseModel):
            count: int

        try:
            Dummy(count="bad")  # type: ignore[arg-type]
        except ValidationError as ve:
            wrapper = RuntimeError("config load failed")
            wrapper.__cause__ = ve
            result = _extract_validation_detail(wrapper)

        assert "Invalid configuration" in result
        assert "count" in result

    def test_non_validation_error_returns_generic(self) -> None:
        from sova.dashboard.routers.settings import _extract_validation_detail

        result = _extract_validation_detail(RuntimeError("something went wrong"))
        assert result == "Failed to fetch configuration"

    def test_generic_exception(self) -> None:
        from sova.dashboard.routers.settings import _extract_validation_detail

        result = _extract_validation_detail(Exception("oops"))
        assert result == "Failed to fetch configuration"


class TestSettingsRouterErrors:
    """Router-level tests for settings API error and validation paths."""

    @pytest.fixture()
    async def _db(self, monkeypatch):
        from sova.db.session import close_db, init_db

        monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
        await init_db(run_migrations=False)
        yield
        await close_db()

    @pytest.fixture()
    async def client(self, _db):
        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.app import create_app

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    async def test_get_config_pydantic_error(self, client, monkeypatch) -> None:
        from pydantic import BaseModel

        class Bad(BaseModel):
            x: int

        def raise_validation(*_a, **_kw):
            Bad(x="nope")  # type: ignore[arg-type]

        monkeypatch.setattr("sova.dashboard.services.settings_service.get_config", raise_validation)
        resp = await client.get("/api/settings/config")
        assert resp.status_code == 500
        assert "Invalid configuration" in resp.json()["detail"]

    async def test_get_config_grouped_pydantic_error(self, client, monkeypatch) -> None:
        from pydantic import BaseModel

        class Bad(BaseModel):
            x: int

        def raise_validation(*_a, **_kw):
            Bad(x="nope")  # type: ignore[arg-type]

        monkeypatch.setattr("sova.dashboard.services.settings_service.get_config", raise_validation)
        resp = await client.get("/api/settings/config/grouped")
        assert resp.status_code == 500
        assert "Invalid configuration" in resp.json()["detail"]

    async def test_get_config_generic_error(self, client, monkeypatch) -> None:
        def raise_generic(*_a, **_kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr("sova.dashboard.services.settings_service.get_config", raise_generic)
        resp = await client.get("/api/settings/config")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "Failed to fetch configuration"

    async def test_update_config_server_error(self, client, monkeypatch) -> None:
        async def raise_generic(*_a, **_kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr("sova.dashboard.services.settings_service.update_config", raise_generic)
        resp = await client.post("/api/settings/config", json={"key": "a.b", "value": "1"})
        assert resp.status_code == 500
        assert resp.json()["detail"] == "Failed to update configuration"

    async def test_update_config_validation_rejection(self, client, monkeypatch) -> None:
        async def reject_validation(*_a, **_kw):
            return {"error": "'x' expects a number, got 'abc'"}

        monkeypatch.setattr(
            "sova.dashboard.services.settings_service.update_config",
            reject_validation,
        )
        resp = await client.post("/api/settings/config", json={"key": "x", "value": "abc"})
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data
        assert "number" in data["error"]


class TestGetConfigAndPersona:
    def test_get_config_returns_error_dict_on_load_failure(self, tmp_path) -> None:
        from sova.dashboard.services.settings_service import get_config

        with patch("sova.config.loader.load_config", side_effect=RuntimeError("bad toml")):
            result = get_config(tmp_path)

        assert result == {"_error": "No configuration found"}

    def test_get_detected_persona_returns_none_on_error(self, tmp_path) -> None:
        from sova.dashboard.services.settings_service import get_detected_persona

        with patch("sova.knowledge.personas.detect_persona", side_effect=OSError("unreadable")):
            assert get_detected_persona(tmp_path) is None


class TestBuildInstallationDiff:
    """Tests for settings_service.build_installation_diff()."""

    @pytest.fixture
    def canonical_dir(self, tmp_path: Path) -> Path:
        cmd_dir = tmp_path / "canonical" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "develop.md").write_text(
            "---\nname: develop\ndescription: Develop.\nuser-invocable: true\ncategory: core\n---\n\nDevelop it.\n"
        )
        (cmd_dir / "standup.md").write_text(
            "---\nname: standup\ndescription: Standup.\nuser-invocable: true\ncategory: management\n---\n\nStand up.\n"
        )
        return cmd_dir

    @pytest.fixture
    def project_dir(self, tmp_path: Path) -> Path:
        project = tmp_path / "project"
        (project / ".claude" / "commands").mkdir(parents=True)
        return project

    @pytest.fixture
    def cfg(self):
        from sova.config.models import ProjectConfig

        return ProjectConfig()

    def _patch_canonical(self, monkeypatch, canonical_dir: Path) -> None:
        monkeypatch.setattr("sova.commands.catalog.get_canonical_dir", lambda: canonical_dir)

    def test_source_unavailable_when_canonical_dir_missing(self, monkeypatch, project_dir, cfg) -> None:
        from sova.dashboard.services.settings_service import build_installation_diff

        monkeypatch.setattr("sova.commands.catalog.get_canonical_dir", lambda: Path("/nonexistent/canonical/dir"))

        result = build_installation_diff(project_dir, cfg)
        assert result.source_available is False
        assert result.files == []

    def test_new_command_not_yet_installed(self, monkeypatch, canonical_dir, project_dir, cfg) -> None:
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)

        result = build_installation_diff(project_dir, cfg)
        names_by_status = {f.filename: f.status for f in result.files}
        assert names_by_status["develop.md"] == "new"
        assert names_by_status["standup.md"] == "new"

        entry = next(f for f in result.files if f.filename == "develop.md")
        assert entry.category == "command"
        assert entry.local_content is None
        assert "Develop it." in entry.canonical_content

    def test_upstream_only_when_canonical_changed_and_local_untouched(
        self, monkeypatch, canonical_dir, project_dir, cfg
    ) -> None:
        from sova.commands.distribution import install_commands
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)
        commands_dir = project_dir / ".claude" / "commands"
        install_commands(canonical_dir, commands_dir, cfg)

        (canonical_dir / "develop.md").write_text(
            "---\nname: develop\ndescription: Develop.\nuser-invocable: true\ncategory: core\n---\n\nNew develop.\n"
        )

        result = build_installation_diff(project_dir, cfg)
        entry = next(f for f in result.files if f.filename == "develop.md")
        assert entry.status == "upstream_only"
        assert "New develop." in entry.canonical_content
        assert "Develop it." in entry.local_content

    def test_conflict_when_both_canonical_and_local_changed(self, monkeypatch, canonical_dir, project_dir, cfg) -> None:
        from sova.commands.distribution import install_commands
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)
        commands_dir = project_dir / ".claude" / "commands"
        install_commands(canonical_dir, commands_dir, cfg)

        (commands_dir / "develop.md").write_text("# Locally customized\n")
        (canonical_dir / "develop.md").write_text(
            "---\nname: develop\ndescription: Develop.\nuser-invocable: true\ncategory: core\n---\n\nNew develop.\n"
        )

        result = build_installation_diff(project_dir, cfg)
        entry = next(f for f in result.files if f.filename == "develop.md")
        assert entry.status == "conflict"
        assert "New develop." in entry.canonical_content
        assert "Locally customized" in entry.local_content

    def test_local_modified_when_only_local_changed(self, monkeypatch, canonical_dir, project_dir, cfg) -> None:
        from sova.commands.distribution import install_commands
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)
        commands_dir = project_dir / ".claude" / "commands"
        install_commands(canonical_dir, commands_dir, cfg)

        (commands_dir / "develop.md").write_text("# Locally customized only\n")

        result = build_installation_diff(project_dir, cfg)
        entry = next(f for f in result.files if f.filename == "develop.md")
        assert entry.status == "local_modified"

    def test_removed_when_canonical_no_longer_has_file(self, monkeypatch, canonical_dir, project_dir, cfg) -> None:
        from sova.commands.distribution import install_commands
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)
        commands_dir = project_dir / ".claude" / "commands"
        install_commands(canonical_dir, commands_dir, cfg)

        (canonical_dir / "standup.md").unlink()

        result = build_installation_diff(project_dir, cfg)
        entry = next(f for f in result.files if f.filename == "standup.md")
        assert entry.status == "removed"
        assert entry.canonical_content is None
        assert "Stand up." in entry.local_content

    def test_local_only_unmanaged_file(self, monkeypatch, canonical_dir, project_dir, cfg) -> None:
        from sova.commands.distribution import install_commands
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)
        commands_dir = project_dir / ".claude" / "commands"
        install_commands(canonical_dir, commands_dir, cfg)

        (commands_dir / "agent-resume.md").write_text("# SOVA-only command\n")

        result = build_installation_diff(project_dir, cfg)
        entry = next(f for f in result.files if f.filename == "agent-resume.md")
        assert entry.status == "local_only"
        assert entry.canonical_content is None
        assert "SOVA-only command" in entry.local_content

    def test_deleted_locally_file_is_local_only(self, monkeypatch, canonical_dir, project_dir, cfg) -> None:
        """A file deleted from disk locally surfaces as "local_only" per the spec's
        seven-value status contract, not a separate "deleted_locally" status."""
        from sova.commands.distribution import install_commands
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)
        commands_dir = project_dir / ".claude" / "commands"
        install_commands(canonical_dir, commands_dir, cfg)

        (commands_dir / "develop.md").unlink()

        result = build_installation_diff(project_dir, cfg)
        entry = next(f for f in result.files if f.filename == "develop.md")
        assert entry.status == "local_only"
        assert entry.canonical_content is None
        assert entry.local_content is None

    def test_canonical_removed_and_locally_modified_is_removed_not_conflict(
        self, monkeypatch, canonical_dir, project_dir, cfg
    ) -> None:
        """A file removed from the canonical source that the user also modified (not
        deleted) locally must surface as "removed", not "conflict": it no longer
        exists in source_files, so a checked "conflict" would silently no-op on
        Apply instead of performing the sync the checkbox implies."""
        from sova.commands.distribution import install_commands
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)
        commands_dir = project_dir / ".claude" / "commands"
        install_commands(canonical_dir, commands_dir, cfg)

        (canonical_dir / "standup.md").unlink()
        (commands_dir / "standup.md").write_text("# Locally customized before removal\n")

        result = build_installation_diff(project_dir, cfg)
        entry = next(f for f in result.files if f.filename == "standup.md")
        assert entry.status == "removed"
        assert entry.canonical_content is None
        assert "Locally customized before removal" in entry.local_content

    def test_new_canonical_file_colliding_with_unmanaged_local_is_a_single_conflict(
        self, monkeypatch, canonical_dir, project_dir, cfg
    ) -> None:
        """A brand-new canonical file sharing a name with an existing unmanaged local file
        must not be reported as a safe "new" install (which would silently overwrite the
        local file) and must not appear twice (once as "new", once as "local_only")."""
        from sova.commands.distribution import install_commands
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)
        commands_dir = project_dir / ".claude" / "commands"
        install_commands(canonical_dir, commands_dir, cfg)

        (commands_dir / "new-thing.md").write_text("# My own local command\n")
        (canonical_dir / "new-thing.md").write_text(
            "---\nname: new-thing\ndescription: New.\nuser-invocable: true\ncategory: core\n---\n\nCanonical.\n"
        )

        result = build_installation_diff(project_dir, cfg)
        matches = [f for f in result.files if f.filename == "new-thing.md"]
        assert len(matches) == 1
        entry = matches[0]
        assert entry.status == "conflict"
        assert "My own local command" in entry.local_content
        assert "Canonical." in entry.canonical_content

    def test_new_canonical_file_colliding_with_local_file_without_manifest_is_conflict(
        self, monkeypatch, canonical_dir, project_dir, cfg
    ) -> None:
        """Without a manifest (no prior install), reverse.unmanaged is always empty
        (_reverse_diff_files returns early), so the unmanaged_local check alone can't
        catch a same-named local file. A direct is_file() check on target_dir must
        still classify it as a conflict, not silently overwrite it as "new"."""
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)
        commands_dir = project_dir / ".claude" / "commands"
        commands_dir.mkdir(parents=True, exist_ok=True)
        (commands_dir / "develop.md").write_text("# Pre-existing local file, never installed\n")

        result = build_installation_diff(project_dir, cfg)
        matches = [f for f in result.files if f.filename == "develop.md"]
        assert len(matches) == 1
        entry = matches[0]
        assert entry.status == "conflict"
        assert "Pre-existing local file" in entry.local_content

    def test_canonical_removed_with_empty_local_file_still_reports_removed(
        self, monkeypatch, canonical_dir, project_dir, cfg
    ) -> None:
        """A canonical file removed upstream must surface as "removed" even when the
        installed file happens to be empty, which would otherwise make the equal-content
        shortcut (both sides "") misclassify it as clean and hide the removal."""
        from sova.commands.distribution import install_commands
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)
        commands_dir = project_dir / ".claude" / "commands"
        install_commands(canonical_dir, commands_dir, cfg)

        (canonical_dir / "standup.md").unlink()
        (commands_dir / "standup.md").write_text("")

        result = build_installation_diff(project_dir, cfg)
        entry = next(f for f in result.files if f.filename == "standup.md")
        assert entry.status == "removed"
        assert entry.canonical_content is None

    def test_removed_upstream_and_deleted_locally_is_a_single_entry(
        self, monkeypatch, canonical_dir, project_dir, cfg
    ) -> None:
        """A file removed from the canonical source that the user also deleted locally
        must not appear twice (once as "removed", once as "local_only")."""
        from sova.commands.distribution import install_commands
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)
        commands_dir = project_dir / ".claude" / "commands"
        install_commands(canonical_dir, commands_dir, cfg)

        (canonical_dir / "standup.md").unlink()
        (commands_dir / "standup.md").unlink()

        result = build_installation_diff(project_dir, cfg)
        matches = [f for f in result.files if f.filename == "standup.md"]
        assert len(matches) == 1
        assert matches[0].status == "removed"

    def test_upstream_changed_and_deleted_locally_is_a_single_entry(
        self, monkeypatch, canonical_dir, project_dir, cfg
    ) -> None:
        """A tracked file the upstream changed that the user also deleted locally
        must not appear twice (once as "upstream_only", once as "local_only")."""
        from sova.commands.distribution import install_commands
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)
        commands_dir = project_dir / ".claude" / "commands"
        install_commands(canonical_dir, commands_dir, cfg)

        (canonical_dir / "develop.md").write_text(
            "---\nname: develop\ndescription: Develop.\nuser-invocable: true\ncategory: core\n---\n\nNew develop.\n"
        )
        (commands_dir / "develop.md").unlink()

        result = build_installation_diff(project_dir, cfg)
        matches = [f for f in result.files if f.filename == "develop.md"]
        assert len(matches) == 1
        assert matches[0].status == "upstream_only"
        assert matches[0].local_content is None

    def test_render_equal_content_is_omitted_as_clean(self, monkeypatch, canonical_dir, project_dir, cfg) -> None:
        """A hash-drifted file whose rendered canonical content matches local exactly is clean, not a conflict."""
        from sova.commands.distribution import install_commands
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)
        commands_dir = project_dir / ".claude" / "commands"
        install_commands(canonical_dir, commands_dir, cfg)

        rendered = (canonical_dir / "develop.md").read_text()
        # Local disk content now differs from the manifest hash (drift is detected),
        # but it is byte-identical to what a sync would write.
        (commands_dir / "develop.md").write_text(rendered)
        # Force a manifest/hash mismatch without changing the rendered content by
        # tampering with the manifest hash directly.
        import json

        manifest_path = commands_dir / ".sova-manifest.json"
        data = json.loads(manifest_path.read_text())
        data["commands"]["develop.md"]["hash"] = "deadbeefdeadbeef"
        manifest_path.write_text(json.dumps(data))

        result = build_installation_diff(project_dir, cfg)
        assert all(f.filename != "develop.md" for f in result.files)

    def test_guidelines_excluded_when_never_installed(self, monkeypatch, canonical_dir, project_dir, cfg) -> None:
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)

        result = build_installation_diff(project_dir, cfg)
        assert all(f.category != "guideline" for f in result.files)

    def test_guidelines_included_when_previously_installed(self, monkeypatch, canonical_dir, project_dir, cfg) -> None:
        from sova.commands.distribution import install_guidelines
        from sova.dashboard.services.settings_service import build_installation_diff

        self._patch_canonical(monkeypatch, canonical_dir)

        guidelines_dir = project_dir.parent / "guidelines_src"
        guidelines_dir.mkdir()
        (guidelines_dir / "security.md").write_text("# Security\n")
        monkeypatch.setattr("sova.commands.catalog.get_guidelines_dir", lambda: guidelines_dir)

        rules_dir = project_dir / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        install_guidelines(guidelines_dir, rules_dir, cfg)

        (guidelines_dir / "security.md").write_text("# Updated security\n")

        result = build_installation_diff(project_dir, cfg)
        entry = next(f for f in result.files if f.filename == "security.md")
        assert entry.category == "guideline"
        assert entry.status == "upstream_only"


class TestReadTextOrNone:
    """Tests for the shared sova.utils.files.read_text_or_none() helper.

    settings_service.py, sova/commands/distribution.py's _diff_files(),
    _reverse_diff_files(), and _update_files() all use this one implementation
    instead of hand-rolling the same try/except (OSError, UnicodeDecodeError)
    read pattern independently.
    """

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        from sova.utils.files import read_text_or_none

        assert read_text_or_none(tmp_path / "does-not-exist.md") is None

    def test_valid_utf8_file_returns_content(self, tmp_path: Path) -> None:
        from sova.utils.files import read_text_or_none

        path = tmp_path / "file.md"
        path.write_text("hello\n", encoding="utf-8")
        assert read_text_or_none(path) == "hello\n"

    def test_non_utf8_file_returns_none_instead_of_raising(self, tmp_path: Path) -> None:
        from sova.utils.files import read_text_or_none

        path = tmp_path / "binary.md"
        path.write_bytes(b"\xff\xfe\x00\x01invalid-utf8")
        assert read_text_or_none(path) is None
