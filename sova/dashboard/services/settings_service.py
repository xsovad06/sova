"""Settings service: config viewing/editing, invariants, detected stack."""

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
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from sova.config.context import resolve_project_dir_or_none
from sova.config.loader import load_config
from sova.dashboard.services import daemon_registry
from sova.dashboard.services.agent_pool import get_default_project_dir
from sova.db.session import get_session
from sova.utils.files import read_text_or_none
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Iterable

    from sova.commands.distribution import DiffResult, ReverseDiffResult
    from sova.config.models import ProjectConfig
    from sova.dashboard.settings_meta import SettingMeta

log = get_logger(component="dashboard.settings")

_T = TypeVar("_T")

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
    try:
        cfg = load_config(project_dir)
    except Exception:  # noqa: BLE001 (config may fail for many reasons (missing file, bad TOML))
        log.warning("settings.config_load_failed", project_dir=str(project_dir), exc_info=True)
        return {"_error": "No configuration found"}

    # Flatten the config into displayable key-value pairs
    result: dict = {}
    _flatten_dict("", cfg.model_dump(), result)
    # load_config resolves this token for callers, but the settings page needs
    # the stored marker to identify Keychain storage and avoid migrating it twice.
    from sova.config.db_loader import read_stored_settings
    from sova.config.root import owning_config_dir
    from sova.llm import keyring_store

    # The same directory load_config() read, so a worktree sees the primary checkout's marker.
    stored = read_stored_settings(owning_config_dir(project_dir if project_dir is not None else Path.cwd()))
    if stored and stored.get(keyring_store.JIRA_TOKEN_KEY) == keyring_store.SENTINEL:
        result[keyring_store.JIRA_TOKEN_KEY] = keyring_store.SENTINEL
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
    parts = row_key.split(".")
    for index, part in enumerate(parts):
        field_info = model.model_fields.get(part)
        if field_info is None:
            return False
        nested = _model_class(field_info.annotation)
        if nested is None:
            # Parts left after a leaf are read only when the field holds entries (a dict or list); a stray
            # ``max_parallel_agents.x`` row is read by nothing and clashes with the scalar.
            return index == len(parts) - 1 or _holds_entries(field_info.annotation)
        model = nested
    return True


def _holds_entries(annotation: object) -> bool:
    """True when *annotation* is a mapping or list type, whose entries a longer dotted path can address."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        return any(_holds_entries(arg) for arg in typing.get_args(annotation))
    return (origin or annotation) in (dict, list)


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
            return unreadable_database_error(project_dir) or {
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

    step = routing_task(key)
    if step is not None:
        return await _reset_route(project_dir, key, step)

    meta = _META_BY_KEY.get(key)
    refusal = _reset_refusal(project_dir, key, meta) or unreadable_database_error(project_dir)
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
            warning = warning or _still_set_warning(key, result["value"], toml_cleared=toml_cleared)
    if warning:
        result["warning"] = warning
    return result


async def _reset_route(project_dir: Path | None, key: str, step: str) -> dict:
    """``reset_config()`` for one route: the Models view's "Use the role model", under the key the CLI names."""
    cleared = await clear_step_model(project_dir, step=step)
    return cleared if "error" in cleared else {**cleared, "key": key, "value": cleared["model"]}


def _still_set_warning(key: str, value: object, *, toml_cleared: bool) -> str:
    """The warning for a removed stored value that *key* still has (*value*), naming the layer that may set it."""
    reason = (
        "it could not be removed from sova.toml, which may still set it"
        if not toml_cleared
        else "another config layer still sets it"
    )
    return f"The stored value was removed, but '{key}' is still {value!r}: {reason}."


_ROUTING_KEY = "llm.routing"


def _routing_env_refusal() -> dict | None:
    """An error result when a ``SOVA_`` variable sets the routing table, so a stored route would never apply.

    Only ``SOVA_LLM_ROUTING`` (a JSON object) overrides routes: the settings models set no nested delimiter, so a
    per-step ``SOVA_LLM_ROUTING_<STEP>`` variable is ignored by ``load_config()`` and needs no check here.
    """
    from sova.config.loader import env_override_var

    env_var = env_override_var(_ROUTING_KEY)
    if env_var:
        return {
            "error": f"'{_ROUTING_KEY}' is set by {env_var} in the server environment. Change it there.",
            "env_var": env_var,
        }
    return None


def _routing_refusal(step: str) -> dict | None:
    """Why ``llm.routing.<step>`` cannot be written here, as an error result, or None when it can."""
    from sova.dashboard.services.model_map_service import ROUTING_KEYS

    if step not in ROUTING_KEYS:
        return {
            "error": f"Unknown step '{step}': routing names a task type or an issue size ({_routing_names_hint()})",
            "unknown": True,
        }
    return _routing_env_refusal()


