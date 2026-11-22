"""The user's default model settings for new projects: ``~/.config/sova/model-profile.json``.

"Save as default for new projects" copies the model settings a project has stored into one file beside the
project registry. ``sova install`` (which ``sova setup`` runs) and the dashboard setup wizard then insert the ones
a new project has no stored row for, so a second machine or project starts on the models the user already chose.

The file holds model names only, as ``{dotted setting key: value}``: no credentials, provider or runtime
selection. Only rows the project stores are saved, never a resolved built-in default, so a later change to a
default still reaches projects that never chose a value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from sova.config.db_loader import insert_setting_if_absent, read_stored_settings
from sova.config.loader import invalidate_config_cache, toml_layer
from sova.config.models import RolesConfig
from sova.dashboard.services.model_map_service import ROUTING_KEYS, is_short_name
from sova.dashboard.services.settings_service import model_id_error, routing_task, unreadable_database_error
from sova.db.models import ProjectSetting
from sova.db.session import get_session
from sova.utils.files import read_json_or_none, write_text_atomic
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.model_profile")

_PROFILE_NAME = "model-profile.json"
_ROUTING_KEY = "llm.routing"
_DISPLAY_PATH = "~/.config/sova/model-profile.json"
# Every role with a fallback list (the runtime pin ``developer_runtime_model`` names a runtime catalog, not a role).
_ROLES = tuple(
    name.removesuffix("_fallback_models") for name in RolesConfig.model_fields if name.endswith("_fallback_models")
)

_MODEL_KEYS = frozenset({"agent.model", "oversight.analysis_model", *(f"roles.{role}_model" for role in _ROLES)})
_MODEL_LIST_KEYS = frozenset({"agent.fallback_models", *(f"roles.{role}_fallback_models" for role in _ROLES)})


def profile_path() -> Path:
    """The profile file, beside ``projects.json``. Raises ``RuntimeError`` when the home directory is unknown."""
    return Path.home() / ".config" / "sova" / _PROFILE_NAME


def _usable(key: str, value: object) -> bool:
    """True when *key* is a setting a profile may hold and *value* has the shape that setting stores."""
    if key in _MODEL_KEYS:
        return model_id_error(value) is None
    if key in _MODEL_LIST_KEYS:
        return isinstance(value, list) and all(model_id_error(item) is None for item in value)
    return routing_task(key) in ROUTING_KEYS and model_id_error(value) is None


def load_model_profile() -> tuple[dict[str, Any], str | None]:
    """The usable settings in the profile (``{}`` when there is none) and a warning naming what was skipped.

    Never raises. A missing file is the normal case and has no warning. An unreadable or malformed one, and an
    entry a profile may not hold (a key outside the allowed set, a task name no longer in ``TASK_TYPE_KEYS``, a
    value of the wrong shape), is logged with the file path, skipped and named in the warning, so a bad profile
    never stops an install but never passes for an empty one either.
    """
    try:
        path = profile_path()
        if not path.exists():
            return {}, None
    except (OSError, RuntimeError):
        log.warning("model_profile.path_unavailable", exc_info=True)
        return {}, "The model profile location could not be resolved, so it was not applied"
    data = read_json_or_none(path)
    if not isinstance(data, dict):
        log.warning("model_profile.unreadable", path=str(path))
        return {}, f"Model profile {_DISPLAY_PATH} is unreadable or malformed, so it was not applied"
    valid = {key: value for key, value in data.items() if _usable(key, value)}
    # A short name (``opus``) is an unpinned alias: saving leaves it out, so a hand-edited or older file is held to
    # the same rule, and never spreads one into a new project as an explicit row.
    short = sorted(key for key, value in valid.items() if _is_short(value))
    usable = {key: value for key, value in valid.items() if key not in short}
    skipped = sorted(data.keys() - valid.keys())
    for key in (*skipped, *short):
        log.warning("model_profile.entry_skipped", path=str(path), key=key, short_name=key in short)
    notes = []
    if skipped:
        notes.append(f"skipped {', '.join(skipped)} (not a model setting or not a model ID)")
    if short:
        notes.append(f"skipped {', '.join(short)} (a short model name: pin an explicit model ID first)")
    if notes:
        return usable, f"Model profile {_DISPLAY_PATH}: {'; '.join(notes)}"
    return usable, None


def _profile_rows(stored: dict[str, Any]) -> dict[str, Any]:
    """The usable model settings among a project's stored rows, a whole-table ``llm.routing`` row expanded.

    A whole-table row replaces the per-step rows when the config is assembled, so it wins here too.
    """
    rows = dict(stored)
    table = rows.pop(_ROUTING_KEY, None)
    if isinstance(table, dict):
        rows = {key: value for key, value in rows.items() if routing_task(key) is None}
        rows.update({f"{_ROUTING_KEY}.{step}": model for step, model in table.items()})
    return {key: value for key, value in rows.items() if _usable(key, value)}


def _is_short(value: object) -> bool:
    """True when *value* is a short model name (``opus``), or a list holding one."""
    return any(is_short_name(item) for item in value) if isinstance(value, list) else is_short_name(value)


def save_model_profile(project_dir: Path | None) -> dict:
    """Save the model settings *project_dir* stores as the default for new projects.

    Refused, with an error result and nothing written, when the project stores none of them (an empty profile
    would only hide the built-in defaults), when the project's rows cannot be read, and when a profile file
    exists but cannot be read (it is not replaced behind the user's back: ``unreadable_profile``). A home
    directory that is unknown or not writable is an error too (``unavailable``). A short model name (``opus``,
    often a default an older install seeded, never a choice) is left out, since it would spread an unpinned alias
    to every new project: the result names it in ``skipped`` and a ``warning`` to pin it first. Returns
    ``{"status": "ok", "path", "keys"}`` plus those.
    """
    stored = read_stored_settings(project_dir)
    if stored is None:
        return unreadable_database_error(project_dir) or {
            "error": "The stored values could not be read",
            "unavailable": True,
        }
    usable = _profile_rows(stored)
    skipped = sorted(key for key, value in usable.items() if _is_short(value))
    rows = {key: value for key, value in usable.items() if key not in skipped}
    if not rows:
        hint = (
            f"only short model names are stored ({', '.join(skipped)}), so pin an explicit model ID first"
            if skipped
            else "choose a model first (Settings, Models view)"
        )
        return {"error": f"This project stores none of the model settings a default profile holds: {hint}"}
    try:
        path = profile_path()
        if path.exists() and not isinstance(read_json_or_none(path), dict):
            return {
                "error": f"Model profile {_DISPLAY_PATH} is unreadable or malformed; fix or remove it, then retry",
                "unreadable_profile": True,
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(rows, indent=2, sort_keys=True) + "\n")
    except (OSError, RuntimeError):
        log.warning("model_profile.save_failed", exc_info=True)
        return {"error": "The model profile could not be written; see the server log", "unavailable": True}
    result: dict[str, Any] = {"status": "ok", "path": _DISPLAY_PATH, "keys": sorted(rows)}
    if skipped:
        result["skipped"] = skipped
        result["warning"] = (
            f"Not saved: {', '.join(skipped)} (a short model name, so pin an explicit model ID first, "
            "Settings Models view, Pin)"
        )
    return result


def _is_profile_key(key: str) -> bool:
    """True when *key* is a setting a profile may hold, whatever its stored value (``llm.routing`` included)."""
    return key in _MODEL_KEYS or key in _MODEL_LIST_KEYS or key == _ROUTING_KEY or routing_task(key) is not None


async def keep_stored_model_settings(session: AsyncSession, config_dict: dict, *, replace_agent_model: bool) -> bool:
    """Carry the model settings the project stores into *config_dict* (from ``generate_config_dict()``).

    Saving a config section deletes every stored row under it that the dict does not name, and the wizard
    rewrites ``agent`` and ``roles``: without this a reconfigure would drop the project's own
    ``agent.fallback_models`` and ``roles.<role>_model`` rows, and the profile would then refill them. A row in a
    section the dict does not name is untouched by the save, so it needs no carrying. With *replace_agent_model*
    (the wizard named a model) the dict's ``agent.model`` wins over the stored one.

    Returns True when the project stored any profile setting before this configure. The caller then leaves the
    profile unapplied: a project that already chose models is never filled from the user-level profile, which
    would also bring back a route the user cleared.
    """
    from sova.db.models import ProjectSetting

    found = False
    for key, raw in (await session.execute(select(ProjectSetting.key, ProjectSetting.value))).all():
        if not _is_profile_key(key):
            continue
        found = True
        section, _, name = key.partition(".")
        target = config_dict.get(section)
        if not isinstance(target, dict) or (key == "agent.model" and replace_agent_model):
            continue
        try:
            target[name] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("model_profile.stored_value_unreadable", key=key)
    return found


def _sets(layer: dict[str, Any], key: str) -> bool:
    """True when the nested *layer* holds a value at the dotted *key*."""
    node: object = layer
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


async def apply_model_profile(project_dir: Path) -> dict:
    """Insert the profile's settings *project_dir* has no value for, in one transaction.

    Insert-only: a key with a row, even one holding the built-in default, is left alone, and so is a row another
    process inserts in between (the insert is skipped, never turned into an update). A whole-table
    ``llm.routing`` row counts as a row for every route. A key the project's ``sova.toml`` sets is left alone
    too: a row would override it. No profile is not an error. Returns ``{"applied": [keys]}`` plus a ``warning``
    when the profile, or an entry of it, was skipped, or ``{"error": ...}`` when ``sova.toml`` could not be read
    or the database could not be written; an install reports either and goes on.
    """
    profile, warning = load_model_profile()
    noted: dict[str, str] = {"warning": warning} if warning else {}
    if not profile:
        return {"applied": [], **noted}
    try:
        toml = toml_layer(project_dir)
    except (OSError, ValueError):
        log.warning("model_profile.toml_unreadable", project_dir=str(project_dir), exc_info=True)
        return {"error": "The model profile was not applied: sova.toml could not be read"}
    profile = {key: value for key, value in profile.items() if not _sets(toml, key)}
    applied: list[str] = []
    failed: list[str] = []
    try:
        async with await get_session(project_dir=project_dir) as session, session.begin():
            keys = [*profile, _ROUTING_KEY]
            stored = set(
                (await session.execute(select(ProjectSetting.key).where(ProjectSetting.key.in_(keys)))).scalars()
            )
            for key, value in profile.items():
                if key in stored or (_ROUTING_KEY in stored and routing_task(key) is not None):
                    continue
                try:
                    # A row another install commits in between is left alone; any other failure is not hidden.
                    if await insert_setting_if_absent(session, key, value):
                        applied.append(key)
                    else:
                        log.debug("model_profile.row_taken", key=key)
                except IntegrityError:
                    log.warning("model_profile.insert_failed", key=key, exc_info=True)
                    failed.append(key)
    except (OSError, RuntimeError, SQLAlchemyError):
        log.warning("model_profile.apply_failed", project_dir=str(project_dir), exc_info=True)
        return {"error": "The model profile could not be applied (database unavailable); see the server log"}
    invalidate_config_cache(project_dir)
    if failed:
        noted["warning"] = " ".join(
            part
            for part in (noted.get("warning"), f"Not applied (see the server log): {', '.join(sorted(failed))}")
            if part
        )
    return {"applied": sorted(applied), **noted}
