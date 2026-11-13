"""Settings service: config viewing/editing, invariants, personas."""

from __future__ import annotations

import asyncio
import copy
import functools
import json
import os
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from sova.utils.files import read_text_or_none
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.commands.distribution import DiffResult, ReverseDiffResult
    from sova.config.models import ProjectConfig
    from sova.dashboard.settings_meta import SettingMeta

log = get_logger(component="dashboard.settings")

# One lock per project, serializing the validate-then-persist sequence in
# update_config() within this process. Without it, two concurrent saves (e.g.
# llm.provider="ollama" and llm.model="") can both validate against the same
# stale snapshot and both pass, leaving an unloadable combination persisted.
# This closes the race for the common case of one dashboard process serving
# concurrent requests; it does not provide cross-process serialization.
_update_locks: dict[str, asyncio.Lock] = {}


def _get_update_lock(project_dir: Path | None) -> asyncio.Lock:
    """Return the per-project lock guarding update_config's validate+persist sequence."""
    key = str((project_dir or Path.cwd()).resolve())
    lock = _update_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _update_locks[key] = lock
    return lock


def get_config(project_dir: Path | None = None, *, raw: dict | None = None) -> dict:
    """Load project config as a flat dict for the settings page.

    Every ``value_type="secret"`` setting (per ``settings_meta``) is replaced
    with a fixed-length mask placeholder when set: the real value must never
    reach the settings API response, even masked client-side, since that is
    still exposure. Callers that need the real value (secret migration,
    resolving a live credential) use ``_get_raw_config()`` directly instead.

    *raw* lets a caller that already loaded ``_get_raw_config()`` (e.g. to
    also call ``get_secret_locations()``) pass it in and avoid a second
    ``load_config()`` round trip; by default it is loaded here.
    """
    from sova.dashboard.settings_meta import SECRET_KEYS

    result = dict(raw) if raw is not None else _get_raw_config(project_dir)
    if "_error" in result:
        return result

    for key in SECRET_KEYS:
        if result.get(key):
            result[key] = _SECRET_MASK_PLACEHOLDER
    return result


def _get_raw_config(project_dir: Path | None = None) -> dict:
    """Load project config as a flat dict, secrets included in plaintext.

    Internal helper. Anything reaching the settings API or another external
    surface must go through ``get_config()`` instead, which masks every
    ``value_type="secret"`` key.
    """
    from sova.config.loader import load_config

    try:
        cfg = load_config(project_dir)
    except Exception:  # noqa: BLE001 (config may fail for many reasons (missing file, bad TOML, import errors))
        log.warning("settings.config_load_failed", project_dir=str(project_dir), exc_info=True)
        return {"_error": "No configuration found"}

    # Flatten the config into displayable key-value pairs
    result: dict = {}
    _flatten_dict("", cfg.model_dump(), result)
    return result


def get_secret_locations(project_dir: Path | None = None, *, raw: dict | None = None) -> dict[str, str]:
    """Return ``{key: "keyring"|"database"|"unset"}`` for every secret setting.

    Read-only/diagnostic: drives the settings page's "Move to keychain"
    action. Never exposes the underlying value.

    *raw* lets a caller that already loaded ``_get_raw_config()`` pass it in
    and avoid a second ``load_config()`` round trip; by default it is loaded
    here.
    """
    from sova.dashboard.settings_meta import SECRET_KEYS
    from sova.llm import keyring_store

    if raw is None:
        raw = _get_raw_config(project_dir)
    if "_error" in raw:
        return {}

    locations: dict[str, str] = {}
    for key in SECRET_KEYS:
        value = raw.get(key)
        if value == keyring_store.SENTINEL:
            locations[key] = "keyring"
        elif value:
            locations[key] = "database"
        else:
            locations[key] = "unset"
    return locations


def _default_instance(model: type[BaseModel]) -> BaseModel:
    """A config model holding only its declared defaults, built without reading the environment.

    ``model_validate()`` and the constructor of a settings section read ``SOVA_*`` variables, and pydantic-settings
    deep-merges a dict-valued one (``SOVA_ROLES_NICKNAMES``, ``SOVA_LLM_ROUTING``) into an explicit value, so
    either would report an environment override as the default. ``model_construct()`` reads nothing, but it would
    still build a nested section through its default factory (the constructor), so nested sections are built here
    first, and the model's "after" validators, which derive a default from other fields (the CodeRabbit quota
    fills ``reviews_per_hour`` from its plan), are run by hand.
    """
    nested = {}
    for name, field_info in model.model_fields.items():
        factory = field_info.default_factory
        if isinstance(factory, type) and issubclass(factory, BaseModel):
            nested[name] = _default_instance(factory)
    instance = model.model_construct(**nested)
    for decorator in model.__pydantic_decorators__.model_validators.values():
        if decorator.info.mode == "after":
            instance = decorator.func(instance)
    return instance


@functools.cache
def _default_values() -> dict[str, Any]:
    from sova.config.models import ProjectConfig

    flat: dict[str, Any] = {}
    _flatten_dict("", _default_instance(ProjectConfig).model_dump(), flat)
    return flat


def default_values() -> dict[str, Any]:
    """Every setting's default, as the flat ``{dotted key: value}`` dict ``get_config()`` returns for a project."""
    return copy.deepcopy(_default_values())


def _setting_for_row(row_key: str) -> str | None:
    """The registered setting a stored row belongs to, or None when no setting reads it.

    A row is its own setting, or one entry of an object-valued setting stored a row per entry
    (``llm.routing.develop`` belongs to ``llm.routing``).
    """
    from sova.dashboard.settings_meta import _META_BY_KEY

    if row_key in _META_BY_KEY:
        return row_key
    parts = row_key.split(".")
    for end in range(len(parts) - 1, 0, -1):
        meta = _META_BY_KEY.get(".".join(parts[:end]))
        if meta is not None:
            return meta.key if meta.value_type == "object" else None
    return None