def _clear_refusal(project_dir: Path | None, step: str) -> dict | None:
    """``_routing_refusal()`` for deleting a route: a stored row for a step no longer named may still be removed.

    Only setting an unknown step is refused. A stale ``llm.routing.<old_task>`` row (a task renamed or dropped
    from ``TASK_TYPE_KEYS``) would otherwise be impossible to reset one key at a time.
    """
    from sova.config.db_loader import read_stored_settings

    stored = read_stored_settings(project_dir)
    if stored is not None and f"{_ROUTING_KEY}.{step}" in stored:
        return _routing_env_refusal()
    return _routing_refusal(step)


async def _write_routing_rows(
    project_dir: Path | None, *, set_rows: dict[str, str], delete: tuple[str, ...] = ()
) -> int:
    """Write and delete ``llm.routing.<step>`` rows in one transaction; returns how many rows were deleted.

    The routing table is stored one row per step. A whole-table ``llm.routing`` row (an older write) would
    replace the per-step rows when the config is assembled, so it is split into per-step rows first.
    """
    from sqlalchemy import select

    from sova.config.db_loader import delete_setting, save_setting
    from sova.db.models import ProjectSetting

    deleted = 0
    async with await get_session(project_dir=project_dir) as session, session.begin():
        # The value only, not the row object: delete_setting() removes the row with a bulk DELETE, which would
        # leave a loaded object behind in the session for a later insert to collide with.
        legacy = (
            await session.execute(select(ProjectSetting.value).where(ProjectSetting.key == _ROUTING_KEY))
        ).scalar_one_or_none()
        if legacy is not None:
            try:
                table = json.loads(legacy)
            except (json.JSONDecodeError, TypeError):
                table = None
            await delete_setting(session, _ROUTING_KEY)
            for step, model in table.items() if isinstance(table, dict) else ():
                if step not in set_rows and step not in delete:
                    await save_setting(session, f"{_ROUTING_KEY}.{step}", model)
        for step, model in set_rows.items():
            await save_setting(session, f"{_ROUTING_KEY}.{step}", model)
        for step in delete:
            deleted += await delete_setting(session, f"{_ROUTING_KEY}.{step}")
    _invalidate_config_cache(project_dir)
    return deleted


