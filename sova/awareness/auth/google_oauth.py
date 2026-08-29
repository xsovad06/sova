"""Google OAuth2 authentication for awareness providers (Gmail, Calendar)."""

from __future__ import annotations

import pickle
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

    from sova.config.models import AwarenessConfig

try:
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    Request = None  # type: ignore[assignment,misc]
    InstalledAppFlow = None  # type: ignore[assignment,misc]

_log = get_logger(component="awareness.auth")

AWARENESS_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

_CONFIG_DIR = Path.home() / ".config" / "sova"
DEFAULT_TOKEN_PATH = _CONFIG_DIR / "google_token.pickle"
DEFAULT_CREDENTIALS_PATH = _CONFIG_DIR / "google_credentials.json"

_ALLOWED_ROOTS = frozenset({Path.home() / ".config" / "sova"})
_CACHE_TTL_SECONDS = 60
_creds_cache: dict[Path, tuple[object, float]] = {}

_GCP_SETUP_GUIDE = """\
Google OAuth credentials not found.

To set up Google API access for SOVA:

1. Go to https://console.cloud.google.com/
2. Create a new project (or select an existing one)
3. Enable the Gmail API and Google Calendar API:
   - APIs & Services > Library > search "Gmail API" > Enable
   - APIs & Services > Library > search "Google Calendar API" > Enable
4. Create OAuth Desktop credentials:
   - APIs & Services > Credentials > Create Credentials > OAuth client ID
   - Application type: Desktop app
   - Download the JSON file
5. Save the JSON file to: {credentials_path}
6. Run `sova briefing` to complete the OAuth flow (opens browser for consent)

For persistent tokens, publish your OAuth consent screen:
  - Google Workspace: set to "Internal"
  - Personal Gmail: set to "External" and add your email as a test user
  - Apps in "Testing" mode issue tokens that expire after 7 days
"""


def _validate_path(path: Path) -> None:
    """Ensure path is under an allowed root directory."""
    resolved = path.resolve()
    if not any(resolved == root or root in resolved.parents for root in _ALLOWED_ROOTS):
        allowed = ", ".join(str(r) for r in _ALLOWED_ROOTS)
        raise ValueError(f"Token path {resolved} is outside allowed directories ({allowed})")


def _resolve_token_path(config: AwarenessConfig, token_path: Path | None) -> Path:
    """Resolve the token path from explicit arg, config, or default."""
    if token_path is not None:
        return token_path
    if config.gmail_token_path:
        return Path(config.gmail_token_path)
    return DEFAULT_TOKEN_PATH


def _has_required_scopes(creds: object) -> bool:
    """Check if credentials include all required Gmail and Calendar scopes."""
    existing = set(getattr(creds, "scopes", None) or [])
    return all(s in existing for s in AWARENESS_GOOGLE_SCOPES)


def _load_token(path: Path) -> object | None:
    """Load pickled credentials from disk. Returns None if missing or corrupted."""
    if not path.exists():
        return None
    _validate_path(path)
    try:
        with path.open("rb") as f:
            return pickle.load(f)  # noqa: S301
    except (pickle.UnpicklingError, EOFError, OSError):
        _log.warning("token_load_failed", path=str(path))
        return None


def _save_token(creds: object, path: Path) -> None:
    """Save credentials to disk with restricted permissions (0o600 file, 0o700 dir)."""
    _validate_path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Ensure existing token files have restricted permissions
        if path.exists():
            path.chmod(0o600)
        # Write to temp file first, then atomically replace
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            pickle.dump(creds, tmp)
        tmp_path.chmod(0o600)
        tmp_path.replace(path)
        _log.info("token_saved", path=str(path))
    except OSError:
        _log.error("token_save_failed", path=str(path), exc_info=True)
        raise


