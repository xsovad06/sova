"""Tests for Google OAuth2 flow in the awareness subsystem."""

from __future__ import annotations

import pickle
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sova.awareness.auth.google_oauth import (
    _CACHE_TTL_SECONDS,
    AWARENESS_GOOGLE_SCOPES,
    DEFAULT_CREDENTIALS_PATH,
    DEFAULT_TOKEN_PATH,
    _creds_cache,
    authenticate_google,
)
from sova.config.models import AwarenessConfig

_MOD = "sova.awareness.auth.google_oauth"


@pytest.fixture()
def awareness_config() -> AwarenessConfig:
    return AwarenessConfig()


@pytest.fixture()
def token_path(tmp_path: Path) -> Path:
    return tmp_path / "sova" / "google_token.pickle"


@pytest.fixture()
def creds_path(tmp_path: Path) -> Path:
    p = tmp_path / "sova" / "google_credentials.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    return p


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    _creds_cache.clear()
    yield  # type: ignore[misc]
    _creds_cache.clear()


def _make_creds(
    *,
    valid: bool = True,
    expired: bool = False,
    scopes: list[str] | None = None,
    refresh_token: str | None = "refresh-tok",
) -> MagicMock:
    creds = MagicMock()
    creds.valid = valid
    creds.expired = expired
    creds.refresh_token = refresh_token
    creds.scopes = scopes if scopes is not None else list(AWARENESS_GOOGLE_SCOPES)
    return creds


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,substring,suffix",
    [
        (DEFAULT_TOKEN_PATH, "sova", "google_token.pickle"),
        (DEFAULT_CREDENTIALS_PATH, "sova", "google_credentials.json"),
    ],
)
def test_config_paths_under_sova_dir(path: Path, substring: str, suffix: str) -> None:
    assert substring in str(path)
    assert str(path).endswith(suffix)


def test_scopes_include_gmail_and_calendar() -> None:
    scope_str = " ".join(AWARENESS_GOOGLE_SCOPES)
    assert "gmail.readonly" in scope_str
    assert "calendar.readonly" in scope_str


# ---------------------------------------------------------------------------
# Token loading and reuse
# ---------------------------------------------------------------------------


def test_loads_valid_cached_token(awareness_config: AwarenessConfig, token_path: Path, creds_path: Path) -> None:
    creds = _make_creds(valid=True)
    with (
        patch(f"{_MOD}._load_token", return_value=creds),
        patch(f"{_MOD}.InstalledAppFlow", MagicMock()),
    ):
        result = authenticate_google(awareness_config, token_path=token_path, credentials_path=creds_path)
    assert result is creds


def test_refreshes_expired_token(awareness_config: AwarenessConfig, token_path: Path, creds_path: Path) -> None:
    creds = _make_creds(valid=False, expired=True)
    with (
        patch(f"{_MOD}._load_token", return_value=creds),
        patch(f"{_MOD}.Request") as mock_req,
        patch(f"{_MOD}._save_token") as mock_save,
        patch(f"{_MOD}.InstalledAppFlow", MagicMock()),
    ):
        result = authenticate_google(awareness_config, token_path=token_path, credentials_path=creds_path)
    creds.refresh.assert_called_once_with(mock_req.return_value)
    mock_save.assert_called_once_with(creds, token_path)
    assert result is creds


def test_refresh_error_triggers_new_flow(awareness_config: AwarenessConfig, token_path: Path, creds_path: Path) -> None:
    creds = _make_creds(valid=False, expired=True)
    creds.refresh.side_effect = Exception("refresh revoked")

    new_creds = _make_creds(valid=True)
    mock_flow = MagicMock()
    mock_flow.run_local_server.return_value = new_creds

    with (
        patch(f"{_MOD}._load_token", return_value=creds),
        patch(f"{_MOD}.InstalledAppFlow") as mock_flow_cls,
        patch(f"{_MOD}._save_token"),
    ):
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow
        result = authenticate_google(awareness_config, token_path=token_path, credentials_path=creds_path)
    assert result is new_creds


