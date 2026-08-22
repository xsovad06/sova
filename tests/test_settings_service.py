"""Tests for sova.dashboard.services.settings_service: config validation and casting."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sova.dashboard.services.settings_service import _cast_value, _validate_value_type


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

        with patch("sova.db.session.get_session", side_effect=Exception("db down")):
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
