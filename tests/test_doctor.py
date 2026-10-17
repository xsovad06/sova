"""Tests for sova.cli.commands.doctor: keyring secret and database schema diagnostics."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from sova.cli.commands.doctor import _check_keyring_secrets


def test_no_checks_when_nothing_stored(tmp_path: Path) -> None:
    from sova.dashboard.services import settings_service

    with patch.object(settings_service, "_get_raw_config", return_value={}):
        checks = _check_keyring_secrets(tmp_path)

    assert checks == []


def test_no_checks_when_sentinel_resolves(tmp_path: Path) -> None:
    from sova.dashboard.services import settings_service
    from sova.llm import keyring_store

    with (
        patch.object(settings_service, "_get_raw_config", return_value={"llm.api_key": keyring_store.SENTINEL}),
        patch.object(keyring_store, "is_keyring_available", return_value=True),
        patch.object(keyring_store, "get_secret", return_value="sk-ant-real"),
    ):
        checks = _check_keyring_secrets(tmp_path)

    assert checks == []


def test_reports_sentinel_without_matching_entry(tmp_path: Path) -> None:
    from sova.dashboard.services import settings_service
    from sova.llm import keyring_store

    with (
        patch.object(settings_service, "_get_raw_config", return_value={"llm.api_key": keyring_store.SENTINEL}),
        patch.object(keyring_store, "is_keyring_available", return_value=False),
    ):
        checks = _check_keyring_secrets(tmp_path)

    assert len(checks) == 1
    name, passed, detail, required = checks[0]
    assert name == "keyring: llm.api_key"
    assert passed is False
    assert "sentinel" in detail
    assert required is False


def test_reports_sentinel_when_keyring_available_but_entry_missing(tmp_path: Path) -> None:
    from sova.dashboard.services import settings_service
    from sova.llm import keyring_store

    with (
        patch.object(settings_service, "_get_raw_config", return_value={"llm.api_key": keyring_store.SENTINEL}),
        patch.object(keyring_store, "is_keyring_available", return_value=True),
        patch.object(keyring_store, "get_secret", return_value=None),
    ):
        checks = _check_keyring_secrets(tmp_path)

    assert len(checks) == 1
    assert checks[0][0] == "keyring: llm.api_key"


def test_no_checks_when_scoped_entry_resolves_but_bare_entry_does_not(tmp_path: Path) -> None:
    """Secrets are written to the per-(project, provider) scoped name, not
    the bare key (see scoped_secret_name()). A lookup using only the bare
    key would never find a secret saved through the normal scoped path and
    would wrongly report a mismatch on every healthy installation
    (CodeRabbit finding, doctor.py:290-294)."""
    from sova.dashboard.services import settings_service
    from sova.llm import keyring_store

    scoped_name = keyring_store.scoped_secret_name("llm.api_key", tmp_path, "anthropic")

    def fake_get_secret(name: str) -> str | None:
        return "sk-ant-real" if name == scoped_name else None

    with (
        patch.object(settings_service, "_get_raw_config", return_value={"llm.api_key": keyring_store.SENTINEL}),
        patch("sova.config.loader.load_config") as mock_load_config,
        patch.object(keyring_store, "is_keyring_available", return_value=True),
        patch.object(keyring_store, "get_secret", side_effect=fake_get_secret),
    ):
        mock_load_config.return_value.llm.provider = "anthropic"
        checks = _check_keyring_secrets(tmp_path)

    assert checks == []


def test_plaintext_value_not_reported(tmp_path: Path) -> None:
    """A plaintext-stored secret (not the sentinel) is normal and not flagged."""
    from sova.dashboard.services import settings_service

    with patch.object(settings_service, "_get_raw_config", return_value={"llm.api_key": "sk-ant-plaintext"}):
        checks = _check_keyring_secrets(tmp_path)

    assert checks == []


def test_failure_reported_as_failed_check(tmp_path: Path) -> None:
    from sova.dashboard.services import settings_service

    with patch.object(settings_service, "_get_raw_config", side_effect=RuntimeError("boom")):
        checks = _check_keyring_secrets(tmp_path)

    assert len(checks) == 1
    assert checks[0][0] == "keyring secrets"
    assert checks[0][1] is False


class TestCheckDatabaseSchema:
    """The "database schema" check compares alembic_version with the migration head."""

    @staticmethod
    def _inspection(**overrides):
        from sova.db.session import SchemaInspection

        fields = {"state": "ready", "legacy": False, "current": "050", "head": "050", "missing": ()}
        fields.update(overrides)
        return SchemaInspection(**fields)

    @staticmethod
    async def _run(inspection=None, *, error: Exception | None = None):
        from sova.cli.commands.doctor import _check_database_schema

        patcher = patch(
            "sova.db.session.inspect_schema",
            new_callable=AsyncMock,
            return_value=inspection,
            side_effect=error,
        )
        with patcher:
            return await _check_database_schema(Path("/unused"))

    async def test_no_row_when_the_project_has_no_database_file(self) -> None:
        """The existing "database" check already reports a missing file."""
        assert await self._run(self._inspection(state="no_file")) == []

    async def test_warns_when_the_database_has_no_tables(self) -> None:
        checks = await self._run(self._inspection(state="no_tables"))

        assert len(checks) == 1
        name, passed, detail, required = checks[0]
        assert (name, passed, required) == ("database schema", False, False)
        assert "sova init-db" in detail

    async def test_passes_when_at_head_with_no_drift(self) -> None:
        checks = await self._run(self._inspection())

        assert checks == [("database schema", True, "at head (050)", False)]

    async def test_warns_when_behind_head(self) -> None:
        checks = await self._run(self._inspection(current="048", head="050"))

        assert len(checks) == 1
        _, passed, detail, required = checks[0]
        assert passed is False
        assert required is False
        assert "at 048, head is 050" in detail
        assert "sova init-db" in detail

    async def test_warns_when_unversioned(self) -> None:
        checks = await self._run(self._inspection(legacy=True, current=None))

        assert checks[0][1] is False
        assert "unversioned" in checks[0][2]

    async def test_warns_when_the_head_cannot_be_determined(self) -> None:
        """An unknown head must not read as a pass, or as "behind a head that is None"."""
        checks = await self._run(self._inspection(head=None))

        assert checks[0][1] is False
        assert "head unknown" in checks[0][2]

    async def test_reports_missing_columns_even_when_at_head(self) -> None:
        checks = await self._run(self._inspection(missing=("task_runs.a", "task_runs.b")))

        assert checks[0][1] is False
        assert "2 ORM column(s) missing" in checks[0][2]
        assert "task_runs.a" in checks[0][2]

    async def test_inspection_failure_is_a_failed_row_not_an_exception(self) -> None:
        checks = await self._run(error=RuntimeError("database is locked"))

        assert len(checks) == 1
        name, passed, detail, required = checks[0]
        assert (name, passed, required) == ("database schema", False, False)
        assert "could not inspect" in detail
        assert "database is locked" in detail

    async def test_doctor_runs_the_schema_check(self, tmp_path: Path) -> None:
        """_doctor() must include the row, or the check exists but never reaches a user."""
        from contextlib import ExitStack
        from unittest.mock import MagicMock

        from sova.cli.commands import doctor as mod

        rendered: list = []
        with ExitStack() as stack:
            for name in (
                "_check_git",
                "_check_gh_cli",
                "_check_claude_cli",
                "_check_git_identity",
                "_check_llm_provider",
                "_check_ollama",
                "_check_agent_runtime",
                "_check_awareness_providers",
            ):
                stack.enter_context(patch.object(mod, name, new_callable=AsyncMock, return_value=[]))
            for name in (
                "_check_terminal_notifier",
                "_check_rtk",
                "_check_sova_config",
                "_check_install_completeness",
                "_check_keyring_secrets",
            ):
                stack.enter_context(patch.object(mod, name, new_callable=MagicMock, return_value=[]))
            stack.enter_context(
                patch.object(
                    mod, "_check_git_hooks", new_callable=AsyncMock, return_value=("git hooks", True, "", False)
                )
            )
            stack.enter_context(
                patch.object(
                    mod,
                    "_check_database_schema",
                    new_callable=AsyncMock,
                    return_value=[("database schema", True, "at head (050)", False)],
                )
            )
            stack.enter_context(patch.object(mod, "_render_results", side_effect=rendered.append))

            await mod._doctor(tmp_path)

        assert ("database schema", True, "at head (050)", False) in rendered[0]