# ---------------------------------------------------------------------------
# Scope mismatch
# ---------------------------------------------------------------------------


def test_scope_mismatch_triggers_new_flow(
    awareness_config: AwarenessConfig, token_path: Path, creds_path: Path
) -> None:
    creds = _make_creds(
        valid=True,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    new_creds = _make_creds(valid=True)
    mock_flow = MagicMock()
    mock_flow.run_local_server.return_value = new_creds

    with (
        patch(f"{_MOD}._load_token", return_value=creds),
        patch(f"{_MOD}.InstalledAppFlow") as mock_flow_cls,
        patch(f"{_MOD}._save_token"),
    ):
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow
        result = authenticate_google(awareness_config, token_path=token_path, credentials_path=creds_path)
    assert result is new_creds


# ---------------------------------------------------------------------------
# Missing credentials file
# ---------------------------------------------------------------------------


def test_missing_credentials_raises(awareness_config: AwarenessConfig, token_path: Path, tmp_path: Path) -> None:
    nonexistent = tmp_path / "no_such_file.json"
    with (
        patch(f"{_MOD}._load_token", return_value=None),
        patch(f"{_MOD}.InstalledAppFlow", MagicMock()),
    ):
        with pytest.raises(FileNotFoundError, match="google_credentials.json"):
            authenticate_google(
                awareness_config,
                token_path=token_path,
                credentials_path=nonexistent,
            )


# ---------------------------------------------------------------------------
# gmail_token_path override
# ---------------------------------------------------------------------------


def test_gmail_token_path_override(tmp_path: Path, creds_path: Path) -> None:
    override_path = tmp_path / "sova" / "shared_token.pickle"
    creds = _make_creds(valid=True)
    config = AwarenessConfig(gmail_token_path=str(override_path))

    with (
        patch(f"{_MOD}._load_token", return_value=creds) as mock_load,
        patch(f"{_MOD}.InstalledAppFlow", MagicMock()),
    ):
        result = authenticate_google(config, credentials_path=creds_path)

    mock_load.assert_called_once_with(override_path)
    assert result is creds


# ---------------------------------------------------------------------------
# New flow (no existing token)
# ---------------------------------------------------------------------------


def test_new_flow_when_no_token(awareness_config: AwarenessConfig, token_path: Path, creds_path: Path) -> None:
    new_creds = _make_creds(valid=True)
    mock_flow = MagicMock()
    mock_flow.run_local_server.return_value = new_creds

    with (
        patch(f"{_MOD}._load_token", return_value=None),
        patch(f"{_MOD}.InstalledAppFlow") as mock_flow_cls,
        patch(f"{_MOD}._save_token") as mock_save,
    ):
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow
        result = authenticate_google(awareness_config, token_path=token_path, credentials_path=creds_path)

    assert result is new_creds
    mock_flow_cls.from_client_secrets_file.assert_called_once_with(str(creds_path), AWARENESS_GOOGLE_SCOPES)
    mock_save.assert_called_once_with(new_creds, token_path)


# ---------------------------------------------------------------------------
# Token directory auto-creation (_save_token)
# ---------------------------------------------------------------------------


def test_save_token_creates_directory(tmp_path: Path) -> None:
    from sova.awareness.auth.google_oauth import _save_token

    deep_path = tmp_path / "sova" / "b" / "token.pickle"
    obj = {"scopes": ["test"]}
    with patch(f"{_MOD}._ALLOWED_ROOTS", frozenset({tmp_path / "sova"})):
        _save_token(obj, deep_path)

    assert deep_path.parent.exists()
    loaded = pickle.loads(deep_path.read_bytes())
    assert loaded == obj


# ---------------------------------------------------------------------------
# Token loading edge cases (_load_token)
# ---------------------------------------------------------------------------


def test_load_token_returns_none_for_missing(tmp_path: Path) -> None:
    from sova.awareness.auth.google_oauth import _load_token

    assert _load_token(tmp_path / "nope.pickle") is None


def test_load_token_returns_none_for_corrupted(tmp_path: Path) -> None:
    from sova.awareness.auth.google_oauth import _load_token

    bad = tmp_path / "sova" / "bad.pickle"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not-a-pickle")
    with patch(f"{_MOD}._ALLOWED_ROOTS", frozenset({tmp_path / "sova"})):
        assert _load_token(bad) is None


# ---------------------------------------------------------------------------
# has_required_scopes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scopes,expected",
    [
        (list(AWARENESS_GOOGLE_SCOPES), True),
        (["https://www.googleapis.com/auth/gmail.readonly"], False),
        (None, False),
    ],
)
def test_has_required_scopes(scopes: list[str] | None, expected: bool) -> None:
    from sova.awareness.auth.google_oauth import _has_required_scopes

    if scopes is None:
        creds = MagicMock(spec=[])
    else:
        creds = _make_creds(scopes=scopes)
    assert _has_required_scopes(creds) is expected


# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------


def test_raises_when_google_libs_missing(awareness_config: AwarenessConfig, token_path: Path, creds_path: Path) -> None:
    with patch(f"{_MOD}.InstalledAppFlow", None):
        with pytest.raises(ImportError, match="Google auth libraries"):
            authenticate_google(
                awareness_config,
                token_path=token_path,
                credentials_path=creds_path,
            )


# ---------------------------------------------------------------------------
# Path validation (Finding #2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "func_name,args",
    [
        ("_load_token", (Path("/etc/passwd"),)),
        ("_save_token", ({"test": True}, Path("/tmp/evil.pickle"))),
    ],
)
def test_path_validation_rejects_outside_allowed(func_name: str, args: tuple) -> None:
    from sova.awareness.auth.google_oauth import _load_token, _save_token

    func = _load_token if func_name == "_load_token" else _save_token
    if func_name == "_load_token":
        fake_exists = patch.object(Path, "exists", return_value=True)
        with fake_exists, pytest.raises(ValueError, match="outside allowed"):
            func(*args)
    else:
        with pytest.raises(ValueError, match="outside allowed"):
            func(*args)


def test_path_validation_accepts_sova_config_dir() -> None:
    from sova.awareness.auth.google_oauth import _validate_path

    _validate_path(Path.home() / ".config" / "sova" / "token.pickle")


# ---------------------------------------------------------------------------
# In-memory caching (Finding #3)
# ---------------------------------------------------------------------------


def test_concurrent_calls_use_cache(awareness_config: AwarenessConfig, token_path: Path, creds_path: Path) -> None:
    creds = _make_creds(valid=True)
    call_count = 0

    def counting_load(path: Path) -> object:
        nonlocal call_count
        call_count += 1
        return creds

    with (
        patch(f"{_MOD}._load_token", side_effect=counting_load),
        patch(f"{_MOD}.InstalledAppFlow", MagicMock()),
    ):
        r1 = authenticate_google(awareness_config, token_path=token_path, credentials_path=creds_path)
        r2 = authenticate_google(awareness_config, token_path=token_path, credentials_path=creds_path)

    assert r1 is creds
    assert r2 is creds
    assert call_count == 1


def test_cache_expires_after_ttl(awareness_config: AwarenessConfig, token_path: Path, creds_path: Path) -> None:
    creds = _make_creds(valid=True)

    with (
        patch(f"{_MOD}._load_token", return_value=creds),
        patch(f"{_MOD}.InstalledAppFlow", MagicMock()),
    ):
        authenticate_google(awareness_config, token_path=token_path, credentials_path=creds_path)

    resolved = token_path.resolve()
    assert resolved in _creds_cache
    entry_creds, _ = _creds_cache[resolved]
    _creds_cache[resolved] = (entry_creds, time.monotonic() - _CACHE_TTL_SECONDS - 1)

    with (
        patch(f"{_MOD}._load_token", return_value=creds) as mock_load,
        patch(f"{_MOD}.InstalledAppFlow", MagicMock()),
    ):
        authenticate_google(awareness_config, token_path=token_path, credentials_path=creds_path)
    mock_load.assert_called_once()


# ---------------------------------------------------------------------------
# Save token error handling (Finding #4)
# ---------------------------------------------------------------------------


