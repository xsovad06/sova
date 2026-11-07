"""Configuration loading for SOVA.

Supports:
- sova.toml (primary, TOML format)
- Database overrides (DB wins over TOML)
- Environment variable overrides (SOVA_ prefix, highest priority)

"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic_settings import BaseSettings

from sova.config.models import (
    ProjectConfig,
)
from sova.utils.logging import get_logger

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

logger = get_logger(component="config.loader")


@dataclass(frozen=True)
class _CacheEntry:
    fingerprint: tuple[Any, ...]
    config: ProjectConfig
    built_at: float
    # Set on a degraded entry (the last good config, or the non-DB layers, served while the
    # database is unreadable): valid for this many seconds whatever the fingerprint says,
    # so an outage costs one failed read per window instead of one per call.
    backoff_ttl: float | None = None
    # True when the config holds DB settings from an earlier successful read; False for a
    # degraded entry built from the non-DB layers only.
    has_db_layer: bool = True
    # Consecutive failed reads behind a degraded entry; the next window doubles per failure.
    failures: int = 0

    def is_fresh(self, fingerprint: tuple[Any, ...], ttl: float | None) -> bool:
        age = time.monotonic() - self.built_at
        if self.backoff_ttl is not None:
            # Ignore the DB part of the stamp (it is what is failing), but an env or TOML change
            # made during the window, including a corrected SOVA_DATABASE_URL, ends it.
            return age < self.backoff_ttl and self.fingerprint[:2] == fingerprint[:2]
        return self.fingerprint == fingerprint and (ttl is None or age < ttl)


# Process-wide, keyed by resolved project_dir. Invalidation bumps a generation so a rebuild
# that started before it cannot store its stale result: `_generation` (every project) for a
# no-argument invalidation, `_project_generation` for one project, so invalidating project A
# does not discard a concurrent rebuild of project B.
_cache: dict[Path, _CacheEntry] = {}
_cache_lock = threading.Lock()
_generation = 0
_project_generation: dict[Path, int] = {}


def _generation_of(project_dir: Path) -> tuple[int, int]:
    """The (global, per-project) generation pair. Call with ``_cache_lock`` held."""
    return (_generation, _project_generation.get(project_dir, 0))


def invalidate_config_cache(project_dir: Path | None = None) -> None:
    """Drop the cached config for *project_dir* (every project when None).

    Call after committing a settings write. For a SQLite database it also touches the
    settings stamp file, which is how another process's cache learns of the write
    (``db_loader.config_fingerprint``); a remote database relies on its short TTL.
    """
    global _generation
    from sova.config.db_loader import touch_settings_stamp

    if project_dir is not None:
        touch_settings_stamp(project_dir.resolve())
    with _cache_lock:
        if project_dir is None:
            _generation += 1
            _cache.clear()
        else:
            key = project_dir.resolve()
            _project_generation[key] = _project_generation.get(key, 0) + 1
            _cache.pop(key, None)


def load_config(project_dir: Path | None = None) -> ProjectConfig:
    """Load project configuration with priority: env > DB > TOML > defaults.

    Cached per project and validated against a cheap staleness fingerprint, so a
    request path pays for the TOML parse and the sync DB read only after a write.
    Every call returns its own copy: mutating it never changes what the next
    caller sees.
    """
    if project_dir is None:
        project_dir = Path.cwd()

    project_dir = project_dir.resolve()

    from sova.config.db_loader import (
        config_fingerprint,
        db_failure_backoff,
        db_read_failed,
        reset_db_read_failure,
    )

    fingerprint, ttl = config_fingerprint(project_dir)
    with _cache_lock:
        entry = _cache.get(project_dir)
        generation = _generation_of(project_dir)
    if entry is not None and entry.is_fresh(fingerprint, ttl):
        return entry.config.model_copy(deep=True)

    reset_db_read_failure()
    config = _build_config(project_dir)
    if db_read_failed():
        # Back off: keep serving the last good config (or, with none, the non-DB layers) for one
        # window before the next read attempt, so an outage does not block every caller on a
        # connect timeout and log a warning per call.
        # The fingerprint leads with (env, toml). When either moved, the last good config
        # predates that change, so serve the fresh build (it lacks the DB layer) instead:
        # a TOML edit or SOVA_* override must not be frozen out for the backoff window.
        # A previous degraded entry with no DB layer is not a last good config: serving it would
        # log that stored settings are applied when they are not.
        has_db_layer = False
        if entry is not None and entry.has_db_layer and entry.fingerprint[:2] == fingerprint[:2]:
            logger.warning("loader.db_unreachable_serving_last_good_config", project_dir=str(project_dir))
            served = entry.config
            has_db_layer = True
        else:
            logger.warning("loader.db_unreachable_settings_not_applied", project_dir=str(project_dir))
            served = config
        failures = (entry.failures if entry is not None else 0) + 1
        degraded = _CacheEntry(
            fingerprint,
            served.model_copy(deep=True),
            time.monotonic(),
            db_failure_backoff(failures),
            has_db_layer,
            failures,
        )
        _store(project_dir, generation, degraded)
        return served.model_copy(deep=True)

    _store(project_dir, generation, _CacheEntry(fingerprint, config.model_copy(deep=True), time.monotonic()))
    return config


def _store(project_dir: Path, generation: tuple[int, int], entry: _CacheEntry) -> None:
    """Cache *entry* unless an invalidation ran since *generation* was read."""
    with _cache_lock:
        if _generation_of(project_dir) == generation:
            _cache[project_dir] = entry


def _build_config(project_dir: Path) -> ProjectConfig:
    """Read every layer from disk, DB and environment (the uncached load)."""
    toml_path = project_dir / "sova.toml"
    toml_flat: dict[str, Any] = {}
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        toml_flat = _flatten_toml(data)
        _migrate_deprecated_keys(toml_flat)

    from sova.config.db_loader import _is_db_confirmed_empty, _try_load_from_db

    db_overrides = _try_load_from_db(project_dir)

    # Outer check for fast path (avoid transaction overhead when DB is non-empty or unreachable).
    # Inner check inside transaction (via only_if_empty=True) provides atomicity.
    if toml_flat and db_overrides is None and _is_db_confirmed_empty(project_dir):
        _auto_migrate_toml_to_db(project_dir, toml_flat)
        db_overrides = _try_load_from_db(project_dir)
        if db_overrides is not None:
            logger.debug("loader.auto_migrated_to_db")

    if db_overrides:
        merged = _deep_merge(toml_flat, db_overrides)
    else:
        merged = toml_flat

    _apply_env_overrides(merged)

    try:
        return ProjectConfig(**merged) if merged else ProjectConfig()
    except ValidationError as exc:
        raise RuntimeError(_format_config_error(exc)) from exc


def _format_config_error(exc: ValidationError) -> str:
    """Render a pydantic.ValidationError as a human-readable message.

    Callers (dashboard startup, roles, CLI, `sova doctor`) all treat
    RuntimeError as the typed failure contract for config problems (see
    docs/model-selection-architecture.md Q6), so a raw ValidationError must
    never escape load_config().
    """
    lines = [f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()]
    return "Invalid configuration:\n" + "\n".join(lines)


_NESTED_SECTIONS = (
    "llm",
    "task_source",
    "agent",
    "codex",
    "review",
    "develop",
    "validation",
    "ci",
    "confidence",
    "watch",
    "worktree",
    "commit",
    "triage",
    "roles",
    "pipeline",
    "pipelines",
    "spec",
    "notification",
    "server",
    "external_reviews",
    "ldap",
    "egress",
    "security",
    "dashboard",
    "mcp",
    "testing",
    "output",
    "feed",
    "monitoring",
    "knowledge",
    "integration",
    "rtk",
    "compression",
    "coderabbit_quota",
    "pr_monitor",
    "supervisor",
    "runaway",
    "memory_guard",
    "network_guard",
    "recovery",
    "watchdog",
    "telemetry",
    "fleet",
    "awareness",
    "oversight",
    "a2a",
    "conflict_resolution",
    "dependency_health",
)


def _flatten_toml(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested TOML sections into ProjectConfig fields.

    Maps TOML sections like [task_source], [agent], [review] to their
    corresponding nested Pydantic models.
    """
    result: dict[str, Any] = {}

    # Top-level [project] section maps to root fields
    if "project" in data:
        for key, value in data["project"].items():
            result[key] = value

    # Nested sections map to their config models
    for section in _NESTED_SECTIONS:
        if section in data:
            result[section] = data[section]

    # Migrate deprecated [resources] section into [memory_guard]
    if "resources" in data and isinstance(data["resources"], dict):
        guard = result.setdefault("memory_guard", {})
        if isinstance(guard, dict):
            key_map = {
                "memory_block_threshold_gb": "block_threshold_gb",
                "memory_warn_threshold_gb": "warn_threshold_gb",
            }
            for old_key, new_key in key_map.items():
                if old_key in data["resources"] and new_key not in guard:
                    guard[new_key] = data["resources"][old_key]

    # Root-level keys that don't belong to a [section] map to ProjectConfig fields.
    # Filter to known fields to avoid validation errors from typos or custom keys.
    known_fields = ProjectConfig.model_fields
    for key, value in data.items():
        if key not in result and key not in _NESTED_SECTIONS and key != "project":
            if key in known_fields:
                result[key] = value

    return result


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge overrides into base. Lists are replaced wholesale, not merged."""
    result = dict(base)
    for key, override_val in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(override_val, dict):
            result[key] = _deep_merge(result[key], override_val)
        else:
            result[key] = override_val
    return result


def _apply_env_overrides(merged: dict[str, Any]) -> None:
    """Make env vars beat merged TOML/DB values, for top-level fields and every section.

    Must run AFTER all merges (TOML + DB) so env vars have highest priority. A
    top-level ``SOVA_<FIELD>`` replaces the merged value. A nested
    ``SOVA_<SECTION>_<FIELD>`` (the section model's own ``env_prefix``) instead
    drops that field from the merged section dict, so the section's Pydantic
    Settings reads it from the environment with its native parsing (JSON for list
    and dict fields). Left in the dict, the stored value would reach the section
    as an init kwarg and win over the env var: that is how ``SOVA_AGENT_MODEL``
    and ``SOVA_LLM_ROUTING`` were silently ignored whenever the database held a
    value for the same field. Nested sub-sections (``review.panel`` via
    ``SOVA_REVIEW_PANEL_*``, ``mcp.atlassian`` via ``SOVA_MCP_ATLASSIAN_*``) are
    handled the same way, each with its own ``env_prefix``.
    """
    prefix = "SOVA_"
    known_fields = ProjectConfig.model_fields
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        field_name = env_key[len(prefix) :].lower()
        if field_name in known_fields:
            merged[field_name] = env_val

    # An empty variable is unset: it must not discard the stored value for a blank override.
    _drop_env_shadowed(merged, ProjectConfig, {key.upper() for key, val in os.environ.items() if val})


def _drop_env_shadowed(stored: dict[str, Any], settings_model: type[BaseSettings], env_keys: set[str]) -> None:
    """Remove stored section fields (recursively, for nested sub-sections) that an env var names."""
    for name, field in settings_model.model_fields.items():
        value = stored.get(name)
        model = field.annotation
        if not isinstance(value, dict) or not (isinstance(model, type) and issubclass(model, BaseSettings)):
            continue
        section_prefix = str(model.model_config.get("env_prefix", "")).upper()
        # Replaced, not edited: the section dict may be shared with the TOML layer.
        value = {key: val for key, val in value.items() if f"{section_prefix}{key}".upper() not in env_keys}
        stored[name] = value
        _drop_env_shadowed(value, model, env_keys)


def _migrate_deprecated_keys(flat: dict[str, Any]) -> None:
    """Migrate deprecated config keys to their replacements."""
    commit = flat.get("commit")
    if isinstance(commit, dict) and "no_ai_coauthor" in commit:
        old_val = commit.pop("no_ai_coauthor")
        if "ai_coauthor" not in commit:
            commit["ai_coauthor"] = not old_val

    spec = flat.get("spec")
    if isinstance(spec, dict) and "auto_approve_threshold" in spec:
        old_val = spec.pop("auto_approve_threshold")
        if "auto_approve_simple" not in spec:
            spec["auto_approve_simple"] = old_val not in ("never", "none")

    pipeline = flat.get("pipeline")
    if isinstance(pipeline, dict):
        # Removed: the auto-retry system these fields configured was deleted in
        # v0.x. No replacement; a stale sova.toml value is simply dropped.
        pipeline.pop("max_retries", None)
        pipeline.pop("retry_delay_seconds", None)


def _auto_migrate_toml_to_db(project_dir: Path, toml_flat: dict[str, Any]) -> None:
    """Auto-migrate TOML config to DB on first load (one-time, silent).

    The emptiness check is performed atomically inside the write transaction
    by _save_config_to_db_sync(only_if_empty=True), preventing race conditions
    where another process inserts a row between the check and write.
    """
    from sova.config.db_loader import _save_config_to_db_sync

    try:
        _save_config_to_db_sync(project_dir, toml_flat, only_if_empty=True)
    except Exception:  # noqa: BLE001 (auto-migration is best-effort; the file config stays authoritative)
        logger.warning("loader.auto_migration_failed", exc_info=True)
