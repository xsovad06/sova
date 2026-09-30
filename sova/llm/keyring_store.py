"""Optional OS-keyring-backed storage for secret settings.

Wraps the optional ``keyring`` package behind an import guard, mirroring the
Headroom compression pattern in ``sova/llm/compression.py``. When the package
is not installed, or no usable backend exists (e.g. headless Linux with no
Secret Service, where ``keyring.get_keyring()`` returns the null
``keyring.backends.fail.Keyring``), every function degrades gracefully and
the caller falls back to plaintext database storage. No exception escapes
this module.

Only settings named in ``RESOLVED_SECRET_KEYS`` are actually moved into the
keyring by the settings layer (``sova/dashboard/services/settings_service.py``):
every ``value_type="secret"`` setting is masked on display, but moving a
setting's storage here is only safe once its one read site calls
``resolve_secret()`` instead of reading the config field directly (see
``sova/llm/provider.py:create_provider``'s anthropic branch for the only
current example). Adding a new name to this set without updating its
consumption site would silently replace a working credential with the
literal ``SENTINEL`` string.
"""

from __future__ import annotations

from sova.utils.logging import get_logger

try:
    import keyring
    from keyring.backends.fail import Keyring as _FailKeyring
    from keyring.errors import PasswordDeleteError

    _KEYRING_IMPORTABLE = True
except ImportError:
    keyring = None  # type: ignore[assignment]
    _FailKeyring = None
    PasswordDeleteError = Exception  # unreachable (guarded by is_keyring_available()) but keeps except clauses valid
    _KEYRING_IMPORTABLE = False

log = get_logger()

SERVICE_NAME = "sova"

# A non-secret marker stored in project_settings once a value has been moved
# into the OS keyring. Contains a colon so it can never collide with a real
# API key, token, or password a user might type.
SENTINEL = "keyring:stored"

RESOLVED_SECRET_KEYS = frozenset({"llm.api_key"})

_backend_usable: bool | None = None


def is_keyring_available() -> bool:
    """Return True if ``keyring`` is importable and has a usable backend.

    Backend usability is probed lazily (``keyring.get_keyring()`` can be slow
    and can raise on a machine with no Secret Service/Keychain/Credential
    Manager) and cached for the life of the process, since availability does
    not change mid-run.
    """
    global _backend_usable  # noqa: PLW0603
    if not _KEYRING_IMPORTABLE:
        return False
    if _backend_usable is None:
        try:
            backend = keyring.get_keyring()
            _backend_usable = backend is not None and not isinstance(backend, _FailKeyring)
        except Exception:  # noqa: BLE001 (any probe failure means no usable backend)
            log.debug("keyring_store.probe_failed", exc_info=True)
            _backend_usable = False
    return _backend_usable


def get_secret(name: str) -> str | None:
    """Read a secret from the OS keyring.

    Returns None on any failure: package missing, no usable backend, a
    locked keychain, or a missing entry. Callers fall back to their own next
    source in that case.
    """
    if not is_keyring_available():
        return None
    try:
        return keyring.get_password(SERVICE_NAME, name)
    except Exception:  # noqa: BLE001 (keyring backends raise arbitrary errors; a read must never crash a caller)
        log.warning("keyring_store.get_failed", name=name, exc_info=True)
        return None


def set_secret(name: str, value: str) -> bool:
    """Write a secret to the OS keyring. Returns True on success."""
    if not is_keyring_available():
        return False
    try:
        keyring.set_password(SERVICE_NAME, name, value)
        return True
    except Exception:  # noqa: BLE001 (keyring backends raise arbitrary errors; a write must never crash a caller)
        log.warning("keyring_store.set_failed", name=name, exc_info=True)
        return False


def delete_secret(name: str) -> bool:
    """Delete a secret from the OS keyring.

    Returns True on success, including when the entry was already absent:
    deleting a secret that isn't there is not a failure.
    """
    if not is_keyring_available():
        return False
    try:
        keyring.delete_password(SERVICE_NAME, name)
        return True
    except PasswordDeleteError:
        return True
    except Exception:  # noqa: BLE001 (keyring backends raise arbitrary errors; a delete must never crash a caller)
        log.warning("keyring_store.delete_failed", name=name, exc_info=True)
        return False


def resolve_secret(name: str, db_value: str | None) -> str:
    """Resolve a secret's live value: keyring first, then a plaintext database value.

    ``db_value`` is treated as absent when it equals ``SENTINEL`` (the value
    was moved to the keyring but the keychain entry is now gone: deleted from
    the keychain, or the database was copied to another machine without it).
    Returns "" when nothing resolves; callers apply their own further
    fallback (e.g. an environment variable) on top of that.
    """
    value = get_secret(name)
    if value:
        return value
    if db_value and db_value != SENTINEL:
        return db_value
    return ""


def migrate_plaintext_to_keyring(name: str, value: str) -> bool:
    """Write ``value`` to the keyring and verify it by reading it back.

    Returns True only when the round-trip succeeds, so a caller can safely
    replace its plaintext copy with ``SENTINEL``. A partial failure (write
    succeeds but read-back fails or disagrees) reports False so the caller
    leaves the plaintext copy in place rather than losing the value.
    """
    if not set_secret(name, value):
        return False
    return get_secret(name) == value