def test_save_token_propagates_os_error(tmp_path: Path) -> None:
    from sova.awareness.auth.google_oauth import _save_token

    ro_path = tmp_path / "sova" / "token.pickle"
    # Mock the tempfile context manager to raise OSError during file operations
    with (
        patch(f"{_MOD}._validate_path"),
        patch(f"{_MOD}.tempfile.NamedTemporaryFile") as mock_tmpfile,
    ):
        mock_tmpfile.return_value.__enter__.return_value.name = str(ro_path)
        mock_tmpfile.return_value.__enter__.return_value.write.side_effect = OSError("disk full")
        with pytest.raises(OSError, match="disk full"):
            _save_token({"test": True}, ro_path)


# ---------------------------------------------------------------------------
# OAuth flow error handling (Finding #5)
# ---------------------------------------------------------------------------


def test_oauth_flow_user_denies_consent(awareness_config: AwarenessConfig, token_path: Path, creds_path: Path) -> None:
    mock_flow = MagicMock()
    mock_flow.run_local_server.side_effect = OSError("port conflict")

    with (
        patch(f"{_MOD}._load_token", return_value=None),
        patch(f"{_MOD}.InstalledAppFlow") as mock_cls,
    ):
        mock_cls.from_client_secrets_file.return_value = mock_flow
        with pytest.raises(OSError, match="OAuth local server failed"):
            authenticate_google(awareness_config, token_path=token_path, credentials_path=creds_path)


def test_malformed_credentials_json(awareness_config: AwarenessConfig, token_path: Path, creds_path: Path) -> None:
    with (
        patch(f"{_MOD}._load_token", return_value=None),
        patch(f"{_MOD}.InstalledAppFlow") as mock_cls,
    ):
        mock_cls.from_client_secrets_file.side_effect = ValueError("bad JSON")
        with pytest.raises(ValueError, match="Malformed credentials file"):
            authenticate_google(awareness_config, token_path=token_path, credentials_path=creds_path)


# ---------------------------------------------------------------------------
# File permissions (CodeRabbit finding)
# ---------------------------------------------------------------------------


def test_save_token_with_restricted_permissions(tmp_path: Path) -> None:
    """Verify token file and directory have restricted permissions (0o600 file, 0o700 dir)."""
    from sova.awareness.auth.google_oauth import _save_token

    token_path = tmp_path / "sova" / "token.pickle"
    obj = {"scopes": ["test"]}

    with patch(f"{_MOD}._ALLOWED_ROOTS", frozenset({tmp_path / "sova"})):
        _save_token(obj, token_path)

    # Check directory permissions: 0o700 (rwx------)
    dir_mode = token_path.parent.stat().st_mode & 0o777
    assert dir_mode == 0o700, f"Expected dir mode 0o700, got {oct(dir_mode)}"

    # Check file permissions: 0o600 (rw-------)
    file_mode = token_path.stat().st_mode & 0o777
    assert file_mode == 0o600, f"Expected file mode 0o600, got {oct(file_mode)}"

    # Verify the token was saved correctly
    loaded = pickle.loads(token_path.read_bytes())
    assert loaded == obj


def test_save_token_fixes_existing_file_permissions(tmp_path: Path) -> None:
    """Verify existing token files get chmod to 0o600 even if they have broader permissions."""
    from sova.awareness.auth.google_oauth import _save_token

    token_path = tmp_path / "sova" / "token.pickle"
    token_path.parent.mkdir(parents=True, exist_ok=True)

    # Create a file with loose permissions (simulating old behavior)
    token_path.write_bytes(b"old token")
    token_path.chmod(0o644)

    obj = {"scopes": ["test"]}
    with patch(f"{_MOD}._ALLOWED_ROOTS", frozenset({tmp_path / "sova"})):
        _save_token(obj, token_path)

    # Verify permissions were fixed to 0o600
    file_mode = token_path.stat().st_mode & 0o777
    assert file_mode == 0o600, f"Expected file mode 0o600, got {oct(file_mode)}"
