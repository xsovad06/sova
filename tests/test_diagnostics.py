"""Tests for sova.utils.diagnostics: shared checks for `sova doctor` and the dashboard health panel."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.config.models import CodexConfig
from sova.dashboard.services import settings_service
from sova.db.session import SchemaInspection
from sova.llm import keyring_store
from sova.utils.diagnostics import (
    DiagnosticCheck,
    _check_agent_runtime,
    _check_awareness_providers,
    _check_claude_cli,
    _check_database_schema,
    _check_gh_cli,
    _check_git,
    _check_git_hooks,
    _check_git_identity,
    _check_github_config,
    _check_install_completeness,
    _check_jira_config,
    _check_keyring_secrets,
    _check_llm_provider,
    _check_ollama,
    _check_python_version,
    _check_rtk,
    _check_sova_config,
    _check_terminal_notifier,
    _extract_auth_detail,
    _validate_sova_config,
)
from sova.utils.shell import GitIdentityResult, ShellResult

# Every test in this module runs against the shared per-worker in-memory DB
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


def _scaffold_install_artifacts(tmp_path: Path) -> None:
    """Create the minimum directory structure that _check_install_completeness expects."""
    (tmp_path / "sova.toml").write_text("[task_source]\ntype = 'github'\n")
    commands_dir = tmp_path / ".claude" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    (commands_dir / "dummy.md").write_text("---\nname: dummy\n---\n")
    memory_dir = tmp_path / ".claude" / "agent-memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.write_text(json.dumps({"permissions": {"allow": ["Bash(*)", "Read(*)", "Edit(*)", "Write(*)"]}}))


# ---------------------------------------------------------------------------
# Keyring secrets
# ---------------------------------------------------------------------------


def test_no_checks_when_nothing_stored(tmp_path: Path) -> None:
    with patch.object(settings_service, "_get_raw_config", return_value={}):
        checks = _check_keyring_secrets(tmp_path)

    assert checks == []


def test_no_checks_when_sentinel_resolves(tmp_path: Path) -> None:
    with (
        patch.object(settings_service, "_get_raw_config", return_value={"llm.api_key": keyring_store.SENTINEL}),
        patch.object(keyring_store, "is_keyring_available", return_value=True),
        patch.object(keyring_store, "get_secret", return_value="sk-ant-real"),
    ):
        checks = _check_keyring_secrets(tmp_path)

    assert checks == []


def test_reports_sentinel_without_matching_entry(tmp_path: Path) -> None:
    with (
        patch.object(settings_service, "_get_raw_config", return_value={"llm.api_key": keyring_store.SENTINEL}),
        patch.object(keyring_store, "is_keyring_available", return_value=False),
    ):
        checks = _check_keyring_secrets(tmp_path)

    assert len(checks) == 1
    check = checks[0]
    assert check.name == "keyring: llm.api_key"
    assert check.passed is False
    assert "sentinel" in check.detail
    assert check.required is False


def test_reports_sentinel_when_keyring_available_but_entry_missing(tmp_path: Path) -> None:
    with (
        patch.object(settings_service, "_get_raw_config", return_value={"llm.api_key": keyring_store.SENTINEL}),
        patch.object(keyring_store, "is_keyring_available", return_value=True),
        patch.object(keyring_store, "get_secret", return_value=None),
    ):
        checks = _check_keyring_secrets(tmp_path)

    assert len(checks) == 1
    assert checks[0].name == "keyring: llm.api_key"


def test_no_checks_when_scoped_entry_resolves_but_bare_entry_does_not(tmp_path: Path) -> None:
    """Secrets are written to the per-(project, provider) scoped name, not
    the bare key (see scoped_secret_name()). A lookup using only the bare
    key would never find a secret saved through the normal scoped path and
    would wrongly report a mismatch on every healthy installation
    (CodeRabbit finding, doctor.py:290-294)."""
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
    with patch.object(settings_service, "_get_raw_config", return_value={"llm.api_key": "sk-ant-plaintext"}):
        checks = _check_keyring_secrets(tmp_path)

    assert checks == []


def test_keyring_failure_reported_as_failed_check(tmp_path: Path) -> None:
    with patch.object(settings_service, "_get_raw_config", side_effect=RuntimeError("boom")):
        checks = _check_keyring_secrets(tmp_path)

    assert len(checks) == 1
    assert checks[0].name == "keyring secrets"
    assert checks[0].passed is False


# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------


class TestCheckDatabaseSchema:
    """The "database schema" check compares alembic_version with the migration head."""

    @staticmethod
    def _inspection(**overrides):
        fields = {"state": "ready", "legacy": False, "current": "050", "head": "050", "missing": ()}
        fields.update(overrides)
        return SchemaInspection(**fields)

    @staticmethod
    async def _run(inspection=None, *, error: Exception | None = None):
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
        check = checks[0]
        assert (check.name, check.passed, check.required) == ("database schema", False, False)
        assert "sova init-db" in check.detail

    async def test_passes_when_at_head_with_no_drift(self) -> None:
        checks = await self._run(self._inspection())

        assert checks == [DiagnosticCheck("database schema", True, "at head (050)", False)]

    async def test_warns_when_behind_head(self) -> None:
        checks = await self._run(self._inspection(current="048", head="050"))

        assert len(checks) == 1
        check = checks[0]
        assert check.passed is False
        assert check.required is False
        assert "at 048, head is 050" in check.detail
        assert "sova init-db" in check.detail

    async def test_warns_when_unversioned(self) -> None:
        checks = await self._run(self._inspection(legacy=True, current=None))

        assert checks[0].passed is False
        assert "unversioned" in checks[0].detail

    async def test_warns_when_the_head_cannot_be_determined(self) -> None:
        """An unknown head must not read as a pass, or as "behind a head that is None"."""
        checks = await self._run(self._inspection(head=None))

        assert checks[0].passed is False
        assert "head unknown" in checks[0].detail

    async def test_reports_missing_columns_even_when_at_head(self) -> None:
        checks = await self._run(self._inspection(missing=("task_runs.a", "task_runs.b")))

        assert checks[0].passed is False
        assert "2 ORM column(s) missing" in checks[0].detail
        assert "task_runs.a" in checks[0].detail

    async def test_inspection_failure_is_a_failed_row_not_an_exception(self) -> None:
        checks = await self._run(error=RuntimeError("database is locked"))

        assert len(checks) == 1
        check = checks[0]
        assert (check.name, check.passed, check.required) == ("database schema", False, False)
        assert "could not inspect" in check.detail
        assert "database is locked" in check.detail


# ---------------------------------------------------------------------------
# Install completeness
# ---------------------------------------------------------------------------


class TestInstallCompletenessChecks:
    def test_check_install_completeness_all_present(self, tmp_path: Path) -> None:
        _scaffold_install_artifacts(tmp_path)
        (tmp_path / ".claude" / "sova.db").write_text("")
        checks = _check_install_completeness(tmp_path)
        assert all(check.passed for check in checks)

    def test_check_install_completeness_missing_commands(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "sova.db").write_text("")
        checks = _check_install_completeness(tmp_path)
        cmd_check = next(c for c in checks if c.name == "commands installed")
        assert cmd_check.passed is False

    def test_check_install_completeness_missing_db(self, tmp_path: Path) -> None:
        _scaffold_install_artifacts(tmp_path)
        checks = _check_install_completeness(tmp_path)
        db_check = next(c for c in checks if c.name == "database")
        assert db_check.passed is False

    def test_check_install_completeness_includes_permissions(self, tmp_path: Path) -> None:
        """Doctor check reports agent permissions status."""
        _scaffold_install_artifacts(tmp_path)
        (tmp_path / ".claude" / "sova.db").write_text("")
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.write_text(json.dumps({"permissions": {"allow": ["Bash(*)", "Read(*)", "Edit(*)", "Write(*)"]}}))

        checks = _check_install_completeness(tmp_path)
        perm_check = next(c for c in checks if c.name == "agent permissions")
        assert perm_check.passed is True
        assert perm_check.detail == "configured"

    def test_check_install_completeness_warns_missing_permissions(self, tmp_path: Path) -> None:
        """Doctor check warns when agent permissions are missing."""
        _scaffold_install_artifacts(tmp_path)
        (tmp_path / ".claude" / "sova.db").write_text("")
        # Remove the permissions that _scaffold_install_artifacts creates
        (tmp_path / ".claude" / "settings.json").unlink()

        checks = _check_install_completeness(tmp_path)
        perm_check = next(c for c in checks if c.name == "agent permissions")
        assert perm_check.passed is False
        assert "missing" in perm_check.detail.lower()
        assert "sova install --update" in perm_check.detail


# ---------------------------------------------------------------------------
# Remaining check helpers
# ---------------------------------------------------------------------------


class TestDiagnosticHelpers:
    """Tests for the individual check functions in sova.utils.diagnostics."""

    def test_check_python_version(self) -> None:
        check = _check_python_version()
        assert check.name == "Python >= 3.12"
        assert isinstance(check.passed, bool)
        assert "." in check.detail
        assert check.required is True

    def test_extract_auth_detail_authenticated(self) -> None:
        result = ShellResult(returncode=0, stdout="Logged in to github.com as testuser\n", stderr="")
        detail = _extract_auth_detail(result, auth_ok=True)
        assert "Logged in" in detail

    def test_extract_auth_detail_not_authenticated(self) -> None:
        result = ShellResult(returncode=1, stdout="", stderr="")
        detail = _extract_auth_detail(result, auth_ok=False)
        assert "not authenticated" in detail

    def test_check_terminal_notifier_non_darwin(self) -> None:
        with patch("sova.utils.diagnostics.platform") as mock_platform:
            mock_platform.system.return_value = "Linux"
            checks = _check_terminal_notifier()
            assert checks == []

    async def test_check_git_found(self) -> None:
        with patch("sova.utils.diagnostics.shutil.which", return_value="/usr/bin/git"):
            with patch("sova.utils.diagnostics.run", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = ShellResult(returncode=0, stdout="git version 2.43.0\n", stderr="")
                checks = await _check_git()
                assert len(checks) == 1
                assert checks[0].name == "git"
                assert checks[0].passed is True

    async def test_check_git_not_found(self) -> None:
        with patch("sova.utils.diagnostics.shutil.which", return_value=None):
            checks = await _check_git()
            assert len(checks) == 1
            assert checks[0].passed is False
            assert "not found" in checks[0].detail

    async def test_check_gh_cli_not_found(self) -> None:
        with patch("sova.utils.diagnostics.shutil.which", return_value=None):
            checks = await _check_gh_cli()
            assert len(checks) == 2
            assert checks[0].passed is False
            assert checks[1].passed is False

    async def test_check_claude_cli_not_found(self) -> None:
        with patch("sova.utils.diagnostics.shutil.which", return_value=None):
            checks = await _check_claude_cli()
            assert len(checks) == 1
            assert checks[0].passed is False

    async def test_check_git_hooks(self, tmp_path: Path) -> None:
        check = await _check_git_hooks(tmp_path)
        assert check.name == "git hooks"
        assert isinstance(check.passed, bool)

    async def test_check_git_hooks_no_githooks_dir(self, tmp_path: Path) -> None:
        check = await _check_git_hooks(tmp_path)
        assert check.name == "git hooks"
        assert check.passed is True
        assert "not applicable" in check.detail
        assert check.required is False

    async def test_check_git_hooks_misconfigured(self, tmp_path: Path) -> None:
        (tmp_path / ".githooks").mkdir()

        with patch("sova.utils.diagnostics.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stdout="")
            check = await _check_git_hooks(tmp_path)

        assert check.name == "git hooks"
        assert check.passed is False
        assert "not set" in check.detail
        assert check.required is True

    async def test_check_git_hooks_configured(self, tmp_path: Path) -> None:
        (tmp_path / ".githooks").mkdir()

        with patch("sova.utils.diagnostics.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=".githooks\n")
            check = await _check_git_hooks(tmp_path)

        assert check.name == "git hooks"
        assert check.passed is True
        assert check.detail == ".githooks"

    def test_check_sova_config_no_toml(self, tmp_path: Path) -> None:
        checks = _check_sova_config(tmp_path)
        assert len(checks) == 1
        assert checks[0].name == "config"
        assert checks[0].passed is False

    def test_check_github_config(self) -> None:
        cfg = MagicMock()
        cfg.github_repo = "owner/repo"
        cfg.github_user = "user"
        checks = _check_github_config(cfg)
        assert len(checks) == 2
        assert checks[0].passed is True
        assert checks[1].passed is True

    async def test_check_git_identity_configured(self, tmp_path: Path) -> None:
        identity = GitIdentityResult(name="Test User", email="test@example.com")
        with patch("sova.utils.diagnostics.check_git_identity", new_callable=AsyncMock, return_value=identity):
            checks = await _check_git_identity(tmp_path)
        assert len(checks) == 1
        assert checks[0].name == "git identity"
        assert checks[0].passed is True
        assert "Test User" in checks[0].detail

    async def test_check_git_identity_missing(self, tmp_path: Path) -> None:
        identity = GitIdentityResult(name="", email="")
        with patch("sova.utils.diagnostics.check_git_identity", new_callable=AsyncMock, return_value=identity):
            checks = await _check_git_identity(tmp_path)
        assert len(checks) == 1
        assert checks[0].name == "git identity"
        assert checks[0].passed is False
        assert "user.name" in checks[0].detail
        assert checks[0].required is True

    async def test_check_agent_runtime_available(self, tmp_path: Path) -> None:
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.ipc.runtime.create_runtime") as mock_create,
        ):
            mock_cfg.return_value.agent.runtime = "claude-code"
            mock_rt = MagicMock()
            mock_rt.check_available = AsyncMock(return_value=(True, "1.0.0"))
            mock_create.return_value = mock_rt

            checks = await _check_agent_runtime(tmp_path)

        assert len(checks) == 1
        assert checks[0].passed is True
        assert "claude-code" in checks[0].detail

    async def test_check_agent_runtime_not_available(self, tmp_path: Path) -> None:
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.ipc.runtime.create_runtime") as mock_create,
        ):
            mock_cfg.return_value.agent.runtime = "aider"
            mock_rt = MagicMock()
            mock_rt.check_available = AsyncMock(return_value=(False, "not found"))
            mock_create.return_value = mock_rt

            checks = await _check_agent_runtime(tmp_path)

        assert len(checks) == 1
        assert checks[0].passed is False

    async def test_check_agent_runtime_unknown_type(self, tmp_path: Path) -> None:
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.ipc.runtime.create_runtime", side_effect=ValueError("Unknown")),
        ):
            mock_cfg.return_value.agent.runtime = "bogus"

            checks = await _check_agent_runtime(tmp_path)

        assert len(checks) == 1
        assert checks[0].passed is False
        assert "Unknown" in checks[0].detail

    async def test_check_agent_runtime_generic_exception(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config", side_effect=FileNotFoundError("no config")):
            checks = await _check_agent_runtime(tmp_path)

        assert len(checks) == 1
        assert checks[0].passed is False

    async def test_check_agent_runtime_codex_forwards_config(self, tmp_path: Path) -> None:
        """The [codex] section must reach create_runtime() so CodexRuntime probes with it."""
        codex_cfg = CodexConfig(model="gpt-5-codex", allow_unenforced_budget=True)
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.ipc.runtime.create_runtime") as mock_create,
        ):
            mock_cfg.return_value.agent.runtime = "codex"
            mock_cfg.return_value.codex = codex_cfg
            mock_rt = MagicMock()
            mock_rt.check_available = AsyncMock(return_value=(True, "authenticated"))
            mock_create.return_value = mock_rt

            checks = await _check_agent_runtime(tmp_path)

        mock_create.assert_called_once_with("codex", codex=codex_cfg)
        assert len(checks) == 1
        assert checks[0].passed is True

    async def test_check_agent_runtime_codex_unenforced_budget_not_set_fails(self, tmp_path: Path) -> None:
        """agent.max_budget is always positive, so a Codex project without this flag set
        will fail every pipeline run on its first tool-using step; doctor must flag it
        before the operator ever starts one."""
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.ipc.runtime.create_runtime") as mock_create,
        ):
            mock_cfg.return_value.agent.runtime = "codex"
            mock_cfg.return_value.codex = CodexConfig()
            mock_rt = MagicMock()
            mock_rt.check_available = AsyncMock(return_value=(True, "authenticated"))
            mock_create.return_value = mock_rt

            checks = await _check_agent_runtime(tmp_path)

        assert len(checks) == 2
        budget_check = next(c for c in checks if c.name == "codex budget enforcement")
        assert budget_check.passed is False
        assert budget_check.required is True
        assert "allow_unenforced_budget" in budget_check.detail

    async def test_check_agent_runtime_non_codex_skips_budget_check(self, tmp_path: Path) -> None:
        """The budget-enforcement check is Codex-specific and must not fire for other runtimes."""
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.ipc.runtime.create_runtime") as mock_create,
        ):
            mock_cfg.return_value.agent.runtime = "claude-code"
            mock_rt = MagicMock()
            mock_rt.check_available = AsyncMock(return_value=(True, "1.0.0"))
            mock_create.return_value = mock_rt

            checks = await _check_agent_runtime(tmp_path)

        assert len(checks) == 1

    async def test_check_awareness_providers_disabled(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value.awareness.enabled = False
            checks = await _check_awareness_providers(tmp_path)

        assert checks == []

    async def test_check_awareness_providers_none_configured(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value.awareness.enabled = True
            mock_cfg.return_value.awareness.providers = []
            checks = await _check_awareness_providers(tmp_path)

        assert len(checks) == 1
        assert checks[0].name == "awareness providers"
        assert checks[0].passed is True
        assert checks[0].required is False

    async def test_check_awareness_providers_healthy(self, tmp_path: Path) -> None:
        mock_provider = MagicMock()
        mock_provider.name = "agent_runs"
        mock_provider.health_check = AsyncMock(return_value=(True, "SOVA Agent Runs: ok"))

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.awareness.create_providers", return_value=[mock_provider]),
        ):
            mock_cfg.return_value.awareness.enabled = True
            mock_cfg.return_value.awareness.providers = ["agent_runs"]

            checks = await _check_awareness_providers(tmp_path)

        assert len(checks) == 1
        assert checks[0].name == "awareness: agent_runs"
        assert checks[0].passed is True
        assert checks[0].required is False

    async def test_check_awareness_providers_unhealthy(self, tmp_path: Path) -> None:
        mock_provider = MagicMock()
        mock_provider.name = "gmail"
        mock_provider.health_check = AsyncMock(return_value=(False, "Gmail: not configured"))

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.awareness.create_providers", return_value=[mock_provider]),
        ):
            mock_cfg.return_value.awareness.enabled = True
            mock_cfg.return_value.awareness.providers = ["gmail"]

            checks = await _check_awareness_providers(tmp_path)

        assert len(checks) == 1
        assert checks[0].passed is False
        assert "not configured" in checks[0].detail

    async def test_check_awareness_providers_missing_dependency(self, tmp_path: Path) -> None:
        """A configured provider absent from the registry (e.g. missing optional dep)."""
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.awareness.create_providers", return_value=[]),
        ):
            mock_cfg.return_value.awareness.enabled = True
            mock_cfg.return_value.awareness.providers = ["gmail"]

            checks = await _check_awareness_providers(tmp_path)

        assert len(checks) == 1
        assert checks[0].name == "awareness: gmail"
        assert checks[0].passed is False
        assert "missing optional dependency" in checks[0].detail

    async def test_check_awareness_providers_health_check_raises(self, tmp_path: Path) -> None:
        """One provider raising must not hide the others or fail the whole check."""
        broken = MagicMock()
        broken.name = "gmail"
        broken.health_check = AsyncMock(side_effect=ConnectionError("unreachable"))
        healthy = MagicMock()
        healthy.name = "agent_runs"
        healthy.health_check = AsyncMock(return_value=(True, "SOVA Agent Runs: ok"))

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.awareness.create_providers", return_value=[broken, healthy]),
        ):
            mock_cfg.return_value.awareness.enabled = True
            mock_cfg.return_value.awareness.providers = ["gmail", "agent_runs"]

            checks = await _check_awareness_providers(tmp_path)

        assert [(c.name, c.passed) for c in checks] == [
            ("awareness: gmail", False),
            ("awareness: agent_runs", True),
        ]
        assert "unreachable" in checks[0].detail

    async def test_check_awareness_providers_generic_exception(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config", side_effect=FileNotFoundError("no config")):
            checks = await _check_awareness_providers(tmp_path)

        assert len(checks) == 1
        assert checks[0].passed is False

    async def test_check_awareness_providers_timeout_reported_as_failed_check(self, tmp_path: Path) -> None:
        """A hung provider must time out instead of blocking the caller forever."""

        async def _hangs() -> tuple[bool, str]:
            await asyncio.sleep(999)
            return True, "unreachable"

        fake_provider = MagicMock()
        fake_provider.name = "gmail"
        fake_provider.health_check = _hangs

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.awareness.create_providers", return_value=[fake_provider]),
            patch("sova.utils.diagnostics._AWARENESS_HEALTH_CHECK_TIMEOUT", 0.05),
        ):
            mock_cfg.return_value.awareness.enabled = True
            mock_cfg.return_value.awareness.providers = ["gmail"]

            checks = await _check_awareness_providers(tmp_path)

        assert len(checks) == 1
        check = checks[0]
        assert check.name == "awareness: gmail"
        assert check.passed is False
        assert check.required is False

    async def test_check_awareness_providers_reports_exception_not_cancellation(self, tmp_path: Path) -> None:
        """A BaseException branch must not swallow CancelledError as a failed check."""

        async def _raises_value_error() -> tuple[bool, str]:
            raise ValueError("boom")

        async def _raises_cancelled() -> tuple[bool, str]:
            raise asyncio.CancelledError()

        provider_ok = MagicMock()
        provider_ok.name = "gmail"
        provider_ok.health_check = _raises_value_error

        provider_cancelled = MagicMock()
        provider_cancelled.name = "gcal"
        provider_cancelled.health_check = _raises_cancelled

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.awareness.create_providers", return_value=[provider_ok]),
        ):
            mock_cfg.return_value.awareness.enabled = True
            mock_cfg.return_value.awareness.providers = ["gmail"]

            checks = await _check_awareness_providers(tmp_path)

        assert len(checks) == 1
        assert checks[0].name == "awareness: gmail"
        assert checks[0].passed is False
        assert "boom" in checks[0].detail

        # A CancelledError raised from health_check must propagate, not be
        # reported as a failed check row.
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.awareness.create_providers", return_value=[provider_cancelled]),
            pytest.raises(asyncio.CancelledError),
        ):
            mock_cfg.return_value.awareness.enabled = True
            mock_cfg.return_value.awareness.providers = ["gcal"]

            await _check_awareness_providers(tmp_path)


# ---------------------------------------------------------------------------
# Ollama check
# ---------------------------------------------------------------------------


def _mock_llm_config(
    mock_cfg: MagicMock,
    *,
    routing: dict[str, str] | None = None,
    model_aliases: dict[str, str] | None = None,
    model: str = "",
    fallback_model: str = "",
    provider: str = "claude-code",
) -> None:
    """Populate a patched load_config mock with concrete llm fields.

    Every field _check_ollama reads must be a real string or dict: a bare
    MagicMock attribute satisfies `.startswith("ollama/")` (and compares
    unequal to "ollama") and would add a phantom model to every check.
    """
    mock_cfg.return_value.llm.routing = routing or {}
    mock_cfg.return_value.llm.model_aliases = model_aliases or {}
    mock_cfg.return_value.llm.model = model
    mock_cfg.return_value.llm.fallback_model = fallback_model
    mock_cfg.return_value.llm.provider = provider


class TestOllamaCheck:
    async def test_no_ollama_models_returns_empty(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config") as mock_cfg:
            _mock_llm_config(mock_cfg, routing={"trivial": "haiku"})
            checks = await _check_ollama(tmp_path)
            assert checks == []

    async def test_ollama_not_installed(self, tmp_path: Path) -> None:
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.utils.diagnostics.shutil.which", return_value=None),
        ):
            _mock_llm_config(mock_cfg, routing={"triage": "ollama/qwen3:8b"})
            checks = await _check_ollama(tmp_path)
            assert len(checks) == 1
            assert checks[0].name == "ollama CLI"
            assert checks[0].passed is False

    async def test_ollama_not_running(self, tmp_path: Path) -> None:
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.utils.diagnostics.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.utils.diagnostics.run", new_callable=AsyncMock) as mock_run,
        ):
            _mock_llm_config(mock_cfg, routing={"triage": "ollama/qwen3:8b"})
            mock_run.return_value = MagicMock(success=False, stdout="")
            checks = await _check_ollama(tmp_path)
            assert any(c.name == "ollama running" and c.passed is False for c in checks)

    async def test_ollama_model_installed(self, tmp_path: Path) -> None:
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.utils.diagnostics.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.utils.diagnostics.run", new_callable=AsyncMock) as mock_run,
        ):
            _mock_llm_config(mock_cfg, routing={"triage": "ollama/qwen3:8b"})
            mock_run.return_value = MagicMock(
                success=True,
                stdout="NAME\tID\tSIZE\tMODIFIED\nqwen3:8b\tabc123\t5.0 GB\t2 hours ago\n",
            )
            checks = await _check_ollama(tmp_path)
            assert any(c.name == "ollama running" and c.passed is True for c in checks)
            model_check = [c for c in checks if "qwen3" in c.name]
            assert model_check
            assert model_check[0].passed is True

    async def test_alias_map_targets_are_checked(self, tmp_path: Path) -> None:
        """An alias pointing at a local model must be checked like a routing entry."""
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.utils.diagnostics.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.utils.diagnostics.run", new_callable=AsyncMock) as mock_run,
        ):
            _mock_llm_config(mock_cfg, model_aliases={"smart": "ollama/llama3.1:70b"})
            mock_run.return_value = MagicMock(success=True, stdout="NAME\tID\nqwen3:8b\tabc123\n")
            checks = await _check_ollama(tmp_path)

        model_check = [c for c in checks if "llama3.1" in c.name]
        assert model_check
        assert model_check[0].passed is False

    async def test_ollama_model_tag_mismatch_is_not_reported_installed(self, tmp_path: Path) -> None:
        """A configured tag must not match a different installed tag of the same base model."""
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.utils.diagnostics.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.utils.diagnostics.run", new_callable=AsyncMock) as mock_run,
        ):
            _mock_llm_config(mock_cfg, routing={"triage": "ollama/llama3.1:70b"})
            mock_run.return_value = MagicMock(success=True, stdout="NAME\tID\nllama3.1:8b\tabc123\n")
            checks = await _check_ollama(tmp_path)

        model_check = [c for c in checks if "llama3.1" in c.name]
        assert model_check
        assert model_check[0].passed is False

    async def test_ollama_model_without_tag_matches_latest(self, tmp_path: Path) -> None:
        """A configured model with no explicit tag matches an installed ':latest' pull."""
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.utils.diagnostics.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.utils.diagnostics.run", new_callable=AsyncMock) as mock_run,
        ):
            _mock_llm_config(mock_cfg, routing={"triage": "ollama/llama3.1"})
            mock_run.return_value = MagicMock(success=True, stdout="NAME\tID\nllama3.1:latest\tabc123\n")
            checks = await _check_ollama(tmp_path)

        model_check = [c for c in checks if "llama3.1" in c.name]
        assert model_check
        assert model_check[0].passed is True

    async def test_ollama_provider_without_prefix_is_flagged(self, tmp_path: Path) -> None:
        """provider = 'ollama' with an unprefixed model must not silently check nothing."""
        with patch("sova.config.loader.load_config") as mock_cfg:
            _mock_llm_config(mock_cfg, provider="ollama", model="llama3.1")
            checks = await _check_ollama(tmp_path)

        assert len(checks) == 1
        assert checks[0].name == "ollama model prefix"
        assert checks[0].passed is False
        assert "ollama/" in checks[0].detail

    async def test_non_ollama_provider_without_models_stays_silent(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config") as mock_cfg:
            _mock_llm_config(mock_cfg, provider="openai", model="gpt-5")
            assert await _check_ollama(tmp_path) == []

    async def test_llm_model_field_is_checked(self, tmp_path: Path) -> None:
        """provider = 'ollama' with llm.model set directly (no routing entry) is checked."""
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.utils.diagnostics.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.utils.diagnostics.run", new_callable=AsyncMock) as mock_run,
        ):
            _mock_llm_config(mock_cfg, model="ollama/llama3.1")
            mock_run.return_value = MagicMock(success=True, stdout="NAME\tID\nqwen3:8b\tabc123\n")
            checks = await _check_ollama(tmp_path)

        model_check = [c for c in checks if "llama3.1" in c.name]
        assert model_check
        assert model_check[0].passed is False

    async def test_llm_fallback_model_field_is_checked(self, tmp_path: Path) -> None:
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.utils.diagnostics.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.utils.diagnostics.run", new_callable=AsyncMock) as mock_run,
        ):
            _mock_llm_config(mock_cfg, model="gpt-5", fallback_model="ollama/llama3.1")
            mock_run.return_value = MagicMock(success=True, stdout="NAME\tID\nllama3.1:latest\tabc123\n")
            checks = await _check_ollama(tmp_path)

        model_check = [c for c in checks if "llama3.1" in c.name]
        assert model_check
        assert model_check[0].passed is True

    async def test_ollama_model_not_pulled(self, tmp_path: Path) -> None:
        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.utils.diagnostics.shutil.which", return_value="/usr/local/bin/ollama"),
            patch("sova.utils.diagnostics.run", new_callable=AsyncMock) as mock_run,
        ):
            _mock_llm_config(mock_cfg, routing={"triage": "ollama/qwen3:8b"})
            mock_run.return_value = MagicMock(
                success=True,
                stdout="NAME\tID\tSIZE\tMODIFIED\nllama3:8b\tabc123\t5.0 GB\t2 hours ago\n",
            )
            checks = await _check_ollama(tmp_path)
            model_check = [c for c in checks if "qwen3" in c.name]
            assert model_check
            assert model_check[0].passed is False
            assert "ollama pull" in model_check[0].detail


# ---------------------------------------------------------------------------
# RTK check
# ---------------------------------------------------------------------------


def test_check_rtk_available() -> None:
    with patch("sova.utils.diagnostics.shutil.which", return_value="/usr/local/bin/rtk"):
        checks = _check_rtk()
    assert len(checks) == 1
    check = checks[0]
    assert check.name == "rtk"
    assert check.passed is True
    assert check.required is False


def test_check_rtk_missing() -> None:
    with patch("sova.utils.diagnostics.shutil.which", return_value=None):
        checks = _check_rtk()
    assert len(checks) == 1
    check = checks[0]
    assert check.name == "rtk"
    assert check.passed is False
    assert check.required is False


# ---------------------------------------------------------------------------
# run_all_checks ordering
# ---------------------------------------------------------------------------


async def test_run_all_checks_includes_the_schema_check(tmp_path: Path) -> None:
    """run_all_checks() must include every check's row, or the check exists but never
    reaches a caller (CLI or dashboard)."""
    from contextlib import ExitStack

    from sova.utils import diagnostics as mod

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
                mod,
                "_check_git_hooks",
                new_callable=AsyncMock,
                return_value=DiagnosticCheck("git hooks", True, "", False),
            )
        )
        stack.enter_context(
            patch.object(
                mod,
                "_check_database_schema",
                new_callable=AsyncMock,
                return_value=[DiagnosticCheck("database schema", True, "at head (050)", False)],
            )
        )

        checks = await mod.run_all_checks(tmp_path)

    assert DiagnosticCheck("database schema", True, "at head (050)", False) in checks


async def test_a_raising_check_is_one_failed_row_and_the_rest_still_run(tmp_path: Path) -> None:
    """A check with no internal guard must not abort the whole report.

    `sova doctor` would print a traceback instead of its table, and the
    dashboard panel would lose all sixteen rows to one batch-level failure.
    """
    from sova.utils import diagnostics as mod

    with patch.object(mod, "_check_install_completeness", side_effect=OSError("disk gone")):
        checks = await mod.run_all_checks(tmp_path)

    crashed = [c for c in checks if c.name == "install completeness"]
    assert len(crashed) == 1
    assert crashed[0].passed is False
    # Optional, so a broken check never fails `sova doctor` with exit code 1.
    assert crashed[0].required is False
    assert "disk gone" in crashed[0].detail
    # Every other check still reported.
    assert any(c.name == "Python >= 3.12" and c.passed for c in checks)


async def test_a_raising_check_does_not_swallow_cancellation(tmp_path: Path) -> None:
    """CancelledError derives from BaseException and must keep propagating."""
    from sova.utils import diagnostics as mod

    with patch.object(mod, "_check_rtk", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await mod.run_all_checks(tmp_path)


# ---------------------------------------------------------------------------
# Config discovery and validation
# ---------------------------------------------------------------------------


class TestSovaConfigCheck:
    """`_check_sova_config` reports where config came from, then validates it."""

    def test_config_source_is_the_database_when_db_config_loads(self, tmp_path: Path) -> None:
        with patch("sova.config.db_loader._try_load_from_db", return_value=MagicMock()):
            with patch("sova.utils.diagnostics._validate_sova_config", return_value=[]):
                checks = _check_sova_config(tmp_path)

        config_rows = [c for c in checks if c.name == "config"]
        assert len(config_rows) == 1
        assert config_rows[0].passed is True
        assert config_rows[0].detail == "database"
        # No sova.toml on disk, so no legacy row.
        assert not any(c.name == "legacy sova.toml" for c in checks)

    def test_config_source_is_the_toml_path_when_only_a_toml_exists(self, tmp_path: Path) -> None:
        (tmp_path / "sova.toml").write_text("[task_source]\ntype = 'github'\n")

        with patch("sova.config.db_loader._try_load_from_db", return_value=None):
            with patch("sova.utils.diagnostics._validate_sova_config", return_value=[]):
                checks = _check_sova_config(tmp_path)

        config_rows = [c for c in checks if c.name == "config"]
        assert config_rows[0].passed is True
        assert config_rows[0].detail == str(tmp_path / "sova.toml")

    def test_a_toml_alongside_db_config_reports_a_legacy_row(self, tmp_path: Path) -> None:
        """Both present is actionable: the DB wins and the toml is silently ignored."""
        (tmp_path / "sova.toml").write_text("[task_source]\ntype = 'github'\n")

        with patch("sova.config.db_loader._try_load_from_db", return_value=MagicMock()):
            with patch("sova.utils.diagnostics._validate_sova_config", return_value=[]):
                checks = _check_sova_config(tmp_path)

        legacy = [c for c in checks if c.name == "legacy sova.toml"]
        assert len(legacy) == 1
        assert legacy[0].passed is False
        assert legacy[0].required is False
        assert "DB takes priority" in legacy[0].detail

    def test_missing_config_names_both_places_it_looked(self, tmp_path: Path) -> None:
        with patch("sova.config.db_loader._try_load_from_db", return_value=None):
            checks = _check_sova_config(tmp_path)

        assert len(checks) == 1
        assert checks[0].passed is False
        assert "not found (checked DB and" in checks[0].detail
        assert str(tmp_path / "sova.toml") in checks[0].detail

    def test_validation_rows_are_appended_once_config_is_found(self, tmp_path: Path) -> None:
        sentinel = DiagnosticCheck("task_source.type", True, "github", False)
        with patch("sova.config.db_loader._try_load_from_db", return_value=MagicMock()):
            with patch("sova.utils.diagnostics._validate_sova_config", return_value=[sentinel]) as mock_validate:
                checks = _check_sova_config(tmp_path)

        mock_validate.assert_called_once_with(tmp_path)
        assert sentinel in checks


class TestValidateSovaConfig:
    """`_validate_sova_config` dispatches on task_source.type and never raises."""

    def test_github_task_source_adds_the_github_rows(self, tmp_path: Path) -> None:
        cfg = MagicMock()
        cfg.task_source.type = "github"
        cfg.github_repo = "owner/repo"
        cfg.github_user = "someone"

        with patch("sova.config.loader.load_config", return_value=cfg):
            checks = _validate_sova_config(tmp_path)

        names = [c.name for c in checks]
        assert names == ["task_source.type", "github_repo configured", "github_user configured"]
        assert all(c.passed for c in checks)

    def test_jira_task_source_adds_the_jira_rows(self, tmp_path: Path) -> None:
        cfg = MagicMock()
        cfg.task_source.type = "jira"
        cfg.task_source.jira_base_url = "https://example.atlassian.net"
        cfg.task_source.jira_email = "someone@example.com"
        cfg.task_source.jira_api_token = "token"
        cfg.task_source.jira_project_key = "PROJ"

        with patch("sova.config.loader.load_config", return_value=cfg):
            checks = _validate_sova_config(tmp_path)

        names = [c.name for c in checks]
        assert names == [
            "task_source.type",
            "jira_base_url",
            "jira_email",
            "jira_api_token",
            "jira_project_key",
        ]
        assert all(c.passed for c in checks)

    def test_an_unknown_task_source_adds_no_provider_rows(self, tmp_path: Path) -> None:
        cfg = MagicMock()
        cfg.task_source.type = "manual"

        with patch("sova.config.loader.load_config", return_value=cfg):
            checks = _validate_sova_config(tmp_path)

        assert [c.name for c in checks] == ["task_source.type"]

    def test_a_load_failure_becomes_one_failed_row(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config", side_effect=ValueError("bad toml")):
            checks = _validate_sova_config(tmp_path)

        assert len(checks) == 1
        assert checks[0].name == "config valid"
        assert checks[0].passed is False
        assert checks[0].required is False
        assert "bad toml" in checks[0].detail


class TestJiraConfigCheck:
    """`_check_jira_config` reports presence, and never echoes the API token."""

    def test_unset_jira_fields_fail_and_render_as_empty(self) -> None:
        cfg = MagicMock()
        cfg.task_source.jira_base_url = ""
        cfg.task_source.jira_email = ""
        cfg.task_source.jira_api_token = ""
        cfg.task_source.jira_project_key = ""

        checks = _check_jira_config(cfg)

        assert len(checks) == 4
        assert not any(c.passed for c in checks)
        assert all(c.detail == "(empty)" for c in checks)

    def test_a_set_token_is_reported_without_its_value(self) -> None:
        cfg = MagicMock()
        cfg.task_source.jira_base_url = "https://example.atlassian.net"
        cfg.task_source.jira_email = "someone@example.com"
        cfg.task_source.jira_api_token = "super-secret-token"
        cfg.task_source.jira_project_key = "PROJ"

        checks = _check_jira_config(cfg)
        token_row = next(c for c in checks if c.name == "jira_api_token")

        assert token_row.passed is True
        assert token_row.detail == "(set)"
        assert "super-secret-token" not in token_row.detail


# ---------------------------------------------------------------------------
# LLM provider
# ---------------------------------------------------------------------------


class TestLLMProviderCheck:
    """`_check_llm_provider` probes the configured provider and never raises."""

    async def test_a_reachable_provider_passes_and_names_the_provider_type(self, tmp_path: Path) -> None:
        cfg = MagicMock()
        cfg.llm.provider = "anthropic-api"
        provider = MagicMock()
        provider.check_available = AsyncMock(return_value=(True, "authenticated"))

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.llm.provider.create_provider", return_value=provider),
        ):
            checks = await _check_llm_provider(tmp_path)

        assert len(checks) == 1
        assert checks[0].name == "llm provider"
        assert checks[0].passed is True
        assert checks[0].required is True
        assert checks[0].detail == "anthropic-api: authenticated"

    async def test_an_unreachable_provider_fails_but_stays_required(self, tmp_path: Path) -> None:
        cfg = MagicMock()
        cfg.llm.provider = "claude-code"
        provider = MagicMock()
        provider.check_available = AsyncMock(return_value=(False, "not authenticated, run: claude login"))

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.llm.provider.create_provider", return_value=provider),
        ):
            checks = await _check_llm_provider(tmp_path)

        assert checks[0].passed is False
        assert checks[0].required is True
        assert "claude login" in checks[0].detail

    async def test_an_unknown_provider_type_reports_the_value_error_as_required(self, tmp_path: Path) -> None:
        """create_provider raises ValueError for an unknown llm.provider value."""
        cfg = MagicMock()
        cfg.llm.provider = "nonsuch"

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.llm.provider.create_provider", side_effect=ValueError("unknown provider: nonsuch")),
        ):
            checks = await _check_llm_provider(tmp_path)

        assert len(checks) == 1
        assert checks[0].passed is False
        # A misconfigured provider is required: SOVA cannot run without one.
        assert checks[0].required is True
        assert checks[0].detail == "unknown provider: nonsuch"

    async def test_a_probe_that_raises_becomes_one_optional_failed_row(self, tmp_path: Path) -> None:
        """An arbitrary provider error must not abort the whole report."""
        cfg = MagicMock()
        cfg.llm.provider = "litellm"
        provider = MagicMock()
        provider.check_available = AsyncMock(side_effect=RuntimeError("socket hung up"))

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.llm.provider.create_provider", return_value=provider),
        ):
            checks = await _check_llm_provider(tmp_path)

        assert len(checks) == 1
        assert checks[0].passed is False
        # Optional on the error path, so a broken probe never fails `sova doctor`.
        assert checks[0].required is False
        assert "socket hung up" in checks[0].detail

    async def test_an_unloadable_config_becomes_one_optional_failed_row(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config", side_effect=OSError("config unreadable")):
            checks = await _check_llm_provider(tmp_path)

        assert len(checks) == 1
        assert checks[0].name == "llm provider"
        assert checks[0].passed is False
        assert checks[0].required is False
        assert "config unreadable" in checks[0].detail


# ---------------------------------------------------------------------------
# Remaining per-check branches
# ---------------------------------------------------------------------------


class TestRemainingCheckBranches:
    """Branches the happy-path tests above don't reach."""

    def test_extract_auth_detail_authenticated_with_no_recognizable_line(self) -> None:
        """`gh auth status` succeeded, so the row passes even with nothing to quote."""
        result = ShellResult(returncode=0, stdout="unexpected output\n", stderr="")
        assert _extract_auth_detail(result, auth_ok=True) == ""

    async def test_git_hooks_set_to_the_wrong_path_reports_the_configured_value(self, tmp_path: Path) -> None:
        (tmp_path / ".githooks").mkdir()

        with patch("sova.utils.diagnostics.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=".git/hooks\n")
            check = await _check_git_hooks(tmp_path)

        assert check.passed is False
        assert check.required is True
        assert "set to '.git/hooks'" in check.detail
        assert "expected '.githooks'" in check.detail

    async def test_git_hooks_unset_with_githooks_present_is_a_required_failure(self, tmp_path: Path) -> None:
        """Invariants are silently bypassed when core.hooksPath is unset."""
        (tmp_path / ".githooks").mkdir()

        with patch("sova.utils.diagnostics.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stdout="")
            check = await _check_git_hooks(tmp_path)

        assert check.passed is False
        assert check.required is True
        assert check.detail == "not set, run: git config core.hooksPath .githooks"

    async def test_an_unloadable_config_becomes_one_failed_ollama_row(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config", side_effect=OSError("config unreadable")):
            checks = await _check_ollama(tmp_path)

        assert len(checks) == 1
        assert checks[0].name == "ollama"
        assert checks[0].passed is False
        assert checks[0].required is False
        assert "config unreadable" in checks[0].detail
