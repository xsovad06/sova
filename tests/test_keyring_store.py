"""Tests for the optional OS-keyring-backed secret store."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sova.llm import keyring_store


class _FakeFailKeyring:
    """Stand-in for keyring.backends.fail.Keyring."""


class _FakeUsableKeyring:
    """Stand-in for a real, usable backend (e.g. macOS Keychain)."""


class _FakePasswordDeleteError(Exception):
    """Stand-in for keyring.errors.PasswordDeleteError."""


def _patch_importable(fail_cls: type = _FakeFailKeyring):
    """Patch the module as if the ``keyring`` package were importable."""
    return (
        patch.object(keyring_store, "_KEYRING_IMPORTABLE", True),
        patch.object(keyring_store, "_FailKeyring", fail_cls, create=True),
        patch.object(keyring_store, "_backend_usable", None),
    )


# is_keyring_available


def test_is_keyring_available_false_when_not_importable() -> None:
    with (
        patch.object(keyring_store, "_KEYRING_IMPORTABLE", False),
        patch.object(keyring_store, "_backend_usable", None),
    ):
        assert keyring_store.is_keyring_available() is False


def test_is_keyring_available_false_for_fail_backend() -> None:
    fake = MagicMock()
    fake.get_keyring.return_value = _FakeFailKeyring()
    p1, p2, p3 = _patch_importable()
    with p1, p2, p3, patch.object(keyring_store, "keyring", fake, create=True):
        assert keyring_store.is_keyring_available() is False


def test_is_keyring_available_true_for_usable_backend() -> None:
    fake = MagicMock()
    fake.get_keyring.return_value = _FakeUsableKeyring()
    p1, p2, p3 = _patch_importable()
    with p1, p2, p3, patch.object(keyring_store, "keyring", fake, create=True):
        assert keyring_store.is_keyring_available() is True


def test_is_keyring_available_false_on_probe_exception() -> None:
    fake = MagicMock()
    fake.get_keyring.side_effect = RuntimeError("no backend")
    p1, p2, p3 = _patch_importable()
    with p1, p2, p3, patch.object(keyring_store, "keyring", fake, create=True):
        assert keyring_store.is_keyring_available() is False


def test_is_keyring_available_caches_result() -> None:
    fake = MagicMock()
    fake.get_keyring.return_value = _FakeUsableKeyring()
    p1, p2, p3 = _patch_importable()
    with p1, p2, p3, patch.object(keyring_store, "keyring", fake, create=True):
        assert keyring_store.is_keyring_available() is True
        assert keyring_store.is_keyring_available() is True
    fake.get_keyring.assert_called_once()


# get_secret / set_secret / delete_secret


def test_get_secret_none_when_unavailable() -> None:
    with patch.object(keyring_store, "is_keyring_available", return_value=False):
        assert keyring_store.get_secret("llm.api_key") is None


def test_get_secret_returns_value() -> None:
    fake = MagicMock()
    fake.get_password.return_value = "sk-ant-real"
    with (
        patch.object(keyring_store, "is_keyring_available", return_value=True),
        patch.object(keyring_store, "keyring", fake, create=True),
    ):
        assert keyring_store.get_secret("llm.api_key") == "sk-ant-real"
    fake.get_password.assert_called_once_with(keyring_store.SERVICE_NAME, "llm.api_key")


def test_get_secret_none_on_locked_keychain() -> None:
    fake = MagicMock()
    fake.get_password.side_effect = RuntimeError("keychain locked")
    with (
        patch.object(keyring_store, "is_keyring_available", return_value=True),
        patch.object(keyring_store, "keyring", fake, create=True),
    ):
        assert keyring_store.get_secret("llm.api_key") is None


def test_set_secret_false_when_unavailable() -> None:
    with patch.object(keyring_store, "is_keyring_available", return_value=False):
        assert keyring_store.set_secret("llm.api_key", "sk-ant") is False


def test_set_secret_true_on_success() -> None:
    fake = MagicMock()
    with (
        patch.object(keyring_store, "is_keyring_available", return_value=True),
        patch.object(keyring_store, "keyring", fake, create=True),
    ):
        assert keyring_store.set_secret("llm.api_key", "sk-ant") is True
    fake.set_password.assert_called_once_with(keyring_store.SERVICE_NAME, "llm.api_key", "sk-ant")


def test_set_secret_false_on_exception() -> None:
    fake = MagicMock()
    fake.set_password.side_effect = RuntimeError("boom")
    with (
        patch.object(keyring_store, "is_keyring_available", return_value=True),
        patch.object(keyring_store, "keyring", fake, create=True),
    ):
        assert keyring_store.set_secret("llm.api_key", "sk-ant") is False


def test_delete_secret_false_when_unavailable() -> None:
    with patch.object(keyring_store, "is_keyring_available", return_value=False):
        assert keyring_store.delete_secret("llm.api_key") is False


def test_delete_secret_true_on_success() -> None:
    fake = MagicMock()
    with (
        patch.object(keyring_store, "is_keyring_available", return_value=True),
        patch.object(keyring_store, "keyring", fake, create=True),
    ):
        assert keyring_store.delete_secret("llm.api_key") is True


def test_delete_secret_true_when_already_absent() -> None:
    fake = MagicMock()
    fake.delete_password.side_effect = _FakePasswordDeleteError("not found")
    with (
        patch.object(keyring_store, "is_keyring_available", return_value=True),
        patch.object(keyring_store, "keyring", fake, create=True),
        patch.object(keyring_store, "PasswordDeleteError", _FakePasswordDeleteError),
    ):
        assert keyring_store.delete_secret("llm.api_key") is True


def test_delete_secret_false_on_other_exception() -> None:
    fake = MagicMock()
    fake.delete_password.side_effect = RuntimeError("boom")
    with (
        patch.object(keyring_store, "is_keyring_available", return_value=True),
        patch.object(keyring_store, "keyring", fake, create=True),
        patch.object(keyring_store, "PasswordDeleteError", _FakePasswordDeleteError),
    ):
        assert keyring_store.delete_secret("llm.api_key") is False


# resolve_secret


def test_resolve_secret_prefers_keyring() -> None:
    with patch.object(keyring_store, "get_secret", return_value="from-keyring"):
        assert keyring_store.resolve_secret("llm.api_key", "from-db") == "from-keyring"


def test_resolve_secret_falls_back_to_db_value() -> None:
    with patch.object(keyring_store, "get_secret", return_value=None):
        assert keyring_store.resolve_secret("llm.api_key", "from-db") == "from-db"


def test_resolve_secret_ignores_sentinel_db_value() -> None:
    """Sentinel present but keyring entry gone: resolves to empty, not the sentinel string."""
    with patch.object(keyring_store, "get_secret", return_value=None):
        assert keyring_store.resolve_secret("llm.api_key", keyring_store.SENTINEL) == ""


def test_resolve_secret_empty_when_nothing_found() -> None:
    with patch.object(keyring_store, "get_secret", return_value=None):
        assert keyring_store.resolve_secret("llm.api_key", None) == ""
        assert keyring_store.resolve_secret("llm.api_key", "") == ""


def test_resolve_secret_empty_keyring_value_falls_through() -> None:
    """An empty string from the keyring must not win over a real db value."""
    with patch.object(keyring_store, "get_secret", return_value=""):
        assert keyring_store.resolve_secret("llm.api_key", "from-db") == "from-db"


def test_resolve_secret_falls_back_to_legacy_name() -> None:
    """Scoped entry absent, legacy global entry present: legacy wins over the db value.

    Covers the upgrade edge case: a user with an existing global llm.api_key
    and no per-provider scoped entry must still authenticate correctly.
    """

    def _get(name: str) -> str | None:
        return "from-legacy" if name == "llm.api_key" else None

    with patch.object(keyring_store, "get_secret", side_effect=_get):
        resolved = keyring_store.resolve_secret("llm.api_key:anthropic:/proj", "from-db", legacy_name="llm.api_key")
    assert resolved == "from-legacy"


def test_resolve_secret_scoped_entry_wins_over_legacy() -> None:
    def _get(name: str) -> str | None:
        if name == "llm.api_key:anthropic:/proj":
            return "from-scoped"
        if name == "llm.api_key":
            return "from-legacy"
        return None

    with patch.object(keyring_store, "get_secret", side_effect=_get):
        resolved = keyring_store.resolve_secret("llm.api_key:anthropic:/proj", "from-db", legacy_name="llm.api_key")
    assert resolved == "from-scoped"


def test_resolve_secret_legacy_name_equal_to_name_is_not_double_read() -> None:
    """legacy_name identical to name must not trigger a second lookup/semantics change."""
    calls: list[str] = []

    def _get(name: str) -> str | None:
        calls.append(name)
        return None

    with patch.object(keyring_store, "get_secret", side_effect=_get):
        assert keyring_store.resolve_secret("llm.api_key", "from-db", legacy_name="llm.api_key") == "from-db"
    assert calls == ["llm.api_key"]


def test_resolve_secret_no_legacy_name_given_skips_legacy_lookup() -> None:
    with patch.object(keyring_store, "get_secret", return_value=None) as mock_get:
        assert keyring_store.resolve_secret("llm.api_key:anthropic:/proj", "from-db") == "from-db"
    mock_get.assert_called_once_with("llm.api_key:anthropic:/proj")


# scoped_secret_name


def test_scoped_secret_name_distinguishes_projects() -> None:
    a = keyring_store.scoped_secret_name("llm.api_key", "/home/user/project-a", "anthropic")
    b = keyring_store.scoped_secret_name("llm.api_key", "/home/user/project-b", "anthropic")
    assert a != b


def test_scoped_secret_name_distinguishes_providers() -> None:
    a = keyring_store.scoped_secret_name("llm.api_key", "/home/user/project", "anthropic")
    b = keyring_store.scoped_secret_name("llm.api_key", "/home/user/project", "openai")
    assert a != b


def test_scoped_secret_name_resolves_relative_paths() -> None:
    """Two different relative spellings of the same directory must scope identically."""
    import os

    cwd = os.getcwd()
    a = keyring_store.scoped_secret_name("llm.api_key", ".", "anthropic")
    b = keyring_store.scoped_secret_name("llm.api_key", cwd, "anthropic")
    assert a == b


def test_scoped_secret_name_never_collides_with_bare_legacy_name() -> None:
    scoped = keyring_store.scoped_secret_name("llm.api_key", "/home/user/project", "anthropic")
    assert scoped != "llm.api_key"


# migrate_plaintext_to_keyring


def test_migrate_plaintext_to_keyring_success() -> None:
    with (
        patch.object(keyring_store, "set_secret", return_value=True),
        patch.object(keyring_store, "get_secret", return_value="sk-ant"),
    ):
        assert keyring_store.migrate_plaintext_to_keyring("llm.api_key", "sk-ant") is True


def test_migrate_plaintext_to_keyring_set_fails() -> None:
    with patch.object(keyring_store, "set_secret", return_value=False):
        assert keyring_store.migrate_plaintext_to_keyring("llm.api_key", "sk-ant") is False


def test_migrate_plaintext_to_keyring_readback_mismatch() -> None:
    with (
        patch.object(keyring_store, "set_secret", return_value=True),
        patch.object(keyring_store, "get_secret", return_value="something-else"),
    ):
        assert keyring_store.migrate_plaintext_to_keyring("llm.api_key", "sk-ant") is False


def test_migrate_plaintext_to_keyring_readback_none() -> None:
    with (
        patch.object(keyring_store, "set_secret", return_value=True),
        patch.object(keyring_store, "get_secret", return_value=None),
    ):
        assert keyring_store.migrate_plaintext_to_keyring("llm.api_key", "sk-ant") is False


# Module-level constants


def test_resolved_secret_keys_contains_llm_api_key() -> None:
    assert "llm.api_key" in keyring_store.RESOLVED_SECRET_KEYS


def test_sentinel_is_not_a_plausible_real_secret() -> None:
    assert ":" in keyring_store.SENTINEL