def _get_cached_creds(path: Path) -> object | None:
    """Return cached credentials if they exist and have not expired (TTL 60s)."""
    resolved = path.resolve()
    if resolved in _creds_cache:
        creds, loaded_at = _creds_cache[resolved]
        if time.monotonic() - loaded_at < _CACHE_TTL_SECONDS:
            return creds
        del _creds_cache[resolved]
    return None


def _set_cached_creds(path: Path, creds: object) -> None:
    """Cache credentials with a timestamp for TTL-based expiration."""
    _creds_cache[path.resolve()] = (creds, time.monotonic())


def _cache_and_return_creds(creds: object, token_path: Path) -> Credentials:
    """Cache credentials and return them."""
    _set_cached_creds(token_path, creds)
    return creds  # type: ignore[return-value]


def _is_valid_with_scopes(creds: object | None) -> bool:
    """Check if credentials are valid and have required scopes."""
    return creds is not None and _has_required_scopes(creds) and getattr(creds, "valid", False)


def _try_refresh_expired_token(creds: object, token_path: Path) -> Credentials | None:
    """Attempt to refresh an expired token. Returns None on failure (logs with context)."""
    if not (getattr(creds, "expired", False) and getattr(creds, "refresh_token", None)):
        return None
    try:
        creds.refresh(Request())
        _save_token(creds, token_path)
        _log.info("token_refreshed", path=str(token_path))
        return _cache_and_return_creds(creds, token_path)
    except Exception:
        _log.warning("token_refresh_failed", path=str(token_path), exc_info=True)
        return None


def _run_oauth_flow(credentials_path: Path, token_path: Path) -> Credentials:
    """Run the OAuth consent flow via browser and save the resulting token."""
    if not credentials_path.exists():
        guide = _GCP_SETUP_GUIDE.format(credentials_path=credentials_path)
        _log.error("credentials_missing", path=str(credentials_path))
        raise FileNotFoundError(f"google_credentials.json not found at {credentials_path}\n\n{guide}")

    _log.info("starting_oauth_flow", credentials=str(credentials_path))
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), AWARENESS_GOOGLE_SCOPES)
    except (ValueError, KeyError) as exc:
        _log.error("credentials_malformed", path=str(credentials_path), exc_info=True)
        raise ValueError(f"Malformed credentials file at {credentials_path}: {exc}") from exc

    try:
        creds = flow.run_local_server(port=0)
    except OSError as exc:
        _log.error("oauth_server_failed", exc_info=True)
        raise OSError(f"OAuth local server failed (port conflict or network error): {exc}") from exc

    _save_token(creds, token_path)
    _set_cached_creds(token_path, creds)
    return creds  # type: ignore[return-value]


def authenticate_google(
    config: AwarenessConfig,
    *,
    token_path: Path | None = None,
    credentials_path: Path | None = None,
) -> Credentials:
    """Authenticate with Google APIs and return a Credentials object.

    Checks for an existing token, refreshes if expired, or runs the
    browser-based OAuth consent flow when no valid token is available.

    Raises FileNotFoundError if the credentials JSON is missing.
    Raises ValueError if a token path is outside allowed directories.
    """
    if InstalledAppFlow is None:
        raise ImportError("Google auth libraries not installed. Install with: pip install sova[awareness]")

    resolved_token = _resolve_token_path(config, token_path)
    resolved_creds = credentials_path or DEFAULT_CREDENTIALS_PATH

    cached = _get_cached_creds(resolved_token)
    if _is_valid_with_scopes(cached):
        _log.info("token_reused_from_cache", path=str(resolved_token))
        return _cache_and_return_creds(cached, resolved_token)

    creds = _load_token(resolved_token)
    if _is_valid_with_scopes(creds):
        _log.info("token_reused", path=str(resolved_token))
        return _cache_and_return_creds(creds, resolved_token)

    if creds is not None and _has_required_scopes(creds):
        refreshed = _try_refresh_expired_token(creds, resolved_token)
        if refreshed is not None:
            return refreshed

    return _run_oauth_flow(resolved_creds, resolved_token)
