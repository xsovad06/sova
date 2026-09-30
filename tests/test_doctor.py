"""Tests for sova.cli.commands.doctor: keyring secret diagnostics."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sova.cli.commands.doctor import _check_keyring_secrets


async def test_no_checks_when_nothing_stored(tmp_path: Path) -> None:
    from sova.dashboard.services import settings_service

    with patch.object(settings_service, "_get_raw_config", return_value={}):
        checks = await _check_keyring_secrets(tmp_path)

    assert checks == []


async def test_no_checks_when_sentinel_resolves(tmp_path: Path) -> None:
    from sova.dashboard.services import settings_service
    from sova.llm import keyring_store

    with (
        patch.object(settings_service, "_get_raw_config", return_value={"llm.api_key": keyring_store.SENTINEL}),
        patch.object(keyring_store, "is_keyring_available", return_value=True),
        patch.object(keyring_store, "get_secret", return_value="sk-ant-real"),
    ):
        checks = await _check_keyring_secrets(tmp_path)

    assert checks == []


async def test_reports_sentinel_without_matching_entry(tmp_path: Path) -> None:
    from sova.dashboard.services import settings_service
    from sova.llm import keyring_store

    with (
        patch.object(settings_service, "_get_raw_config", return_value={"llm.api_key": keyring_store.SENTINEL}),
        patch.object(keyring_store, "is_keyring_available", return_value=False),
    ):
        checks = await _check_keyring_secrets(tmp_path)

    assert len(checks) == 1
    name, passed, detail, required = checks[0]
    assert name == "keyring: llm.api_key"
    assert passed is False
    assert "sentinel" in detail
    assert required is False


async def test_reports_sentinel_when_keyring_available_but_entry_missing(tmp_path: Path) -> None:
    from sova.dashboard.services import settings_service
    from sova.llm import keyring_store

    with (
        patch.object(settings_service, "_get_raw_config", return_value={"llm.api_key": keyring_store.SENTINEL}),
        patch.object(keyring_store, "is_keyring_available", return_value=True),
        patch.object(keyring_store, "get_secret", return_value=None),
    ):
        checks = await _check_keyring_secrets(tmp_path)

    assert len(checks) == 1
    assert checks[0][0] == "keyring: llm.api_key"


async def test_plaintext_value_not_reported(tmp_path: Path) -> None:
    """A plaintext-stored secret (not the sentinel) is normal and not flagged."""
    from sova.dashboard.services import settings_service

    with patch.object(settings_service, "_get_raw_config", return_value={"llm.api_key": "sk-ant-plaintext"}):
        checks = await _check_keyring_secrets(tmp_path)

    assert checks == []


async def test_failure_reported_as_failed_check(tmp_path: Path) -> None:
    from sova.dashboard.services import settings_service

    with patch.object(settings_service, "_get_raw_config", side_effect=RuntimeError("boom")):
        checks = await _check_keyring_secrets(tmp_path)

    assert len(checks) == 1
    assert checks[0][0] == "keyring secrets"
    assert checks[0][1] is False