async def _run_to_completion(work: Awaitable[_T]) -> _T:
    """Await *work* so that it always finishes before this returns, even when the caller is cancelled.

    A bare ``asyncio.shield()`` lets the work run on after a cancellation, but the cancelled caller leaves
    its ``async with _get_update_lock()`` block at once, so a second write could start while the first is still
    committing. This waits for the work to end first and only then re-raises the cancellation, so the lock is
    held for the whole write.
    """
    task = asyncio.ensure_future(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            await asyncio.wait({task})  # unlike shield(), a cancellation here leaves the task running
        if not task.cancelled() and task.exception() is not None:  # retrieved, so it is not reported as lost
            log.warning("settings.write_failed_after_cancel", exc_info=task.exception())
        raise


async def _write_routing_rows_to_completion(
    project_dir: Path | None, *, set_rows: dict[str, str], delete: tuple[str, ...] = ()
) -> int:
    """``_write_routing_rows()`` that always finishes before it returns, even when the caller is cancelled."""
    return await _run_to_completion(_write_routing_rows(project_dir, set_rows=set_rows, delete=delete))


async def set_step_model(project_dir: Path | None, *, step: str, model: str, skip_if_unchanged: bool = False) -> dict:
    """Set the model one step runs on: writes exactly the ``llm.routing.<step>`` row (Settings, Models view).

    *step* is a task type (``TASK_TYPE_KEYS``) or an issue size. An empty *model* deletes the row instead, so the
    step uses its role's model again (``clear_step_model()``). Refused for an unknown step, a model ID with
    whitespace, a routing table a ``SOVA_`` variable sets (``env_var``), and a value that would make the config
    unloadable. With *skip_if_unchanged*, a step already at *model* (checked under the update lock, so a
    concurrent write cannot invalidate the check) is not written and the result carries ``unchanged``. Returns
    ``{"status": "ok", "step", "model"}`` or ``{"error": ...}``.
    """
    model = model.strip()
    if not model:
        return await clear_step_model(project_dir, step=step, skip_if_unchanged=skip_if_unchanged)
    refusal = _routing_refusal(step) or unreadable_database_error(project_dir)
    if refusal:
        return refusal
    if error := model_id_error(model):
        return {"error": error}
    async with _get_update_lock(project_dir):
        if skip_if_unchanged and _step_unchanged(project_dir, step, model):
            return {"status": "ok", "step": step, "model": model, "unchanged": True}
        routing = _current_routing(project_dir) or {}
        consistency_error = _validate_config_consistency(project_dir, _ROUTING_KEY, {**routing, step: model})
        if consistency_error:
            return {"error": consistency_error}
        try:
            await _write_routing_rows_to_completion(project_dir, set_rows={step: model})
        except (OSError, RuntimeError, SQLAlchemyError):
            log.warning("settings.route_write_failed", step=step, exc_info=True)
            return {"error": "Failed to save the step's model (DB unavailable)", "unavailable": True}
    _save_setting_to_toml(project_dir, f"{_ROUTING_KEY}.{step}", model)
    return {"status": "ok", "step": step, "model": model}


async def clear_step_model(project_dir: Path | None, *, step: str, skip_if_unchanged: bool = False) -> dict:
    """Delete ``llm.routing.<step>``, so the step uses its role's model again ("Use the role model").

    Also removes the step from ``sova.toml``, which would otherwise set it again on the next load. With
    *skip_if_unchanged*, a step already unset (checked under the update lock) is not written and the result
    carries ``unchanged``. Returns ``{"status": "ok", "step", "model": None, "deleted"}`` or ``{"error": ...}``.
    """
    refusal = _clear_refusal(project_dir, step) or unreadable_database_error(project_dir)
    if refusal:
        return refusal
    async with _get_update_lock(project_dir):
        if skip_if_unchanged and _step_unchanged(project_dir, step, None):
            return {"status": "ok", "step": step, "model": None, "unchanged": True}

        async def clear() -> tuple[int, bool]:
            # One unit that a cancellation cannot split: a row deleted while sova.toml still sets the step would
            # bring the override back on the next config load.
            removed = await _write_routing_rows(project_dir, set_rows={}, delete=(step,))
            cleared = _remove_setting_from_toml(project_dir, f"{_ROUTING_KEY}.{step}")
            _invalidate_config_cache(project_dir)
            return removed, cleared

        try:
            deleted, toml_cleared = await _run_to_completion(clear())
        except (OSError, RuntimeError, SQLAlchemyError):
            log.warning("settings.route_delete_failed", step=step, exc_info=True)
            return {"error": "Failed to remove the step's model (DB unavailable)", "unavailable": True}
    result: dict[str, Any] = {"status": "ok", "step": step, "model": None, "deleted": deleted}
    routing = _current_routing(project_dir)
    still = routing.get(step) if routing is not None else None
    if routing is None:
        result["warning"] = (
            f"The stored value was removed, but the effective '{_ROUTING_KEY}.{step}' could not be read back, "
            "so whether another config layer still sets it is unknown."
        )
    elif still is not None:
        result["warning"] = _still_set_warning(f"{_ROUTING_KEY}.{step}", still, toml_cleared=toml_cleared)
    return result


def reload_key(key: str) -> str:
    """The key for ``apply_config_update()``: one route (``llm.routing.<step>``) reloads the whole table."""
    return _ROUTING_KEY if routing_task(key) is not None else key


def routing_task(key: str) -> str | None:
    """The step a ``llm.routing.<step>`` key names, or None for any other key (``llm.routing`` itself included)."""
    prefix = f"{_ROUTING_KEY}."
    return key.removeprefix(prefix) if key.startswith(prefix) else None


def _routing_names_hint() -> str:
    """The valid names a routing key may take, for an error message."""
    from sova.llm.complexity import ComplexityTier
    from sova.llm.routing import TASK_TYPE_KEYS

    return (
        f"valid task types: {', '.join(sorted(TASK_TYPE_KEYS))}; "
        f"valid issue sizes: {', '.join(tier.value for tier in ComplexityTier)}"
    )


def model_id_error(model: object) -> str | None:
    """Why *model* cannot be stored as a route's model ID, or None when it can."""
    if not isinstance(model, str) or not model.strip():
        return f"{model!r} is not a model ID: a route needs a non-empty model ID string"
    if any(ch.isspace() for ch in model):
        return f"'{model}' is not a model ID: it contains whitespace"
    return None


def _parse_routes(value: str) -> tuple[dict[str, str], str | None]:
    """Parse a ``llm.routing`` JSON object into ``{step: model}``, or return why it cannot be stored.

    Every entry is checked before anything is written, so one bad entry rejects the whole object.
    """
    from sova.dashboard.services.model_map_service import ROUTING_KEYS

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        return {}, f'\'{_ROUTING_KEY}\' expects a JSON object such as {{"develop": "<model ID>"}}: {exc.msg}'
    if not isinstance(parsed, dict):
        return {}, f"'{_ROUTING_KEY}' expects a JSON object, got {type(parsed).__name__}"
    unknown = sorted(set(parsed) - ROUTING_KEYS)
    if unknown:
        return {}, f"Unknown routing key {', '.join(repr(name) for name in unknown)} ({_routing_names_hint()})"
    for step, model in parsed.items():
        error = model_id_error(model)
        if error:
            return {}, f"'{_ROUTING_KEY}.{step}': {error}"
    return parsed, None


def _short_name_warning(models: Iterable[str]) -> str | None:
    """A warning naming the short model names (``sonnet``) among *models*, which the Claude CLI resolves itself."""
    from sova.dashboard.services.model_map_service import is_short_name

    short = sorted({model for model in models if is_short_name(model)})
    if not short:
        return None
    names = ", ".join(short)
    if len(short) > 1:
        return f"{names} are short model names that the Claude CLI resolves; pin explicit model IDs instead."
    return f"{names} is a short model name that the Claude CLI resolves; pin an explicit model ID instead."


async def _replace_routing_rows(project_dir: Path | None, routes: dict[str, str]) -> None:
    """Make *routes* the whole routing table in one transaction: every ``llm.routing`` row goes, then one row a step.

    The whole-table ``llm.routing`` row an older write left, and any row for a step that is no longer named,
    go with them, so a failure never leaves a half-replaced table.
    """
    from sova.config.db_loader import delete_setting, save_setting

    async with await get_session(project_dir=project_dir) as session, session.begin():
        await delete_setting(session, _ROUTING_KEY, with_children=True)
        for step, model in routes.items():
            await save_setting(session, f"{_ROUTING_KEY}.{step}", model)
    _invalidate_config_cache(project_dir)


async def replace_routing(project_dir: Path | None, *, routes: dict[str, str]) -> dict:
    """Make *routes* the project's whole routing table (``sova config set llm.routing '{...}'``).

    A step missing from *routes* loses its row and goes back to the model its role picks, so ``{}`` clears every
    route. Refused for a routing table a ``SOVA_`` variable sets (``env_var``), an unreadable database, and a
    table that would make the config unloadable. Returns ``{"status": "ok", "key", "value"}`` (``unchanged``
    when the table in effect already is *routes*, with no write) or ``{"error": ...}``.
    """
    refusal = _routing_env_refusal() or unreadable_database_error(project_dir)
    if refusal:
        return refusal
    result: dict[str, Any] = {"status": "ok", "key": _ROUTING_KEY, "value": json.dumps(routes)}
    async with _get_update_lock(project_dir):
        if _stored_routing(project_dir) == routes and _current_routing(project_dir) == routes:
            return {**result, "unchanged": True}
        consistency_error = _validate_config_consistency(project_dir, _ROUTING_KEY, routes)
        if consistency_error:
            return {"error": consistency_error}

        async def replace() -> bool:
            # One unit that a cancellation cannot split: a table replaced while sova.toml still lists a dropped
            # step would bring that route back on the next config load.
            await _replace_routing_rows(project_dir, routes)
            try:
                cleared = _remove_setting_from_toml(project_dir, _ROUTING_KEY)
                if routes:
                    # False here can only mean there is no sova.toml, so it is no sign of a route left behind.
                    _save_setting_to_toml(project_dir, _ROUTING_KEY, routes)
            except OSError:
                # The rows are committed and in effect: only the sova.toml mirror failed, which is no DB failure.
                log.warning("settings.routes_toml_failed", exc_info=True)
                return False
            return cleared

        try:
            toml_cleared = await _run_to_completion(replace())
        except (OSError, RuntimeError, SQLAlchemyError):
            log.warning("settings.routes_replace_failed", exc_info=True)
            return {"error": "Failed to save the routing table (DB unavailable)", "unavailable": True}
    left_behind = None if toml_cleared else _routes_left_behind_warning(project_dir, routes)
    warning = " ".join(w for w in (left_behind, _short_name_warning(routes.values())) if w)
    return {**result, "warning": warning} if warning else result


def _routes_left_behind_warning(project_dir: Path | None, routes: dict[str, str]) -> str | None:
    """After a whole-table replace whose ``sova.toml`` cleanup failed: a warning naming a dropped step still set."""
    effective = _current_routing(project_dir)
    if effective is None:
        return (
            f"The routing table was replaced, but '{_ROUTING_KEY}' could not be read back and could not be removed "
            "from sova.toml, so whether sova.toml still sets a dropped step is unknown."
        )
    left = sorted(set(effective) - set(routes))
    if not left:
        return None
    names = ", ".join(f"'{_ROUTING_KEY}.{step}'" for step in left)
    return (
        f"The routing table was replaced, but {names} still applies: it could not be removed from sova.toml, "
        "which may still set it."
    )


def _stored_routing(project_dir: Path | None) -> dict[str, Any] | None:
    """The per-step routing rows the database stores, as ``{step: model}``.

    None when they cannot be read, or when a whole-table ``llm.routing`` row (an older write) is stored: that row
    masks the per-step rows, so the stored table is not normalized and a write is needed to make it so.
    """
    from sova.config.db_loader import read_stored_settings

    stored = read_stored_settings(project_dir)
    if stored is None or _ROUTING_KEY in stored:
        return None
    return {step: value for key, value in stored.items() if (step := routing_task(key)) is not None}


async def update_routing(project_dir: Path | None, *, key: str, value: str) -> dict:
    """``update_config()`` for ``llm.routing`` (a JSON object, replacing every route) and ``llm.routing.<step>``.

    The object form is ``replace_routing()``. The single-key form sets one step and leaves the others alone; an
    empty value deletes that step's row (``clear_step_model()``). A step already at the value, or already unset,
    answers ``unchanged`` without a write. Returns what those functions return, with ``key`` and ``value``.
    """
    step = routing_task(key)
    if step is None:
        routes, error = _parse_routes(value)
        return {"error": error} if error else await replace_routing(project_dir, routes=routes)

    model = value.strip()
    refusal = _routing_refusal(step) if model else _clear_refusal(project_dir, step)
    if refusal:
        return refusal
    if model and (error := model_id_error(model)):
        return {"error": error}
    result = await (
        set_step_model(project_dir, step=step, model=model, skip_if_unchanged=True)
        if model
        else clear_step_model(project_dir, step=step, skip_if_unchanged=True)
    )
    if "error" in result:
        return result
    if result.get("unchanged"):
        return {"status": "ok", "key": key, "value": model, "unchanged": True}
    out: dict[str, Any] = {"status": "ok", "key": key, "value": model, "cleared": not model}
    warning = result.get("warning") or _short_name_warning([model])
    if warning:
        out["warning"] = warning
    return out


def _step_unchanged(project_dir: Path | None, step: str, model: str | None) -> bool:
    """True when *step* already runs on *model* (None: unset) and the stored rows say so too, so no write is needed.

    The stored rows are checked as well as the effective table: a whole-table ``llm.routing`` row can mask a
    per-step row that a later write would bring back.
    """
    routing = _current_routing(project_dir)
    stored = _stored_routing(project_dir)
    return routing is not None and stored is not None and routing.get(step) == model == stored.get(step)


def _changed_since_read(project_dir: Path | None, expected: dict[str, Any] | None) -> dict | None:
    """A ``conflict`` error result when a key in *expected* no longer has the value the caller read, else None.

    Called under the update lock. An unloadable config is a conflict too: the comparison cannot be made.
    """
    if not expected:
        return None
    from sova.dashboard.services.model_map_service import setting_value

    try:
        cfg = load_config(project_dir)
        changed = sorted(key for key, value in expected.items() if setting_value(cfg, key) != value)
    except Exception:  # noqa: BLE001 (an unreadable config cannot confirm the value; refuse rather than overwrite)
        log.warning("settings.pin_recheck_failed", exc_info=True)
        return {"error": "Could not re-read the configuration to confirm nothing changed; try again", "conflict": True}
    if changed:
        return {"error": f"{', '.join(changed)} changed since it was read; reload and try again", "conflict": True}
    return None


def _current_routing(project_dir: Path | None) -> dict[str, str] | None:
    """The effective routing table, or None when the config cannot be loaded (never an empty table for a failure)."""
    try:
        return dict(load_config(project_dir).llm.routing)
    except Exception:  # noqa: BLE001 (an unloadable config is reported by the save path's consistency check)
        return None


async def pin_short_names(
    project_dir: Path | None, *, keys: list[str] | None = None, catalog: list[str] | None = None
) -> dict:
    """Replace model settings stored as a short name (``sonnet``) with the exact ID each runs as today.

    *keys* limits it to those settings ("Pin"); None pins every one ("Pin all"). The exact ID comes from
    ``model_map_service.short_name_settings()``: ``resolve_alias()`` first, else the single model of that family
    in *catalog*. A setting whose ID is ambiguous, or that a ``SOVA_`` variable sets, is left alone and listed in
    ``skipped``. Single-model and list settings are written in one transaction, routing rows in another.
    Returns ``{"status": "ok", "pinned": {key: value}, "skipped": [{key, reason}]}`` or ``{"error": ...}``.
    """
    from sova.dashboard.config_errors import CONFIG_LOAD_ERRORS
    from sova.dashboard.services.model_map_service import short_name_settings

    try:
        cfg = load_config(project_dir)
    except CONFIG_LOAD_ERRORS:  # a bug in the loader is not an outage: it reaches the catch-all handler
        log.warning("settings.pin.config_unloadable", exc_info=True)
        return {"error": "Could not load the configuration", "unavailable": True}
    pinned: dict[str, Any] = {}
    routes: dict[str, str] = {}
    updates: dict[str, str] = {}
    # What each pinned key was when read: compared under the write lock, so a concurrent edit is not overwritten.
    read_values: dict[str, Any] = {}
    skipped: list[dict[str, str]] = []
    for item in short_name_settings(cfg, catalog):
        key = item["key"]
        if keys is not None and key not in keys:
            continue
        if item["env_var"]:
            skipped.append({"key": key, "reason": f"set by {item['env_var']}"})
        elif item["pin_to"] is None:
            skipped.append({"key": key, "reason": f"'{item['value']}' does not name one model here; pick one"})
        elif key.startswith(f"{_ROUTING_KEY}."):
            routes[key.removeprefix(f"{_ROUTING_KEY}.")] = item["pin_to"]
            pinned[key] = item["pin_to"]
            read_values[key] = item["value"]
        else:
            value = item["pin_to"]
            updates[key] = json.dumps(value) if isinstance(value, list) else value
            pinned[key] = value
            read_values[key] = item["value"]
    unknown = sorted(set(keys or ()) - set(pinned) - {s["key"] for s in skipped})
    skipped += [{"key": key, "reason": "not stored as a short name"} for key in unknown]
    if updates:
        result = await update_config_many(project_dir, updates, expected={k: read_values[k] for k in updates})
        if "error" in result:
            return result
    # The settings the first transaction stored. An error from the routing write below lists them as ``committed``
    # so the caller still applies their reloads: they are stored, and a retry will not pin them again.
    committed = sorted(updates)
    if routes:
        unreadable = unreadable_database_error(project_dir)
        if unreadable:
            return {**unreadable, "committed": committed}
        async with _get_update_lock(project_dir):
            stale = _changed_since_read(
                project_dir, {f"{_ROUTING_KEY}.{step}": read_values[f"{_ROUTING_KEY}.{step}"] for step in routes}
            )
            if stale:
                return {**stale, "committed": committed}
            try:
                await _write_routing_rows_to_completion(project_dir, set_rows=routes)
            except (OSError, RuntimeError, SQLAlchemyError):
                log.warning("settings.pin_routes_failed", exc_info=True)
                return {
                    "error": "Failed to save the pinned step models (DB unavailable)",
                    "pinned": pinned,
                    "committed": committed,
                    "unavailable": True,
                }
        for step, model in routes.items():
            _save_setting_to_toml(project_dir, f"{_ROUTING_KEY}.{step}", model)
    return {"status": "ok", "pinned": pinned, "skipped": skipped}


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

    if key == _ROUTING_KEY or routing_task(key) is not None:
        return await update_routing(project_dir, key=key, value=value)

    meta = _META_BY_KEY.get(key)
    if meta is None:
        return {"error": f"Unknown setting: '{key}'", "unknown": True}

    if meta.value_type == "secret" and _is_masked_secret(value):
        # The UI submitted the masked placeholder unchanged; do not overwrite
        # the stored key with the mask string.
        return {"status": "ok", "key": key, "value": value, "unchanged": True}

    validation_error = _validate_value_type(key, value)
    if validation_error:
        return {"error": validation_error}

    cast = _cast_value(value, meta.value_type, meta.item_type)

    unreadable = unreadable_database_error(project_dir)
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


async def update_config_many(
    project_dir: Path | None, updates: dict[str, str], *, expected: dict[str, Any] | None = None
) -> dict:
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

    *expected* maps a key to the value the caller read before computing its update. It is compared under the
    write lock, and a key that changed since is refused (``conflict``) rather than overwritten with a value
    derived from the old one.
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

    unreadable = unreadable_database_error(project_dir)
    if unreadable:
        return unreadable

    async with _get_update_lock(project_dir):
        stale = _changed_since_read(project_dir, expected)
        if stale:
            return stale
        consistency_error = _validate_config_consistency_many(project_dir, casted)
        if consistency_error:
            return {"error": consistency_error}

        try:
            await _run_to_completion(_save_settings_in_one_transaction(project_dir, casted))
        except Exception:  # noqa: BLE001 (any persist failure must return the error contract, never escape uncaught)
            log.warning("settings.atomic_db_write_failed", keys=sorted(casted), exc_info=True)
            return {"error": "Failed to persist settings (DB unavailable); no keys were changed"}

    for key, value in casted.items():
        if not _save_setting_to_toml(project_dir, key, value):
            log.debug("settings.toml_write_skipped", key=key)

    return {"status": "ok"}


async def _save_settings_in_one_transaction(project_dir: Path | None, values: dict[str, object]) -> None:
    """Write every key in *values* in one DB transaction, then drop the cached config."""
    from sova.config.db_loader import save_setting

    async with await get_session(project_dir=project_dir) as session, session.begin():
        for key, value in values.items():
            await save_setting(session, key, value)
    _invalidate_config_cache(project_dir)


def _validate_config_consistency_many(project_dir: Path | None, updates: dict[str, object]) -> str | None:
    """Like ``_validate_config_consistency``, but applies every key in *updates* together.

    Validating one key at a time rejects a multi-key change whose
    intermediate states are individually invalid even though the final
    combined state is fine. Fails open, same as the single-key version: only
    errors whose location touches an edited section are reported.
    """
    from pydantic import ValidationError

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


def unreadable_database_error(project_dir: Path | None) -> dict | None:
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


# Config reload dispatch: what a saved setting does to the running components.

RESTART_REQUIRED_PREFIXES = frozenset({"server.", "watch.", "database_url"})

RELOAD_PREFIX_MAP: dict[str, str] = {
    "supervisor.": "supervisor",
    "oversight.": "oversight",
    "watchdog.": "watchdog",
    "memory_guard.": "watchdog",
    "pr_monitor.": "pr_monitor",
    "coderabbit_quota.": "coderabbit_quota",
    "integration.": "integration",
    "llm.": "llm",
    "agent.runtime": "runtime",
    "codex.": "runtime",
}


def match_reload_target(key: str) -> str | None:
    """Match a config key to a reload target via prefix lookup."""
    for prefix, target in RELOAD_PREFIX_MAP.items():
        if key == prefix.rstrip(".") or key.startswith(prefix):
            return target
    return None


async def _start_pr_throttle_if_enabled(cfg: ProjectConfig, project_dir: Path | None) -> None:
    """Start the PR throttle loop for *project_dir* once the quota is enabled at runtime.

    Enabling the quota after the server started leaves no loop to reload; without one, create_pr's
    heartbeat check sends every PR down the direct path. *project_dir* and *cfg* are the ones
    ``reload_all_configs`` resolved (a request scope, else the single-project default ``create_app()``
    was given, which need not be the working directory), so the start, the reload and the daemon
    components lookup all act on the same project. ``None`` is multi-project mode with no request scope:
    the root config decides nothing there, so nothing starts (a loop already registered under the working
    directory is still reloaded by the caller, since that is the config the request just wrote).
    """
    from sova.dashboard.services.daemon_registry import ensure_pr_throttle_loop

    if project_dir is None:
        log.debug("settings.pr_throttle_start_skipped", reason="no project in scope")
        return
    await ensure_pr_throttle_loop(project_dir, cfg)


async def dispatch_config_reload(
    target: str,
    cfg: ProjectConfig,
    components: dict,
    project_dir: Path | None,
) -> None:
    """Execute the reload for a single target. Raises on failure."""
    if target == "supervisor":
        await daemon_registry.reload_daemon_config(project_dir)
    elif target == "oversight":
        await reload_oversight_config_with_cfg(cfg)
    elif target == "watchdog":
        wd = components.get("watchdog")
        if wd is not None:
            wd.reload_config(cfg.watchdog, cfg.memory_guard)
    elif target == "pr_monitor":
        for mon in components.get("pr_monitors", []):
            mon.reload_config(cfg.pr_monitor, cfg.notification)
    elif target == "coderabbit_quota":
        # Reloading acts on whatever loop *components* holds, which the caller looked up under the resolved
        # project (the working directory when no project is in scope, as for every other target). Starting
        # a new loop is a decision about a named project, so it needs one.
        for throttle in components.get("pr_throttles", []):
            throttle.reload_config(cfg.coderabbit_quota)
        await _start_pr_throttle_if_enabled(cfg, project_dir)
    elif target == "integration":
        for mqm in components.get("merge_queue_monitors", []):
            mqm.reload_config(cfg.integration, cfg.notification)
    elif target == "llm":
        from sova.llm.client import reload_provider_async

        await reload_provider_async(cfg, project_dir)
    elif target == "runtime":
        from sova.ipc.runtime import reload_runtime

        reload_runtime(cfg)


async def reload_all_configs(project_dir: Path | None, key: str) -> str | None:
    """Dispatch config reload to the appropriate daemons based on key prefix.

    Returns a lifecycle error message string, or None on success.
    """
    from sova.dashboard.services.daemon_registry import get_daemon_components

    matched = match_reload_target(key)
    if matched is None:
        return None

    try:
        # Resolve the target once, the way the lifespan keyed its loops at startup, so the config, the
        # daemon components lookup and the dispatch all name the same project. Without a request scope
        # that is the single-project default, which need not be the working directory.
        target_dir = resolve_project_dir_or_none(project_dir, default_project_dir=get_default_project_dir())
        cfg = await asyncio.to_thread(load_config, target_dir)
        resolved_key = str(target_dir.resolve()) if target_dir else str(Path.cwd().resolve())
        components = get_daemon_components().get(resolved_key, {})
        await dispatch_config_reload(matched, cfg, components, target_dir)
    except Exception:  # noqa: BLE001 (reload spans config load, daemon lookup and per-component dispatch)
        log.warning("settings.reload_failed", target=matched, exc_info=True)
        return f"Config saved but {matched} reload failed"

    return None


async def reload_oversight_config_with_cfg(cfg: ProjectConfig) -> None:
    """Trigger a config reload on the oversight agent using a pre-loaded config.

    Manages lifecycle: stops when disabled, starts when enabled.
    """
    from sova.dashboard.services.daemon_registry import get_oversight_agent, get_oversight_lifecycle_lock

    async with get_oversight_lifecycle_lock():
        agent = get_oversight_agent()
        if agent is not None:
            agent.reload_config(cfg)
            if not agent.enabled and agent.running:
                await agent.stop()
                log.info("settings.oversight_stopped_on_disable")
            elif agent.enabled and not agent.running:
                agent.start()


def config_value_to_str(raw: object) -> str:
    """Normalize a JSON boolean or number to the string form ``update_config`` expects."""
    if isinstance(raw, bool):
        return "true" if raw else "false"
    return str(raw)


def key_requires_restart(key: str) -> bool:
    """Return True when changing *key* only takes effect after a server restart."""
    from sova.dashboard.settings_meta import get_meta

    meta = get_meta(key)
    if meta is not None and meta.requires_restart:
        return True
    return any(key == prefix or key.startswith(prefix) for prefix in RESTART_REQUIRED_PREFIXES)


async def apply_config_update(project_dir: Path, key: str, result: dict) -> None:
    """Propagate a saved setting to running components and annotate *result*."""
    if key == "max_parallel_agents":
        from sova.dashboard.services.agent_pool import sync_max_concurrent

        sync_max_concurrent(project_dir)
    lifecycle_error = await reload_all_configs(project_dir, key)
    if lifecycle_error:
        result["lifecycle_error"] = lifecycle_error
    if key_requires_restart(key):
        result["restart_required"] = True


async def reload_pinned(project_dir: Path, keys: Iterable[str], result: dict) -> None:
    """Apply the config update for each reload target the pinned *keys* belong to, once per target."""
    reloaded: set[str] = set()
    for key in keys:
        setting = reload_key(key)
        target = match_reload_target(setting) or setting
        if target not in reloaded:
            reloaded.add(target)
            await apply_config_update(project_dir, setting, result)


# Model map helpers.


async def llm_catalog(project_dir: Path) -> list[str] | None:
    """The LLM-layer model IDs ``GET /models/available`` lists, or None when there is no real list (the curated
    fallback stands in for an unreachable provider, so it does not name this deployment's models)."""
    from sova.dashboard.services import models_service

    try:
        data = await models_service.get_available_models(project_dir, layer="llm")
    except Exception:  # noqa: BLE001 (no list means Pin leaves an ambiguous short name alone)
        log.debug("settings.models.catalog_unavailable", exc_info=True)
        return None
    if data.get("source") == "curated":
        return None
    return [m["id"] for m in data.get("models", []) if isinstance(m, dict) and m.get("id")]


async def catalog_if_needed(short_names: list[dict], project_dir: Path | None) -> list[str] | None:
    """The model list, fetched only when one of *short_names* (``short_name_settings()`` without a list) is left
    unresolved by ``resolve_alias()``: enumerating can take seconds, and on the usual first-party setup every short
    name resolves without it."""
    if all(item["pin_to"] is not None or item["env_var"] for item in short_names):
        return None
    return await llm_catalog(project_dir or Path.cwd())


def runtime_honours_model(cfg: ProjectConfig) -> bool:
    """Whether the configured agent runtime runs the model a step passes (Codex does not)."""
    from sova.ipc.runtime import create_runtime

    try:
        return create_runtime(cfg.agent.runtime, codex=cfg.codex).honours_model_override
    except Exception:  # noqa: BLE001 (an unbuildable runtime is reported elsewhere; assume the default behavior)
        return True


async def get_model_map_payload(project_dir: Path | None) -> dict:
    """Which model each role and step runs on and why, with fallbacks, and the settings stored as a short name."""
    from sova.dashboard.services.model_map_service import build_model_map, short_name_settings

    cfg = await asyncio.to_thread(load_config, project_dir)
    honours = runtime_honours_model(cfg)
    payload = build_model_map(cfg, runtime_honours_model=honours)
    payload["runtime"] = {"name": cfg.agent.runtime, "honours_model": honours}
    short_names = short_name_settings(cfg)
    catalog = await catalog_if_needed(short_names, project_dir)
    payload["short_names"] = short_names if catalog is None else short_name_settings(cfg, catalog)
    return payload


async def pin_catalog(project_dir: Path | None) -> list[str] | None:
    """The model list ``pin_short_names()`` needs to resolve an ambiguous short name, or None.

    None also covers a config that cannot be loaded (logged here): ``pin_short_names()`` reports that as its own
    refusal, a 503 since a config the server cannot read is not a fault in the request.
    """
    from sova.dashboard.config_errors import CONFIG_LOAD_ERRORS
    from sova.dashboard.services.model_map_service import short_name_settings

    try:
        cfg = await asyncio.to_thread(load_config, project_dir)
    except CONFIG_LOAD_ERRORS:  # pin_short_names() reports an unloadable config as a 503
        log.warning("settings.pin.catalog_config_unloadable", exc_info=True)
        return None
    return await catalog_if_needed(short_name_settings(cfg), project_dir)