def _model_class(annotation: Any) -> type[BaseModel] | None:
    """The config model an annotation names directly (a section, or an optional one), else None.

    A ``dict[str, Model]`` or ``list[Model]`` is not a section: its entries are the rest of the dotted path.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        for arg in typing.get_args(annotation):
            found = _model_class(arg)
            if found is not None:
                return found
    return None


def config_reads_row(row_key: str) -> bool:
    """True when the dotted path *row_key* resolves to a field ``ProjectConfig`` reads.

    The settings registry (``SettingMeta``) is a subset of the config: a stored row for a field that has no
    registry entry is still read by ``load_config()``, so it is not a leftover and must never be offered for
    removal. The walk descends sections via ``model_fields``; a field that is not a section (a leaf, or a dict
    whose entries are the remaining parts) ends it.
    """
    from sova.config.models import ProjectConfig

    model: type[BaseModel] = ProjectConfig
    for part in row_key.split("."):
        field_info = model.model_fields.get(part)
        if field_info is None:
            return False
        nested = _model_class(field_info.annotation)
        if nested is None:
            return True
        model = nested
    return True


_SECRET_LOOKING_KEY_PARTS = ("token", "secret", "password", "api_key", "apikey")


def get_grouped_settings(project_dir: Path | None = None) -> dict:
    """The settings page payload: every setting grouped, with its default and where its value comes from.

    Each setting carries, besides ``get_grouped_config()``'s fields, ``default`` (from a default config, never
    the environment), ``stored`` (a value is stored for this project; for an object-valued setting, any of its
    rows, counted in ``stored_rows``) and ``env_var`` (the ``SOVA_`` variable overriding it, or ""). ``leftover``
    lists stored rows no setting reads any more (neither the registry nor ``ProjectConfig``), with every text,
    list or mapping value masked. Secret values stay masked as ``get_config()`` masks them.
    ``stored_unreadable`` is True when the stored rows could not be read, so no ``stored`` flag is trustworthy.
    """
    from sova.config.db_loader import read_stored_settings
    from sova.config.loader import env_override_var
    from sova.dashboard.settings_meta import get_grouped_config

    raw = _get_raw_config(project_dir)
    if "_error" in raw:
        return {"groups": [], "leftover": [], "stored_unreadable": False, "error": raw["_error"]}
    flat = get_config(project_dir, raw=raw)
    groups = get_grouped_config(flat, secret_locations=get_secret_locations(project_dir, raw=raw))

    stored = read_stored_settings(project_dir)
    rows_per_setting: dict[str, int] = {}
    leftover: list[dict] = []
    for row_key, value in (stored or {}).items():
        owner = _setting_for_row(row_key)
        if owner is not None:
            rows_per_setting[owner] = rows_per_setting.get(owner, 0) + 1
            continue
        if config_reads_row(row_key):
            continue  # read by load_config() though the registry has no entry for it: live, not a leftover
        # No registered setting describes this row, so nothing says whether it held a credential: only a
        # flag, a number or an empty value is sent as it is, and any text, list or mapping is masked.
        if not isinstance(value, bool | int | float | None):
            value = _SECRET_MASK_PLACEHOLDER
        leftover.append({"key": row_key, "value": value})

    defaults = _default_values()
    environ = {name.upper(): value for name, value in os.environ.items()}
    for group in groups:
        for setting in group["settings"]:
            key = setting["key"]
            setting["default"] = copy.deepcopy(defaults.get(key))
            setting["stored"] = rows_per_setting.get(key, 0) > 0
            if setting.get("value_type") == "object":
                setting["stored_rows"] = rows_per_setting.get(key, 0)
            setting["env_var"] = env_override_var(key, environ) or ""
    return {"groups": groups, "leftover": leftover, "stored_unreadable": stored is None}


def _reset_refusal(project_dir: Path | None, key: str, meta: SettingMeta | None) -> dict | None:
    """Why *key* cannot be reset, as an error result, or None when it can.

    A key with no setting can be reset only as a leftover row (stored, not an entry of an object-valued
    setting, and not a field the config loader reads); a setting a ``SOVA_`` variable overrides cannot be reset
    here at all.
    """
    from sova.config.db_loader import read_stored_settings
    from sova.config.loader import env_override_var

    if meta is None:
        stored = read_stored_settings(project_dir)
        if stored is None:
            # Not "nothing stored": the rows could not be read, so nothing can be said about this key.
            return _unreadable_database_error(project_dir) or {
                "error": "The stored values could not be read",
                "unavailable": True,
            }
        if key not in stored or _setting_for_row(key) is not None or config_reads_row(key):
            return {"error": f"Unknown setting: '{key}'", "unknown": True}
        return None
    env_var = env_override_var(key)
    if env_var:
        return {"error": f"'{key}' is set by {env_var} in the server environment. Change it there.", "env_var": env_var}
    return None


def _has_keychain_entry(key: str, meta: SettingMeta | None) -> bool:
    """True when *key* is a secret whose value may live in the OS keychain."""
    from sova.llm import keyring_store

    return meta is not None and meta.value_type == "secret" and key in keyring_store.RESOLVED_SECRET_KEYS


async def _delete_stored_value(project_dir: Path | None, key: str, meta: SettingMeta | None) -> tuple[int, str | None]:
    """Delete *key*'s stored rows and, for a keyring-backed secret, its keychain entry.

    Returns ``(rows deleted, warning)``. The row goes first and is committed before the keychain is touched:
    a database failure then leaves the credential intact, while a keychain entry that could not be confirmed
    removed only leaves an orphan, which is reported as the warning rather than treated as a failure.
    """
    from sova.config.db_loader import delete_setting
    from sova.db.session import get_session
    from sova.llm import keyring_store

    async with await get_session(project_dir=project_dir) as session, session.begin():
        deleted = await delete_setting(session, key, with_children=meta is not None and meta.value_type == "object")
    if not _has_keychain_entry(key, meta):
        return deleted, None
    provider = _keyring_scope(project_dir, key, "")
    if not provider:
        return deleted, "Could not determine the active LLM provider; the keychain entry was not removed"
    resolved_dir = project_dir if project_dir is not None else Path.cwd()
    scoped_name = keyring_store.scoped_secret_name(key, resolved_dir, provider)
    legacy_owned = keyring_store.fixed_scope(key) is None and provider == _active_llm_provider(project_dir)
    return deleted, _clear_keychain_entries(key, scoped_name, legacy_owned)


async def reset_config(project_dir: Path | None = None, *, key: str) -> dict:
    """Delete the value stored for *key*, so its default applies again.

    Removes the database row (every row under an object-valued setting), a keychain entry behind a
    keyring-backed secret, and the key in ``sova.toml``, which would otherwise apply again on the next load. A
    stored row no setting reads (a leftover) can be removed the same way. Refused when a ``SOVA_`` variable
    overrides the setting (``env_var`` names it), when the default would make the project config unloadable,
    and for a key that is neither a setting nor stored (``unknown``). Returns ``{"status": "ok", "key",
    "value", "deleted"}`` with the value now in effect, plus ``warning`` when a keychain entry could not be
    removed or the value in effect still differs from the default, or ``{"error": ...}``.
    """
    from sova.dashboard.settings_meta import _META_BY_KEY

    meta = _META_BY_KEY.get(key)
    refusal = _reset_refusal(project_dir, key, meta) or _unreadable_database_error(project_dir)
    if refusal:
        return refusal
    toml_cleared = True

    async with _get_update_lock(project_dir):
        if meta is not None:
            consistency_error = _validate_config_consistency(project_dir, key, _default_values().get(key))
            if consistency_error:
                return {"error": f"Not reset: {consistency_error}"}
        try:
            deleted, warning = await _delete_stored_value(project_dir, key, meta)
        except (OSError, RuntimeError, SQLAlchemyError):
            log.warning("settings.reset_failed", key=key, exc_info=True)
            return {"error": "Failed to remove the stored value (DB unavailable)", "unavailable": True}
        toml_cleared = _remove_setting_from_toml(project_dir, key)
        _invalidate_config_cache(project_dir)

    result: dict[str, Any] = {"status": "ok", "key": key, "value": None, "deleted": deleted}
    if meta is not None:
        result["value"] = get_config(project_dir).get(key)
        if meta.value_type != "secret" and result["value"] != _default_values().get(key):
            reason = (
                "it could not be removed from sova.toml, which may still set it"
                if not toml_cleared
                else "another config layer still sets it"
            )
            warning = warning or f"The stored value was removed, but '{key}' is still {result['value']!r}: {reason}."
    if warning:
        result["warning"] = warning
    return result


def _remove_setting_from_toml(project_dir: Path | None, key: str) -> bool:
    """Best-effort removal of *key* from sova.toml, the counterpart of ``_save_setting_to_toml()``.

    Returns False only when the key may still be there because the removal failed; True when it was removed or
    was never in the file.
    """
    toml_path = get_config_file_path(project_dir)
    if not toml_path.exists():
        return True
    try:
        import tomlkit

        doc = tomlkit.parse(toml_path.read_text(encoding="utf-8"))
        # A top-level field may sit at the root or in the [project] table, which the loader maps onto the root.
        parts = key.split(".")
        if len(parts) == 1 and key not in doc and key in doc.get("project", {}):
            parts = ["project", key]
        target = doc
        for part in parts[:-1]:
            if part not in target:
                return True
            target = target[part]
        if parts[-1] not in target:
            return True
        del target[parts[-1]]
        tmp_path = toml_path.with_suffix(".toml.tmp")
        tmp_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
        tmp_path.replace(toml_path)
    except ImportError:
        log.debug("tomlkit not available")
        return False
    except Exception:  # noqa: BLE001 (tomlkit surfaces arbitrary parse errors; the stored row is already gone)
        log.warning("settings.toml_remove_failed", key=key, exc_info=True)
        return False
    return True


def _flatten_dict(prefix: str, obj: dict, result: dict, registered: frozenset[str] | None = None) -> None:
    """Recursively flatten a nested dict into dotted keys.

    Stops recursing when ``full_key`` is a registered setting leaf (e.g.
    ``triage.labels``, ``roles.nicknames``) so object-valued settings stay
    intact.  Only intermediate containers (e.g. ``external_reviews.sonarcloud``)
    are expanded further.
    """
    if registered is None:
        from sova.dashboard.settings_meta import _META_BY_KEY

        registered = frozenset(_META_BY_KEY)

    for key, value in obj.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and full_key not in registered:
            _flatten_dict(full_key, value, result, registered)
        else:
            result[full_key] = value


def get_config_file_path(project_dir: Path | None = None) -> Path:
    """Get the path to sova.toml for this project."""
    if project_dir is None:
        project_dir = Path.cwd()
    return project_dir / "sova.toml"


async def update_config(project_dir: Path | None = None, *, key: str, value: str, provider: str = "") -> dict:
    """Update a single config key in the DB and sova.toml.

    Writes to the DB first (authoritative store read by load_config),
    then best-effort updates sova.toml for human readability.
    Only registered settings (present in settings_meta) can be updated.

    *provider* scopes a keyring-backed secret write to the provider the key
    is *for* (e.g. the Connections page's per-provider card), as opposed to
    whichever provider happens to be active right now. Ignored for
    non-secret keys. Empty (the settings-page edit of ``llm.api_key`` with
    no per-card context) falls back to the currently-active provider.
    """
    from sova.dashboard.settings_meta import _META_BY_KEY

    meta = _META_BY_KEY.get(key)
    if meta is None:
        return {"error": f"Unknown setting: '{key}'"}

    if meta.value_type == "secret" and _is_masked_secret(value):
        # The UI submitted the masked placeholder unchanged; do not overwrite
        # the stored key with the mask string.
        return {"status": "ok", "key": key, "value": value, "unchanged": True}

    validation_error = _validate_value_type(key, value)
    if validation_error:
        return {"error": validation_error}

    cast = _cast_value(value, meta.value_type, meta.item_type)

    unreadable = _unreadable_database_error(project_dir)
    if unreadable:
        return unreadable

    async with _get_update_lock(project_dir):
        consistency_error = _validate_config_consistency(project_dir, key, cast)
        if consistency_error:
            return {"error": consistency_error}

        # Secrets are DB-only: never written to sova.toml in plaintext.
        if meta.value_type == "secret":
            db_ok, warning = await _save_secret(project_dir, key, str(cast), provider=provider)
            if not db_ok:
                return {"error": warning or "Failed to persist setting (DB unavailable)"}
            # The submitted secret must never be echoed back verbatim in the
            # response body (devtools/HAR captures, reverse-proxy access
            # logs): mask it, mirroring the unchanged-mask branch above. An
            # empty value (a deliberate clear) stays empty rather than
            # being masked, since masking it would misleadingly imply a key
            # is still set.
            result = {"status": "ok", "key": key, "value": _SECRET_MASK_PLACEHOLDER if value else ""}
            if warning:
                result["warning"] = warning
            return result

        db_ok = await _save_setting_to_db(project_dir, key, cast)
        if not db_ok:
            log.warning("settings.db_write_failed", key=key)

    toml_ok = _save_setting_to_toml(project_dir, key, cast)
    if not toml_ok:
        log.debug("settings.toml_write_skipped", key=key)
    if not db_ok and not toml_ok:
        return {"error": "Failed to persist setting (neither DB nor TOML available)"}

    return {"status": "ok", "key": key, "value": value}


async def update_config_many(project_dir: Path | None, updates: dict[str, str]) -> dict:
    """Persist several non-secret config keys in one all-or-nothing DB transaction.

    Unlike ``update_config()`` (one key validated and persisted at a time),
    this validates the *combined* resulting config once and writes every key
    inside a single DB transaction: a failure partway through rolls back
    every key in *updates*, so a provider activation (``llm.provider``,
    ``llm.model``, ``llm.api_base``) can never land as a partial mix of the
    old provider plus a new model (issue #1148). Validating per-key, as a
    loop of ``update_config()`` calls would, also rejects some valid combined
    states outright: switching to a model-required provider while also
    setting its model fails the per-key consistency check on whichever key
    lands first, which is why ``activate_llm_candidate`` previously had to
    pick a write order by hand.

    Secret keys are rejected: they route through ``_save_secret``'s
    keyring-aware path, which has no place in a bulk DB-only write.
    Returns ``{"status": "ok"}`` or ``{"error": ...}``; ``sova.toml`` is
    updated best-effort per key afterward, same as ``update_config()``: a
    TOML write failure never rolls back the authoritative DB transaction.
    """
    from sova.dashboard.settings_meta import _META_BY_KEY

    casted: dict[str, object] = {}
    for key, value in updates.items():
        meta = _META_BY_KEY.get(key)
        if meta is None:
            return {"error": f"Unknown setting: '{key}'"}
        if meta.value_type == "secret":
            return {"error": f"'{key}' is a secret setting and cannot be written via update_config_many"}

        validation_error = _validate_value_type(key, value)
        if validation_error:
            return {"error": validation_error}
        casted[key] = _cast_value(value, meta.value_type, meta.item_type)

    unreadable = _unreadable_database_error(project_dir)
    if unreadable:
        return unreadable

    async with _get_update_lock(project_dir):
        consistency_error = _validate_config_consistency_many(project_dir, casted)
        if consistency_error:
            return {"error": consistency_error}

        try:
            from sova.config.db_loader import save_setting
            from sova.db.session import get_session

            async with await get_session(project_dir=project_dir) as session, session.begin():
                for key, value in casted.items():
                    await save_setting(session, key, value)
            _invalidate_config_cache(project_dir)
        except Exception:  # noqa: BLE001 (any persist failure must return the error contract, never escape uncaught)
            log.warning("settings.atomic_db_write_failed", keys=sorted(casted), exc_info=True)
            return {"error": "Failed to persist settings (DB unavailable); no keys were changed"}

    for key, value in casted.items():
        if not _save_setting_to_toml(project_dir, key, value):
            log.debug("settings.toml_write_skipped", key=key)

    return {"status": "ok"}


def _validate_config_consistency_many(project_dir: Path | None, updates: dict[str, object]) -> str | None:
    """Like ``_validate_config_consistency``, but applies every key in *updates* together.

    Validating one key at a time rejects a multi-key change whose
    intermediate states are individually invalid even though the final
    combined state is fine. Fails open, same as the single-key version: only
    errors whose location touches an edited section are reported.
    """
    from pydantic import ValidationError

    from sova.config.loader import load_config
    from sova.config.models import ProjectConfig

    try:
        data = load_config(project_dir).model_dump()
    except Exception:  # noqa: BLE001 (fails open; an unloadable base config is not this save's fault)
        return None

    sections: set[str] = set()
    for key, value in updates.items():
        section, _, field = key.partition(".")
        sections.add(section)
        if field:
            target = data.get(section)
            if not isinstance(target, dict) or field not in target:
                continue
            target[field] = value
        elif key in data:
            data[key] = value

    try:
        ProjectConfig(**data)
    except ValidationError as exc:
        related = [
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors()
            if err.get("loc") and str(err["loc"][0]) in sections
        ]
        if related:
            return f"Activation rejected: {'; '.join(related)}"
    except Exception:  # noqa: BLE001 (unrelated validation failure must not block this save)
        return None
    return None


def _active_llm_provider(project_dir: Path | None) -> str | None:
    """Resolve the provider id used to scope a keyring-backed secret.

    Read via a fresh ``load_config()`` rather than any cached config, so the
    scope always matches whichever provider is actually active for this
    project right now: a secret saved after switching providers must be
    scoped to the new provider, not a stale one. Returns ``None`` on a
    config load failure rather than falling open to a fixed bucket name:
    nothing ever reads back from an "unknown" scope (every reader scopes by
    the real ``cfg.provider``/``provider_id``), so writing there would
    silently discard the credential while reporting success, the opposite
    of this codebase's "never silently discard credential state" posture.
    Callers must treat ``None`` as a hard failure for a write.
    """
    from sova.config.loader import load_config

    try:
        return load_config(project_dir).llm.provider
    except Exception:  # noqa: BLE001 (any load failure is reported to the caller as unresolvable)
        return None


def _keyring_scope(project_dir: Path | None, key: str, provider: str) -> str | None:
    """Return the scope segment for *key*'s keyring entry (``None`` when it cannot be resolved)."""
    from sova.llm import keyring_store

    return keyring_store.fixed_scope(key) or provider or _active_llm_provider(project_dir)


def _clear_keychain_entries(key: str, scoped_name: str, legacy_owned: bool) -> str | None:
    """Delete a secret's keychain entry (and its legacy unscoped one when *legacy_owned*).

    Returns a warning when a real delete failure (locked keychain, backend error, permission denied) means the
    old key may still live in the keyring and be picked up again by ``resolve_secret()`` on the next read,
    silently defeating the user's intent to fall back to ``ANTHROPIC_API_KEY``. No backend at all is not a failure.
    """
    from sova.llm import keyring_store

    deleted = keyring_store.delete_secret(scoped_name)
    legacy_deleted = keyring_store.delete_secret(key) if legacy_owned else True
    if keyring_store.is_keyring_available() and (not deleted or not legacy_deleted):
        return "Could not confirm the OS keychain entry was removed; the old key may still be used"
    return None


async def _save_secret(
    project_dir: Path | None, key: str, value: str, *, provider: str = ""
) -> tuple[bool, str | None]:
    """Persist a secret, preferring the OS keyring over a plaintext database row.

    An empty value clears the secret entirely (keyring entry deleted, database
    row cleared) rather than falling back to plaintext storage of nothing, so
    the user can deliberately fall back to an environment-variable credential.
    Clearing also attempts to delete the legacy unscoped keyring entry (if
    the resolved provider owns it; see below): a clear is an explicit user
    action, unlike the read-only legacy fallback applied elsewhere, so
    deleting it here is not a destructive auto-migration.

    Only ``key in keyring_store.RESOLVED_SECRET_KEYS`` is actually routed
    through the keyring: every other ``value_type="secret"`` setting keeps
    today's plaintext-database behaviour, since moving its storage without
    also updating its one read site to call ``resolve_secret()`` would
    silently replace a working credential with the literal sentinel string.

    The keyring entry is scoped to ``(project_dir, provider)`` via
    ``keyring_store.scoped_secret_name()`` (issue #1148): two projects on the
    same machine, or a provider switch within one project, must never read or
    overwrite each other's credential. *provider* names the provider the key
    is *for* (e.g. the Connections page's per-provider card); when empty, it
    falls back to the project's currently-active provider (the settings
    page's generic ``llm.api_key`` edit, with no per-card context). The
    database row (``key`` itself, unscoped) still only ever holds the
    non-secret ``SENTINEL`` marker or a plaintext fallback value, matching
    today's shape, and is therefore only ever meaningful for whichever
    provider was active at write time.

    Returns ``(db_ok, warning)``; ``warning`` is set when a keyring-backed key
    fell back to plaintext because the keyring write did not succeed (no
    backend, or a backend that refused the write), or when a clear could not
    confirm the keyring entry was actually removed. A provider that cannot be
    resolved at all (config load failure, with no explicit *provider* given)
    is a hard failure, not a fallback bucket: returns ``(False, ...)`` rather
    than writing to an unreadable scope while reporting success.
    """
    from sova.llm import keyring_store

    if key not in keyring_store.RESOLVED_SECRET_KEYS:
        return await _save_setting_to_db(project_dir, key, value), None

    resolved_dir = project_dir if project_dir is not None else Path.cwd()
    resolved_provider = _keyring_scope(project_dir, key, provider)
    if not resolved_provider:
        return False, "Could not determine the active LLM provider; the key was not saved"
    scoped_name = keyring_store.scoped_secret_name(key, resolved_dir, resolved_provider)
    # The legacy unscoped entry only ever belonged to whichever provider was
    # active when it was written, so it is only deleted here (never for an
    # arbitrary other provider's clear) when the resolved provider is also
    # the currently-active one.
    # Only llm.api_key ever had a legacy unscoped entry.
    legacy_owned = keyring_store.fixed_scope(key) is None and resolved_provider == _active_llm_provider(project_dir)

    if not value:
        warning = _clear_keychain_entries(key, scoped_name, legacy_owned)
        db_ok = await _save_setting_to_db(project_dir, key, "")
        return db_ok, warning

    if keyring_store.set_secret(scoped_name, value):
        return await _save_setting_to_db(project_dir, key, keyring_store.SENTINEL), None

    db_ok = await _save_setting_to_db(project_dir, key, value)
    if keyring_store.is_keyring_available():
        return db_ok, "OS keyring write failed; stored in the project database as plaintext"
    return db_ok, "OS keyring unavailable; stored in the project database as plaintext"


async def migrate_secret_to_keyring(project_dir: Path | None, key: str) -> dict:
    """Move an already plaintext-stored secret into the OS keyring.

    Explicit user action only (the settings page "Move to keychain" button):
    never triggered automatically on config load, startup, or migration.
    Fails without touching the database if the keyring write can't be
    confirmed by reading it back, so a partial migration never leaves the
    secret in neither place. Written under the same ``(project_dir, active
    provider)``-scoped keyring name ``_save_secret`` uses, so a later
    ``resolve_secret()`` scoped lookup finds it (issue #1148).
    """
    from sova.dashboard.settings_meta import _META_BY_KEY
    from sova.llm import keyring_store

    meta = _META_BY_KEY.get(key)
    if meta is None or meta.value_type != "secret":
        return {"error": f"'{key}' is not a secret setting"}
    if key not in keyring_store.RESOLVED_SECRET_KEYS:
        return {"error": f"'{key}' does not support keychain storage yet"}
    if not keyring_store.is_keyring_available():
        return {"error": "OS keyring is not available on this machine"}

    resolved_dir = project_dir if project_dir is not None else Path.cwd()
    provider = _keyring_scope(project_dir, key, "")
    if not provider:
        return {"error": "Could not determine the active LLM provider; the key was not migrated"}
    scoped_name = keyring_store.scoped_secret_name(key, resolved_dir, provider)

    async with _get_update_lock(project_dir):
        raw = _get_raw_config(project_dir)
        current = raw.get(key)
        if not current or current == keyring_store.SENTINEL:
            return {"error": f"'{key}' is not currently stored in the database as plaintext"}

        if not keyring_store.migrate_plaintext_to_keyring(scoped_name, str(current)):
            return {"error": "Failed to write the secret to the OS keyring"}

        db_ok = await _save_setting_to_db(project_dir, key, keyring_store.SENTINEL)
        if not db_ok:
            return {
                "error": "Secret written to the OS keyring, but the database update failed; "
                "retry or repair the database row manually"
            }

    return {"status": "ok", "key": key}


def _unreadable_database_error(project_dir: Path | None) -> dict | None:
    """An error result when a write would land in a database ``load_config()`` cannot read back.

    The flag lets the HTTP layer answer with a client error instead of a 200 body.
    """
    from sova.config.db_loader import config_db_unreadable_reason

    reason = config_db_unreadable_reason(project_dir)
    if reason is None:
        return None
    return {"error": f"Setting not saved: {reason}", "unreadable_database": True}


def _invalidate_config_cache(project_dir: Path | None) -> None:
    """Drop the cached config after a committed write, so the next load reads the new value."""
    from sova.config.loader import invalidate_config_cache

    invalidate_config_cache(project_dir or Path.cwd())


async def _save_setting_to_db(project_dir: Path | None, key: str, value: object) -> bool:
    """Persist a setting to the database. Returns True on success."""
    try:
        from sova.config.db_loader import save_setting
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session, session.begin():
            await save_setting(session, key, value)
        _invalidate_config_cache(project_dir)
        return True
    except (OSError, RuntimeError, SQLAlchemyError):
        log.warning("settings.db_save_failed", key=key, exc_info=True)
        return False


def _save_setting_to_toml(project_dir: Path | None, key: str, value: object) -> bool:
    """Best-effort update of sova.toml. Returns True on success."""
    toml_path = get_config_file_path(project_dir)
    if not toml_path.exists():
        return False

    try:
        import tomlkit

        doc = tomlkit.parse(toml_path.read_text(encoding="utf-8"))
        parts = key.split(".")
        target = doc
        for part in parts[:-1]:
            if part not in target:
                target[part] = tomlkit.table()
            target = target[part]

        target[parts[-1]] = value
        tmp_path = toml_path.with_suffix(".toml.tmp")
        tmp_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
        tmp_path.replace(toml_path)
        return True
    except ImportError:
        log.debug("tomlkit not available")
        return False
    except Exception:  # noqa: BLE001 (tomlkit surfaces arbitrary parse errors; the write is reported as failed)
        log.warning("settings.toml_write_failed", exc_info=True)
        return False


_SECRET_MASK_CHARS = frozenset("*•·")
_SECRET_MASK_PLACEHOLDER = "••••••••"


def _is_masked_secret(value: str) -> bool:
    """Return True when a secret value is only mask characters (bullets/asterisks).

    Such a value means the UI round-tripped the masked placeholder without the
    user typing a new secret, so it must not overwrite the stored value.
    """
    stripped = value.strip()
    return bool(stripped) and all(ch in _SECRET_MASK_CHARS for ch in stripped)


def _validate_value_type(key: str, value: str) -> str | None:
    """Validate the value against the expected type from settings metadata.

    Returns an error message string if invalid, None if valid.

    A secret's raw value must never appear in the returned message: every
    branch below otherwise echoes the submitted value back verbatim (e.g.
    "'{key}' must be one of {options}, got '{value}'"), which is fine for an
    ordinary setting but would leak a credential straight back to the client
    in an error response for ``value_type="secret"`` (issue #1148). Secrets
    have no ``options``/number/boolean shape to validate in practice, so this
    returns early rather than threading a redaction through every branch.
    """
    from sova.dashboard.settings_meta import _META_BY_KEY

    meta = _META_BY_KEY.get(key)
    if meta is None:
        return None
    if meta.value_type == "secret":
        return None

    # Validate against allowed options if present
    if meta.options and value not in meta.options:
        return f"'{key}' must be one of {meta.options}, got '{value}'"

    if meta.value_type == "number":
        stripped = value.strip()
        if not stripped:
            return f"'{key}' expects a number, got '{value}'"
        try:
            float(stripped)
        except ValueError:
            return f"'{key}' expects a number, got '{value}'"
    elif meta.value_type == "boolean":
        if value.lower() not in ("true", "false"):
            return f"'{key}' expects true or false, got '{value}'"
    elif meta.value_type == "list" and meta.item_type == "int":
        bad = [item for item in _cast_list(value) if not _is_positive_int_text(item)]
        if bad:
            return f"'{key}' expects positive whole numbers, got '{bad[0]}'"

    return None


def _validate_config_consistency(project_dir: Path | None, key: str, value: object) -> str | None:
    """Reject a value that would make the whole project config unloadable.

    _validate_value_type only inspects the edited field in isolation, so a
    cross-field rule (llm.provider="ollama" requires an explicit llm.model)
    passes it, gets persisted, and then every load_config() call for the
    project raises: agents stop spawning and the settings page can no longer
    render the field needed to undo it.

    Fails open. Only errors whose location touches the edited section are
    reported, so an unrelated pre-existing config problem can never block an
    unrelated save.
    """
    from pydantic import ValidationError

    from sova.config.loader import load_config
    from sova.config.models import ProjectConfig

    try:
        data = load_config(project_dir).model_dump()
    except Exception:  # noqa: BLE001 (fails open; an unloadable base config is not this save's fault)
        return None

    section, _, field = key.partition(".")
    if field:
        target = data.get(section)
        if not isinstance(target, dict) or field not in target:
            return None
        target[field] = value
    elif key in data:
        data[key] = value
    else:
        return None

    try:
        ProjectConfig(**data)
    except ValidationError as exc:
        related = [
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors()
            if err.get("loc") and str(err["loc"][0]) == section
        ]
        if related:
            return f"'{key}' rejected: {'; '.join(related)}"
    except Exception:  # noqa: BLE001 (unrelated validation failure must not block this save)
        return None
    return None


def _is_positive_int_text(item: str) -> bool:
    return item.isascii() and item.isdigit() and int(item) > 0


def _cast_value(value: str, value_type: str = "string", item_type: str = "string") -> object:
    """Try to cast a string value to the appropriate type.

    List-typed settings must never be stored as a bare string: the config
    models declare them as list[str], so a string value makes load_config()
    raise a ValidationError and every command for that project fails. A list
    whose ``item_type`` is ``int`` (``dashboard.hidden_issues``, a list[int])
    casts each entry, since ``_cast_list()`` yields strings.
    """
    if value_type == "list":
        items = _cast_list(value)
        if item_type == "int":
            return [int(item) if _is_positive_int_text(item) else item for item in items]
        return items
    if value_type == "secret":
        # A secret must never be coerced to bool/int/float just because it
        # happens to look like one (an all-digit token, a "true"-shaped value).
        return value
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _cast_list(value: str) -> list[str]:
    """Parse a list-typed setting from a JSON array or a comma-separated string."""
    stripped = value.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in stripped.split(",") if item.strip()]


def list_invariants(project_dir: Path | None = None) -> list[dict]:
    """List invariant scripts in the project."""
    if project_dir is None:
        project_dir = Path.cwd()

    inv_dir = project_dir / "invariants"
    if not inv_dir.is_dir():
        return []

    result = []
    for f in sorted(inv_dir.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            result.append(
                {
                    "name": f.name,
                    "path": str(f),
                    "executable": f.stat().st_mode & 0o111 != 0,
                }
            )
    return result


def list_personas(project_dir: Path | None = None) -> list[dict]:
    """List available personas for the project.

    A project-local ``personas/`` directory wins, so a project can override
    the shipped set. Otherwise fall back to SOVA's own packaged personas,
    which is what ``sova.knowledge.personas.load_persona()`` actually reads
    when no directory is passed: without this fallback the settings page
    reports no personas at all for a project that never created the
    directory, including SOVA's own checkout.
    """
    from sova.commands.catalog import get_personas_dir

    if project_dir is None:
        project_dir = Path.cwd()

    personas_dir = project_dir / "personas"
    if not personas_dir.is_dir():
        personas_dir = get_personas_dir()
    if not personas_dir.is_dir():
        return []

    result = []
    for f in sorted(personas_dir.iterdir()):
        if f.suffix == ".md" and not f.name.startswith("."):
            result.append(
                {
                    "name": f.stem,
                    "path": str(f),
                }
            )
    return result


def get_detected_persona(project_dir: Path | None = None) -> str | None:
    """Detect the project's persona based on tech stack."""
    if project_dir is None:
        project_dir = Path.cwd()

    try:
        from sova.knowledge.personas import detect_persona

        return detect_persona(project_dir)
    except (OSError, ValueError):
        log.warning("settings.persona_detect_failed", project_dir=str(project_dir), exc_info=True)
        return None


# Installation diff (review-changes modal) ----------------------------------


@dataclass
class FileDiff:
    """A single file's drift status for the installation review modal."""

    filename: str
    category: str  # "command" | "guideline"
    status: str  # conflict, local_modified, upstream_only, new, removed, local_only
    canonical_content: str | None = None
    local_content: str | None = None


@dataclass
class InstallationDiff:
    """Combined forward + reverse diff, ready for the settings review modal."""

    source_available: bool = True
    files: list[FileDiff] = field(default_factory=list)


def _read_rendered_or_none(path: Path, variables: dict[str, str]) -> str | None:
    """Read and render a canonical source file, returning None if missing/unreadable."""
    from sova.commands.templates import render_command

    text = read_text_or_none(path)
    if text is None:
        return None
    return render_command(text, variables)


def _build_category_diffs(
    category: str,
    source_dir: Path,
    target_dir: Path,
    diff: DiffResult,
    reverse: ReverseDiffResult,
    variables: dict[str, str],
) -> list[FileDiff]:
    """Merge a forward DiffResult and reverse ReverseDiffResult into FileDiff entries.

    Each filename appears at most once in the result, in priority order:
    reverse.modified (conflict/local_modified/removed) > diff.changed
    (upstream_only) > diff.new (new, or conflict on a name collision) >
    diff.removed (removed) > reverse.unmanaged (local_only) >
    reverse.deleted (local_only). The forward diff and reverse diff are
    computed independently and can name the same file for unrelated reasons
    (an unmanaged local file sharing a name with a newly-added canonical
    file; a tracked file the upstream changed that the user also deleted
    locally; a file both removed upstream and deleted locally), so later
    branches skip any filename already handled. Without this, the same
    filename could appear twice with conflicting statuses, and a name
    collision between a brand-new canonical file and a pre-existing
    unmanaged local file would be reported as a safe "new" install
    (``local_content=None``, checked by default) even though applying it
    would silently overwrite the local file.

    A reverse.modified entry whose canonical file was removed upstream
    (``canonical_removed=True``) is classified as "removed", not "conflict":
    the file no longer exists in ``source_files``, so ``_update_files()``'s
    allow-list filter would silently no-op if it were presented as a
    checkable, syncable conflict.
    """
    entries: list[FileDiff] = []
    handled: set[str] = set()
    unmanaged_local = set(reverse.unmanaged)

    def _add(filename: str, status: str, canonical_content: str | None, local_content: str | None) -> None:
        handled.add(filename)
        entries.append(
            FileDiff(
                filename=filename,
                category=category,
                status=status,
                canonical_content=canonical_content,
                local_content=local_content,
            )
        )

    for drift in reverse.modified:
        handled.add(drift.filename)
        if drift.canonical_removed:
            # The canonical file was removed upstream (not just changed), so it is
            # no longer present in source_files: presenting this as a syncable
            # "conflict" would be a silent no-op if the user applied it. Match the
            # diff.removed shape instead (informational, canonical_content=None).
            # Checked before the equal-content shortcut below: an empty local file
            # and the placeholder canonical_content="" both compare equal, which
            # would otherwise mask a real upstream removal as "clean".
            status = "removed"
            canonical_content = None
        elif drift.local_content == drift.canonical_content:
            # Hash-based drift detection fired, but the rendered canonical content is
            # byte-identical to what's on disk: a sync would be a no-op, so this is
            # clean rather than a conflict.
            continue
        else:
            status = "conflict" if drift.upstream_also_changed else "local_modified"
            canonical_content = drift.canonical_content
        _add(drift.filename, status, canonical_content, drift.local_content)

    def _rendered_content(filename: str) -> str | None:
        # diff.rendered is populated by _diff_files() as a byproduct of the hash
        # comparison it already performs, so this avoids a second read+render of
        # the canonical file. Fall back to a direct read for callers (tests, or a
        # DiffResult built by hand) that don't populate it.
        cached = diff.rendered.get(filename)
        if cached is not None:
            return cached
        return _read_rendered_or_none(source_dir / filename, variables)

    for filename in diff.changed:
        if filename in handled:
            continue
        _add(
            filename,
            "upstream_only",
            _rendered_content(filename),
            read_text_or_none(target_dir / filename),
        )

    for filename in diff.new:
        if filename in handled:
            continue
        if filename in unmanaged_local or (target_dir / filename).is_file():
            # A local file with this name already exists but was never
            # installed by SOVA: syncing it would silently overwrite the
            # local file, so this is a conflict, not a clean "new" install.
            # The direct is_file() check covers the no-manifest case, where
            # reverse.unmanaged is always empty (_reverse_diff_files() returns
            # early without a manifest) but a same-named local file can still
            # exist on disk.
            rendered_canonical = _rendered_content(filename)
            local_content = read_text_or_none(target_dir / filename)
            if rendered_canonical is not None and rendered_canonical == local_content:
                # The unmanaged local file is byte-identical to the rendered
                # canonical content: a sync would be a no-op, so this is clean
                # rather than a conflict, mirroring the reverse.modified check above.
                handled.add(filename)
                continue
            _add(filename, "conflict", rendered_canonical, local_content)
            continue
        _add(filename, "new", _rendered_content(filename), None)

    for filename in diff.removed:
        if filename in handled:
            continue
        _add(filename, "removed", None, read_text_or_none(target_dir / filename))

    for filename in reverse.unmanaged:
        if filename in handled:
            continue
        _add(filename, "local_only", None, read_text_or_none(target_dir / filename))

    for filename in reverse.deleted:
        if filename in handled:
            continue
        _add(filename, "local_only", None, None)

    return entries


def build_installation_diff(project_dir: Path, cfg: ProjectConfig) -> InstallationDiff:
    """Build a per-file diff merging upstream changes and local drift.

    Read-only: computes status for every non-clean command and guideline file
    without writing anything. Returns ``source_available=False`` when the
    canonical commands source directory doesn't exist (e.g. a non-editable
    pip install), since every file would otherwise misleadingly look drifted.
    """
    from sova.agents.claude_code import ClaudeCodeAdapter
    from sova.commands.catalog import get_canonical_dir, get_guidelines_dir
    from sova.commands.distribution import (
        diff_commands,
        diff_guidelines,
        reverse_diff_commands,
        reverse_diff_guidelines,
    )
    from sova.commands.manifest import read_manifest
    from sova.commands.templates import build_variables

    canonical_dir = get_canonical_dir()
    if not canonical_dir.is_dir():
        log.warning("settings.installation_diff.no_canonical_dir", path=str(canonical_dir))
        return InstallationDiff(source_available=False, files=[])

    variables = build_variables(cfg)
    files: list[FileDiff] = []

    claude_adapter = ClaudeCodeAdapter()
    commands_dir = claude_adapter.commands_dir(project_dir)
    files.extend(
        _build_category_diffs(
            "command",
            canonical_dir,
            commands_dir,
            diff_commands(canonical_dir, commands_dir, cfg, adapter=claude_adapter),
            reverse_diff_commands(canonical_dir, commands_dir, cfg, adapter=claude_adapter),
            variables,
        )
    )

    # Only diff guidelines if they were previously installed (manifest exists),
    # matching the guard in POST /setup/commands/sync.
    rules_dir = project_dir / ".claude" / "rules"
    if rules_dir.is_dir() and read_manifest(rules_dir) is not None:
        guidelines_dir = get_guidelines_dir()
        files.extend(
            _build_category_diffs(
                "guideline",
                guidelines_dir,
                rules_dir,
                diff_guidelines(guidelines_dir, rules_dir, cfg),
                reverse_diff_guidelines(guidelines_dir, rules_dir, cfg),
                variables,
            )
        )

    return InstallationDiff(source_available=True, files=files)
