"""Shared diagnostic checks for `sova doctor` and the dashboard health panel.

Every check function returns one or more DiagnosticCheck rows. run_all_checks()
runs them all, in the order `sova doctor` has always used, and never raises:
the checks that reach out to config, the database or a provider catch their own
exceptions, and `_guarded()` is the backstop for the rest (the plain
`shutil.which`/`Path` ones), so a single broken check costs one failed row
rather than the whole report.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import platform
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sova.utils.files import read_text_or_none
from sova.utils.logging import get_logger
from sova.utils.shell import ShellResult, check_git_identity, run

if TYPE_CHECKING:
    from sova.config.models import LLMConfig, ProjectConfig
    from sova.llm.models import ModelInfo

log = get_logger(component="diagnostics")

_EMPTY = "(empty)"
_CLAUDE_DIR = ".claude"

# Check-row labels that a check function and run_all_checks()'s _guarded()
# call both have to name: the label _guarded() reports a crashed check under
# has to be the same row the check itself would have produced, or a failure
# shows up as an unfamiliar extra row instead of the check that broke.
_LABEL_GIT_IDENTITY = "git identity"
_LABEL_GH_CLI = "gh CLI"
_LABEL_CLAUDE_CLI = "claude CLI"
_LABEL_GIT_HOOKS = "git hooks"
_LABEL_LLM_PROVIDER = "llm provider"
_LABEL_CONFIGURED_MODELS = "configured models"
_LABEL_AGENT_RUNTIME = "agent runtime"
_LABEL_DATABASE_SCHEMA = "database schema"
_LABEL_AWARENESS_PROVIDERS = "awareness providers"

# Bounds each awareness provider health check so a single hung provider
# (e.g. a stalled Gmail or Calendar network call) can't block the caller
# indefinitely.
_AWARENESS_HEALTH_CHECK_TIMEOUT = 15


@dataclass(frozen=True)
class DiagnosticCheck:
    """One diagnostic check result."""

    name: str
    passed: bool
    detail: str
    required: bool


def _check_python_version() -> DiagnosticCheck:
    """Check Python >= 3.12."""
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 12)
    return DiagnosticCheck("Python >= 3.12", py_ok, py_version, True)


async def _check_git() -> list[DiagnosticCheck]:
    """Check git availability and version."""
    git_path = shutil.which("git")
    if git_path:
        result = await run("git", "--version")
        return [DiagnosticCheck("git", result.success, result.stdout.strip() if result.success else "error", True)]
    return [DiagnosticCheck("git", False, "not found, install git", True)]


async def _check_git_identity(project_dir: Path) -> list[DiagnosticCheck]:
    """Check git user.name and user.email are configured."""
    identity = await check_git_identity(cwd=project_dir)
    checks: list[DiagnosticCheck] = []
    if identity.valid:
        checks.append(DiagnosticCheck(_LABEL_GIT_IDENTITY, True, f"{identity.name} <{identity.email}>", True))
    else:
        missing = ", ".join(identity.missing_fields)
        checks.append(
            DiagnosticCheck(
                _LABEL_GIT_IDENTITY,
                False,
                f"missing {missing}. Set with: git config user.name / user.email",
                True,
            )
        )
    return checks


async def _check_gh_cli() -> list[DiagnosticCheck]:
    """Check GitHub CLI availability, version, and authentication."""
    checks: list[DiagnosticCheck] = []
    gh_path = shutil.which("gh")
    if not gh_path:
        checks.append(DiagnosticCheck(_LABEL_GH_CLI, False, "not found. Install: https://cli.github.com/", True))
        checks.append(DiagnosticCheck("gh authenticated", False, "gh CLI not installed", True))
        return checks

    result = await run("gh", "--version")
    version_line = result.stdout.strip().split("\n")[0] if result.success and result.stdout else "error"
    checks.append(DiagnosticCheck(_LABEL_GH_CLI, result.success, version_line, True))

    auth_result = await run("gh", "auth", "status")
    auth_ok = auth_result.success
    auth_detail = _extract_auth_detail(auth_result, auth_ok)
    checks.append(DiagnosticCheck("gh authenticated", auth_ok, auth_detail, True))
    return checks


def _extract_auth_detail(auth_result: ShellResult, auth_ok: bool) -> str:
    """Extract authentication detail from gh auth status output."""
    if not auth_ok:
        return "not authenticated, run: gh auth login"
    for line in (auth_result.stdout + auth_result.stderr).splitlines():
        if "Logged in" in line or "account" in line.lower():
            return line.strip()
    return ""


async def _check_claude_cli() -> list[DiagnosticCheck]:
    """Check Claude CLI availability and version."""
    claude_path = shutil.which("claude")
    if claude_path:
        result = await run("claude", "--version")
        version = result.stdout.strip().split("\n")[0] if result.success and result.stdout else "error"
        return [DiagnosticCheck(_LABEL_CLAUDE_CLI, result.success, version, True)]
    return [
        DiagnosticCheck(
            _LABEL_CLAUDE_CLI,
            False,
            "not found, install: https://docs.anthropic.com/en/docs/claude-code",
            True,
        )
    ]


def _check_terminal_notifier() -> list[DiagnosticCheck]:
    """Check terminal-notifier on macOS (optional)."""
    if platform.system() != "Darwin":
        return []
    tn_path = shutil.which("terminal-notifier")
    detail = tn_path or "not found, install: brew install terminal-notifier"
    return [DiagnosticCheck("terminal-notifier", bool(tn_path), f"(optional) {detail}", False)]


def _check_rtk() -> list[DiagnosticCheck]:
    """Check RTK availability (optional context compression)."""
    rtk_path = shutil.which("rtk")
    detail = rtk_path or "not found, optional context compression tool"
    return [DiagnosticCheck("rtk", bool(rtk_path), f"(optional) {detail}", False)]


async def _check_git_hooks(project_dir: Path) -> DiagnosticCheck:
    """Check git hooks configuration.

    Required when .githooks/ exists: invariants are silently bypassed otherwise.
    """
    githooks_dir = project_dir / ".githooks"
    if not githooks_dir.is_dir():
        return DiagnosticCheck(_LABEL_GIT_HOOKS, True, "no .githooks/ directory (not applicable)", False)

    hooks_result = await run("git", "config", "--get", "core.hooksPath", cwd=str(project_dir))
    hooks_path = hooks_result.stdout.strip() if hooks_result.success else ""
    hooks_ok = hooks_path == ".githooks"
    if hooks_ok:
        hooks_detail = hooks_path
    elif hooks_path:
        hooks_detail = f"set to '{hooks_path}' (expected '.githooks'), run: git config core.hooksPath .githooks"
    else:
        hooks_detail = "not set, run: git config core.hooksPath .githooks"
    return DiagnosticCheck(_LABEL_GIT_HOOKS, hooks_ok, hooks_detail, True)


def _check_sova_config(project_dir: Path) -> list[DiagnosticCheck]:
    """Check project configuration (DB or sova.toml)."""
    checks: list[DiagnosticCheck] = []

    from sova.config.db_loader import _try_load_from_db

    db_config = _try_load_from_db(project_dir)
    toml_path = project_dir / "sova.toml"
    toml_exists = toml_path.exists()

    has_config = db_config is not None or toml_exists
    if db_config is not None:
        source = "database"
    elif toml_exists:
        source = str(toml_path)
    else:
        source = f"not found (checked DB and {toml_path})"
    checks.append(DiagnosticCheck("config", has_config, source, False))

    if toml_exists and db_config is not None:
        checks.append(
            DiagnosticCheck("legacy sova.toml", False, "exists alongside DB config (DB takes priority)", False)
        )

    if has_config:
        checks.extend(_validate_sova_config(project_dir))

    return checks


def _validate_sova_config(project_dir: Path) -> list[DiagnosticCheck]:
    """Validate sova.toml configuration details."""
    checks: list[DiagnosticCheck] = []
    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        ts_type = cfg.task_source.type
        checks.append(DiagnosticCheck("task_source.type", True, ts_type, False))

        if ts_type == "github":
            checks.extend(_check_github_config(cfg))
        elif ts_type == "jira":
            checks.extend(_check_jira_config(cfg))
    except Exception as exc:  # noqa: BLE001 (diagnostic check reports any failure as a failed check row)
        checks.append(DiagnosticCheck("config valid", False, str(exc)[:80], False))
    return checks


def _check_github_config(cfg: ProjectConfig) -> list[DiagnosticCheck]:
    """Check GitHub-specific configuration fields."""
    return [
        DiagnosticCheck("github_repo configured", bool(cfg.github_repo), cfg.github_repo or _EMPTY, False),
        DiagnosticCheck("github_user configured", bool(cfg.github_user), cfg.github_user or _EMPTY, False),
    ]


def _check_jira_config(cfg: ProjectConfig) -> list[DiagnosticCheck]:
    """Check Jira-specific configuration fields."""
    ts = cfg.task_source
    return [
        DiagnosticCheck("jira_base_url", bool(ts.jira_base_url), ts.jira_base_url or _EMPTY, False),
        DiagnosticCheck("jira_email", bool(ts.jira_email), ts.jira_email or _EMPTY, False),
        DiagnosticCheck("jira_api_token", bool(ts.jira_api_token), "(set)" if ts.jira_api_token else _EMPTY, False),
        DiagnosticCheck("jira_project_key", bool(ts.jira_project_key), ts.jira_project_key or _EMPTY, False),
    ]


async def _check_llm_provider(project_dir: Path) -> list[DiagnosticCheck]:
    """Check configured LLM provider availability."""
    checks: list[DiagnosticCheck] = []
    try:
        from sova.config.loader import load_config
        from sova.llm.provider import create_provider

        cfg = load_config(project_dir)
        provider_type = cfg.llm.provider
        try:
            provider = create_provider(cfg.llm, project_dir)
        except ValueError as exc:
            # Only ValueError from create_provider (unknown provider type).
            # pydantic.ValidationError also inherits ValueError in v2 but
            # load_config() is called outside this inner try, so it won't
            # be caught here.
            checks.append(DiagnosticCheck(_LABEL_LLM_PROVIDER, False, str(exc), True))
            return checks

        available, detail = await provider.check_available()
        checks.append(DiagnosticCheck(_LABEL_LLM_PROVIDER, available, f"{provider_type}: {detail}", True))
    except Exception as exc:  # noqa: BLE001 (diagnostic check reports any failure as a failed check row)
        checks.append(DiagnosticCheck(_LABEL_LLM_PROVIDER, False, str(exc)[:80], False))
    return checks


def _check_keyring_secrets(project_dir: Path) -> list[DiagnosticCheck]:
    """Report a sentinel-without-entry mismatch for any keyring-backed secret.

    Only emits a row when something is actionable: a secret's database value
    is the keyring sentinel but no matching OS keychain entry can be read
    back (deleted from the keychain, or the database was copied to another
    machine without it). A healthy resolution (or a secret that was never
    moved to the keyring) is silent.
    """
    checks: list[DiagnosticCheck] = []
    try:
        from sova.config.loader import load_config
        from sova.dashboard.services.settings_service import get_secret_locations
        from sova.llm import keyring_store

        cfg = load_config(project_dir)
        for key, location in get_secret_locations(project_dir).items():
            if location != "keyring":
                continue
            # Secrets are written to the per-(project, provider) scoped name
            # (see scoped_secret_name()), not the bare key: a bare-key lookup
            # here would only ever find the legacy unscoped entry, reporting
            # a mismatch for every secret resolved through the scoped path
            # even though it resolves correctly. A key with a fixed scope (the
            # ntfy token) does not depend on the LLM provider.
            scope = keyring_store.fixed_scope(key) or cfg.llm.provider
            scoped = keyring_store.scoped_secret_name(key, project_dir, scope)
            if keyring_store.get_secret(scoped) or keyring_store.get_secret(key):
                continue
            checks.append(
                DiagnosticCheck(
                    f"keyring: {key}",
                    False,
                    "sentinel present in the database but no matching OS keychain entry was found",
                    False,
                )
            )
    except Exception as exc:  # noqa: BLE001 (diagnostic check reports any failure as a failed check row)
        checks.append(DiagnosticCheck("keyring secrets", False, str(exc)[:80], False))
    return checks


async def _check_ollama(project_dir: Path) -> list[DiagnosticCheck]:
    """Check Ollama availability when routing config references ollama/ models."""
    checks: list[DiagnosticCheck] = []
    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        # Aliases and the direct model fields are scanned alongside routing:
        # provider = "ollama" is the first-class way to point straight at a
        # local model via llm.model/llm.fallback_model (no routing entry
        # needed), so checking routing alone would report "no ollama models"
        # on exactly that deployment shape.
        configured = [
            *cfg.llm.routing.values(),
            *cfg.llm.model_aliases.values(),
            cfg.llm.model,
            cfg.llm.fallback_model,
        ]
        ollama_models = sorted({v for v in configured if v.startswith("ollama/")})
        if not ollama_models:
            if cfg.llm.provider == "ollama":
                # LiteLLM routes to Ollama on the model prefix, not on the SOVA
                # provider name, so an unprefixed model here reaches a different
                # vendor (or none) and silently skips every check below.
                detail = (
                    f"llm.provider='ollama' but no model starts with 'ollama/' (llm.model={cfg.llm.model or _EMPTY})"
                )
                checks.append(DiagnosticCheck("ollama model prefix", False, detail, False))
            return checks

        ollama_path = shutil.which("ollama")
        if not ollama_path:
            checks.append(DiagnosticCheck("ollama CLI", False, "not found, install: https://ollama.com/", False))
            return checks

        result = await run("ollama", "list")
        if not result.success:
            checks.append(DiagnosticCheck("ollama running", False, "not running, start with: ollama serve", False))
            return checks

        checks.append(DiagnosticCheck("ollama running", True, "connected", False))

        installed_models = set()
        for line in result.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if parts:
                installed_models.add(parts[0])

        for model in ollama_models:
            model_name = model.removeprefix("ollama/")
            # Ollama's own default: a name with no explicit tag means ":latest".
            lookup_name = model_name if ":" in model_name else f"{model_name}:latest"
            found = lookup_name in installed_models
            detail = "installed" if found else f"not pulled, run: ollama pull {model_name}"
            checks.append(DiagnosticCheck(f"ollama model: {model_name}", found, detail, False))

    except Exception as exc:  # noqa: BLE001 (diagnostic check reports any failure as a failed check row)
        checks.append(DiagnosticCheck("ollama", False, str(exc)[:80], False))
    return checks


def _configured_model_fields_by_layer(cfg: ProjectConfig) -> tuple[dict[str, str], dict[str, str]]:
    """Split ``configured_model_fields()`` into fields llm.provider serves and the runtime's own pins.

    ``agent.model`` and ``agent.fallback_models`` stay on the provider side: every text-only step
    (create_pr, commit, refresh_pr_body) sends them through ``select_model()`` to llm.provider.
    ``conflict_resolution.models`` is left out: rebase consensus builds its own LiteLLM provider per
    entry, so llm.provider's catalog says nothing about whether those IDs exist.
    """
    llm: dict[str, str] = {}
    runtime: dict[str, str] = {}
    for key, value in configured_model_fields(cfg):
        if key.startswith("conflict_resolution."):
            continue
        # roles.*_runtime_model pins reach only the runtime, which receives them verbatim.
        (runtime if key.endswith("_runtime_model") else llm)[key] = value
    if cfg.agent.runtime == "codex" and cfg.codex.model:
        runtime["codex.model"] = cfg.codex.model
    return llm, runtime


async def _configured_llm_catalog(cfg: ProjectConfig, project_dir: Path) -> list[ModelInfo] | None:
    from sova.llm.provider import CAPABILITY_PROBE_TIMEOUT, create_provider

    try:
        provider = create_provider(cfg.llm, project_dir)
        # Only the Claude CLI enumerates by running billable model turns.
        # Other providers use free listing endpoints and their shared TTL cache.
        return await asyncio.wait_for(
            provider.list_available_models(allow_probe=cfg.llm.provider != "claude-code"),
            timeout=CAPABILITY_PROBE_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 (discovery is advisory; errors must not expose credentials)
        log.debug("models.catalog_unavailable", layer="llm", error_type=type(exc).__name__)
        return None


async def _configured_runtime_catalog(cfg: ProjectConfig, project_dir: Path) -> set[str] | None:
    from sova.dashboard.services.models_service import get_available_models

    # Claude Code declares a curated dialect, and Codex echoes codex.model.
    # Neither is an independent catalog capable of identifying stale IDs.
    if cfg.agent.runtime in ("claude-code", "codex"):
        return None
    try:
        # Reuse the project-scoped cache and per-call timeout: constructing a
        # new runtime here would discard its per-instance enumeration cache.
        result = await get_available_models(project_dir, layer="runtime")
        if result["source"] != "runtime":
            return None
        return {m["id"] for m in result["models"]} or None
    except Exception as exc:  # noqa: BLE001 (one unavailable layer must not hide the other's diagnostics)
        log.debug("models.catalog_unavailable", layer="runtime", error_type=type(exc).__name__)
        return None


def _catalog_ids_for_model(model: str, catalog: list[ModelInfo] | None, cfg: LLMConfig) -> set[str] | None:
    """Use only discovered entries from the backend that will receive this ID."""
    if not catalog or any(entry.id == model and entry.source == "curated" for entry in catalog):
        return None
    discovered = [entry for entry in catalog if entry.source != "curated"]
    if cfg.provider in ("litellm", "hybrid", "openai", "ollama", "vertex"):
        from sova.llm.models import classify_model_family

        if model.startswith("ollama/"):
            discovered = [entry for entry in discovered if entry.source == "ollama"]
        elif model.startswith("vertex_ai/") or cfg.provider == "vertex":
            # A failed Vertex publisher must not be answered by another
            # publisher's successful catalog in the same enumeration pass.
            family = classify_model_family(model.removeprefix("vertex_ai/"))
            discovered = [entry for entry in discovered if entry.source == "vertex" and entry.family == family]
        else:
            discovered = [entry for entry in discovered if entry.source == "openai_compatible"]
    return {entry.id for entry in discovered} if discovered else None


def _catalog_lookups(model: str) -> list[str]:
    """Return the spellings under which a catalog may list *model*, exact ID first."""
    lookups = [model]
    if model.startswith("ollama/") and ":" not in model.rsplit("/", 1)[-1]:
        # Ollama's own default: a name with no explicit tag means ":latest".
        lookups.append(f"{model}:latest")
    elif model.startswith(("vertex_ai/", "openai/")):
        lookups.append(model.split("/", 1)[1])
    # A Vertex publisher catalog lists the model, not its @-pinned snapshot
    # (claude-sonnet-4-5@20250929 is listed as claude-sonnet-4-5).
    lookups.extend(lookup.split("@", 1)[0] for lookup in list(lookups) if "@" in lookup)
    return lookups


def _provider_model(value: str, llm: LLMConfig, *, follow_aliases: bool = True) -> str:
    """Return the ID llm.provider receives for *value*: client-side alias resolution, then the provider's own.

    ``follow_aliases=False`` skips the ``llm.model_aliases`` lookup and keeps only the tier correction
    and provider normalization: an alias target is the value a field is *mapped to*, and the real resolver
    never takes a second alias hop from it.

    ``AnthropicAPIProvider.invoke()`` runs ``normalize_model_name()`` (``resolve_model_alias``) after
    ``resolve_alias()``, so a bare tier such as ``sonnet`` works there even when ``resolve_alias()``
    left it unexpanded (a mapped alias, or ``llm.resolve_tier_aliases = false``).
    """
    from sova.llm.client import resolve_alias
    from sova.llm.models import resolve_model_alias

    model = resolve_alias(value, llm if follow_aliases else llm.model_copy(update={"model_aliases": {}}))
    return resolve_model_alias(model) if llm.provider == "anthropic" else model


def _alias_is_active(key: str, llm: LLMConfig) -> bool:
    """True when ``llm.model_aliases[key]`` is the entry resolve_alias() looks up on this backend.

    resolve_alias() prefers ``"{backend}:{name}"`` over the bare ``name``; a key it never consults
    (another backend's scope, or a bare key shadowed by a scoped one) is never used.
    """
    from sova.llm.backends import Backend, detect_backend

    scope, sep, alias = key.partition(":")
    if not sep or scope not in {backend.value for backend in Backend}:
        # An unscoped key may itself contain a colon (an Ollama tag such as qwen3:8b).
        alias = key
    scoped_key = f"{detect_backend(llm).value}:{alias}"
    return key == scoped_key or (key == alias and scoped_key not in llm.model_aliases)


_DATED_SUFFIX = re.compile(r"-\d{8}")
_FLOATING_ALIAS_SUFFIXES = ("-latest", "-0")


def _dated_bases(lookup: str) -> list[str]:
    """Return the prefixes a dated catalog ID may extend to be the same model as *lookup*.

    The Anthropic API serves ``claude-sonnet-4-0`` and ``claude-3-5-haiku-latest`` as floating aliases of a
    dated ID (``claude-sonnet-4-20250514``), so those drop their suffix before the date is matched.
    """
    bases = [lookup]
    bases.extend(lookup.removesuffix(suffix) for suffix in _FLOATING_ALIAS_SUFFIXES if lookup.endswith(suffix))
    return bases


def _configured_model_row(
    field: str, model: str, ids: set[str] | None, lookups: list[str], identity: str, *, allow_dated: bool = False
) -> DiagnosticCheck | None:
    """Return *field*'s row, or None when no discovered catalog can settle *model* either way.

    ``allow_dated`` also accepts a catalog ID that is a lookup plus an 8-digit date suffix: the
    Anthropic API lists ``claude-sonnet-4-5-20250929`` yet serves the undated ``claude-sonnet-4-5``, and
    the ``-0`` and ``-latest`` aliases (``claude-sonnet-4-0``) resolve the same way.
    """
    if ids is None:
        return None
    found = any(
        lookup in ids
        or (
            allow_dated
            and any(
                i.startswith(base) and _DATED_SUFFIX.fullmatch(i[len(base) :])
                for base in _dated_bases(lookup)
                for i in ids
            )
        )
        for lookup in lookups
    )
    detail = f"{identity}: {model} " + ("available" if found else "not found in discovered catalog")
    return DiagnosticCheck(f"model: {field}", found, detail, False)


async def _check_configured_models(project_dir: Path) -> list[DiagnosticCheck]:
    """Warn about configured model IDs absent from a discovered catalog, never about unverifiable ones.

    One row per field a real catalog settles, warn-level when the ID is missing. Fields no catalog can
    settle (curated or unreachable layer, transient failure, another backend's ID) share one
    informational row, so a deployment whose provider cannot enumerate sees one note rather than a
    warning per field. A backend-scoped alias inactive on this backend is never used, so it gets no row.
    """
    from sova.config.loader import load_config
    from sova.llm.client import resolve_bare_tier_for_provider

    try:
        cfg = await asyncio.to_thread(load_config, project_dir)
    except Exception:  # noqa: BLE001 (the config check already reports an unloadable config; stay silent here)
        return []
    llm_fields, runtime_fields = _configured_model_fields_by_layer(cfg)
    llm_catalog, runtime_ids = await asyncio.gather(
        _configured_llm_catalog(cfg, project_dir),
        _configured_runtime_catalog(cfg, project_dir) if runtime_fields else asyncio.sleep(0, result=None),
    )
    candidates: list[tuple[str, str, DiagnosticCheck | None]] = []
    provider_identity = f"provider {cfg.llm.provider}"
    for field, value in llm_fields.items():
        alias_key = field.removeprefix("llm.model_aliases.")
        is_alias_row = alias_key != field
        if is_alias_row and not _alias_is_active(alias_key, cfg.llm):
            continue
        model = _provider_model(value, cfg.llm, follow_aliases=not is_alias_row)
        if field == "roles.reviewer_model":
            # ReviewerRole substitutes a bare tier the same way; check what it actually sends.
            tier_model = resolve_bare_tier_for_provider(value, cfg.llm)
            if tier_model is not None:
                target, kind = tier_model
                model = target if kind == "alias" else _provider_model(target, cfg.llm)
        ids = _catalog_ids_for_model(model, llm_catalog, cfg.llm)
        candidates.append(
            (
                provider_identity,
                field,
                _configured_model_row(field, model, ids, _catalog_lookups(model), provider_identity, allow_dated=True),
            )
        )
    runtime_identity = f"runtime {cfg.agent.runtime}"
    for field, value in runtime_fields.items():
        # resolve_runtime_model_pin() hands the runtime its pin verbatim, with no alias resolution.
        candidates.append(
            (runtime_identity, field, _configured_model_row(field, value, runtime_ids, [value], runtime_identity))
        )

    rows = [row for _, _, row in candidates if row is not None]
    unchecked: dict[str, list[str]] = {}
    for identity, field, row in candidates:
        if row is None:
            unchecked.setdefault(identity, []).append(field)
    if unchecked:
        layers = "; ".join(f"{identity}: {', '.join(fields)}" for identity, fields in unchecked.items())
        rows.append(
            DiagnosticCheck(_LABEL_CONFIGURED_MODELS, True, f"not checked (catalog unknown) for {layers}", False)
        )
    return rows


async def _check_agent_runtime(project_dir: Path) -> list[DiagnosticCheck]:
    """Check configured agent runtime availability."""
    checks: list[DiagnosticCheck] = []
    try:
        from sova.config.loader import load_config
        from sova.ipc.runtime import create_runtime

        cfg = load_config(project_dir)
        runtime_type = cfg.agent.runtime
        try:
            runtime = create_runtime(runtime_type, codex=cfg.codex)
        except ValueError as exc:
            checks.append(DiagnosticCheck(_LABEL_AGENT_RUNTIME, False, str(exc), True))
            return checks

        available, detail = await runtime.check_available()
        checks.append(DiagnosticCheck(_LABEL_AGENT_RUNTIME, available, f"{runtime_type}: {detail}", True))

        # agent.max_budget is gt=0-validated (always positive), so every
        # pipeline step's dispatch_command()/dispatch_prompt() call passes a
        # non-None max_budget_usd. CodexRuntime.spawn() refuses that outright
        # unless this flag is set, which otherwise only surfaces mid-pipeline
        # on the first tool-using step, after create_worktree/capture_baseline
        # have already done their setup work. Surfacing it here lets the
        # operator fix it before ever starting a run.
        if runtime_type == "codex" and not cfg.codex.allow_unenforced_budget:
            checks.append(
                DiagnosticCheck(
                    "codex budget enforcement",
                    False,
                    "Codex CLI cannot enforce agent.max_budget; every pipeline run will fail on "
                    "its first tool-using step unless codex.allow_unenforced_budget = true is set",
                    True,
                )
            )
    except Exception as exc:  # noqa: BLE001 (diagnostic check reports any failure as a failed check row)
        checks.append(DiagnosticCheck(_LABEL_AGENT_RUNTIME, False, str(exc)[:80], False))
    return checks


async def _check_awareness_providers(project_dir: Path) -> list[DiagnosticCheck]:
    """Check configured awareness provider health (Gmail, Calendar, etc.).

    Skipped entirely when awareness.enabled is false, so projects that
    don't use the subsystem see no noise. A provider name in config that
    isn't in the registry (e.g. its optional dependency isn't installed)
    is reported separately from providers that instantiated but can't
    reach their source.
    """
    checks: list[DiagnosticCheck] = []
    try:
        from sova.awareness import create_providers
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        if not cfg.awareness.enabled:
            return checks

        if not cfg.awareness.providers:
            checks.append(DiagnosticCheck(_LABEL_AWARENESS_PROVIDERS, True, "no providers configured", False))
            return checks

        providers = create_providers(cfg.awareness)
        found_names = {p.name for p in providers}
        for name in cfg.awareness.providers:
            if name not in found_names:
                detail = "unavailable: unknown name, missing optional dependency, or init failed"
                checks.append(DiagnosticCheck(f"awareness: {name}", False, detail, False))

        # Concurrent with per-provider isolation, mirroring
        # dashboard/services/awareness_service.py:get_provider_statuses(). Each
        # check is time-boxed so one hung provider (e.g. a stalled Gmail or
        # Calendar network call) can't block the caller indefinitely.
        results = await asyncio.gather(
            *(asyncio.wait_for(p.health_check(), timeout=_AWARENESS_HEALTH_CHECK_TIMEOUT) for p in providers),
            return_exceptions=True,
        )
        for provider, result in zip(providers, results):
            if isinstance(result, BaseException):
                # Only report real failures as a check row; KeyboardInterrupt/
                # CancelledError must keep propagating, not be swallowed as a
                # provider health failure.
                if not isinstance(result, Exception):
                    raise result
                detail = str(result) or type(result).__name__
                checks.append(DiagnosticCheck(f"awareness: {provider.name}", False, detail[:80], False))
                continue
            ok, detail = result
            checks.append(DiagnosticCheck(f"awareness: {provider.name}", ok, detail, False))
    except Exception as exc:  # noqa: BLE001 (diagnostic check reports any failure as a failed check row)
        checks.append(DiagnosticCheck(_LABEL_AWARENESS_PROVIDERS, False, str(exc)[:80], False))
    return checks


def _check_install_completeness(project_dir: Path) -> list[DiagnosticCheck]:
    """Check that sova install created all expected artifacts."""
    from sova.utils.permissions import check_agent_permissions

    checks: list[DiagnosticCheck] = []
    claude_dir = project_dir / _CLAUDE_DIR

    commands_dir = claude_dir / "commands"
    if commands_dir.is_dir():
        cmd_count = len(list(commands_dir.glob("*.md")))
        checks.append(DiagnosticCheck("commands installed", cmd_count > 0, f"{cmd_count} commands", cmd_count == 0))
    else:
        checks.append(
            DiagnosticCheck("commands installed", False, ".claude/commands/ missing, run: sova install", True)
        )

    memory_dir = claude_dir / "agent-memory"
    checks.append(
        DiagnosticCheck(
            "agent memory",
            memory_dir.is_dir(),
            str(memory_dir) if memory_dir.is_dir() else "missing, run: sova install",
            False,
        )
    )

    db_path = claude_dir / "sova.db"
    checks.append(
        DiagnosticCheck("database", db_path.is_file(), str(db_path) if db_path.is_file() else "missing", True)
    )

    # Agent permissions (warning-level: agents now use bypassPermissions by default)
    has_all, missing = check_agent_permissions(claude_dir)
    if has_all:
        checks.append(DiagnosticCheck("agent permissions", True, "configured", False))
    else:
        detail = f"missing {', '.join(missing)}, run: sova install --update"
        checks.append(DiagnosticCheck("agent permissions", False, detail, False))

    checks.append(_check_settings_hooks(claude_dir / "settings.json"))

    return checks


def _check_settings_hooks(settings_path: Path) -> DiagnosticCheck:
    """Flag hook entries that make Claude Code ignore the whole settings.json.

    Every entry under a hook event must be ``{"matcher": ..., "hooks": [...]}``;
    a single entry without a ``hooks`` list makes Claude Code drop the file,
    permissions included. SOVA's RTK injection used to write its entry that
    way, and ``sova install --update`` now rewrites it; any other entry needs a
    hand edit.
    """
    name = "settings hooks"
    if not settings_path.is_file():
        return DiagnosticCheck(name, True, "no settings.json", False)

    text = read_text_or_none(settings_path)
    try:
        data = json.loads(text) if text is not None else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return DiagnosticCheck(name, False, "settings.json is unreadable or not a JSON object", False)

    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return DiagnosticCheck(name, False, '"hooks" in settings.json is not an object', False)

    invalid_events = sorted(
        event
        for event, entries in hooks.items()
        if not isinstance(entries, list)
        or any(not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list) for entry in entries)
    )
    if invalid_events:
        detail = (
            f"entries without a hooks list under {', '.join(invalid_events)}, so Claude Code ignores the whole file;"
            " run: sova install --update (fixes the RTK entry), edit any other entry by hand"
        )
        return DiagnosticCheck(name, False, detail, False)
    return DiagnosticCheck(name, True, "matcher format", False)


async def _check_database_schema(project_dir: Path) -> list[DiagnosticCheck]:
    """Compare the project database's alembic revision with the migration head.

    Warning-level, never required: ``init_db()`` migrates a database that is
    behind on the next server start, so drift here is worth surfacing but does
    not stop SOVA from working. A project with no database file yields no row,
    since the "database" check in ``_check_install_completeness`` already reports
    that and a schema comparison against nothing would only duplicate it.
    """
    checks: list[DiagnosticCheck] = []
    try:
        from sova.db.session import inspect_schema

        inspection = await inspect_schema(project_dir)
        if inspection.state == "no_tables":
            checks.append(
                DiagnosticCheck(_LABEL_DATABASE_SCHEMA, False, "database has no tables, run: sova init-db", False)
            )
        elif inspection.state == "ready":
            problems: list[str] = []
            if inspection.legacy:
                problems.append("unversioned (no alembic_version table)")
            elif inspection.head is None:
                problems.append(f"at {inspection.current}, migration head unknown")
            elif not inspection.at_head:
                problems.append(f"at {inspection.current}, head is {inspection.head}")
            if inspection.missing:
                problems.append(f"{len(inspection.missing)} ORM column(s) missing: {', '.join(inspection.missing[:3])}")
            if problems:
                checks.append(
                    DiagnosticCheck(_LABEL_DATABASE_SCHEMA, False, f"{'; '.join(problems)}, run: sova init-db", False)
                )
            else:
                checks.append(DiagnosticCheck(_LABEL_DATABASE_SCHEMA, True, f"at head ({inspection.head})", False))
    except Exception as exc:  # noqa: BLE001 (diagnostic check reports any failure as a failed check row)
        checks.append(DiagnosticCheck(_LABEL_DATABASE_SCHEMA, False, f"could not inspect: {str(exc)[:60]}", False))
    return checks


async def _guarded(label: str, check: Callable[..., Any], *args: Any) -> list[DiagnosticCheck]:
    """Run one check, turning an unexpected raise into a single failed row.

    Most checks already catch their own failures, but the ones that only touch
    `shutil.which`/`platform`/`Path` carry no internal guard, so an OSError
    reading the project dir (or a bug in a check) would otherwise abort the
    whole report: a traceback instead of the `sova doctor` table, and every
    other check's result lost on the dashboard panel. Reported as optional,
    matching what the self-guarding checks do on their own error paths, so a
    broken check never fails `sova doctor` with exit code 1.

    CancelledError and KeyboardInterrupt derive from BaseException, so they
    keep propagating rather than being recorded as a check failure.
    """
    try:
        result = check(*args)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # noqa: BLE001 (backstop; one broken check is one failed row, not a lost report)
        return [DiagnosticCheck(label, False, f"check failed: {str(exc)[:80]}", False)]
    return result if isinstance(result, list) else [result]


_CLAUDE_ALIASES = frozenset({"opus", "sonnet", "haiku"})
_LABEL_OPENAI_MODELS = "openai-only models"


def names_claude_model(value: str) -> bool:
    """True for a bare Claude alias (``sonnet``) or a Claude model ID, with or without a provider prefix."""
    lowered = value.strip().lower()
    return lowered in _CLAUDE_ALIASES or lowered.startswith("claude-") or "/claude-" in lowered or ".claude-" in lowered


def names_non_openai_model(value: str) -> bool:
    """True for a Claude alias or ID, or a Vertex or Gemini ID: anything an OpenAI-only run must not reach."""
    lowered = value.strip().lower()
    return names_claude_model(lowered) or "vertex" in lowered or "gemini" in lowered


# Model fields only the agent runtime consumes. Under a non-Codex runtime they legitimately name that
# runtime's models (Claude IDs for claude-code), so a mixed setup does not fail on them.
_RUNTIME_MODEL_KEYS = frozenset({"agent.model", "roles.developer_runtime_model"})


def configured_model_fields(cfg: ProjectConfig) -> list[tuple[str, str]]:
    """List every non-empty model field of *cfg* as (key, value), in a stable order.

    Covers the llm (routing and tier aliases included), agent, roles, oversight, confidence, review-panel and
    conflict-resolution model fields. The one enumeration of SOVA's model settings: the openai-only and
    configured-models doctor checks both filter it rather than keeping their own field list.
    """
    scalar_fields = {
        "llm.model": cfg.llm.model,
        "llm.fallback_model": cfg.llm.fallback_model,
        "agent.model": cfg.agent.model,
        "roles.researcher_model": cfg.roles.researcher_model,
        "roles.triage_model": cfg.roles.triage_model,
        "roles.reviewer_model": cfg.roles.reviewer_model,
        "roles.developer_model": cfg.roles.developer_model,
        "roles.planner_model": cfg.roles.planner_model,
        "roles.developer_runtime_model": cfg.roles.developer_runtime_model,
        "oversight.analysis_model": cfg.oversight.analysis_model,
        "confidence.model": cfg.confidence.model,
    }
    fields = list(scalar_fields.items())
    fields += [(f"agent.fallback_models[{i}]", value) for i, value in enumerate(cfg.agent.fallback_models)]
    fields += [(f"llm.routing.{task_type}", value) for task_type, value in cfg.llm.routing.items()]
    fields += [(f"llm.model_aliases.{alias}", value) for alias, value in cfg.llm.model_aliases.items()]
    fields += [(f"conflict_resolution.models[{i}]", value) for i, value in enumerate(cfg.conflict_resolution.models)]
    fields += [
        (f"review.panel.dimension_models.{dim}", value) for dim, value in cfg.review.panel.dimension_models.items()
    ]
    return [(key, value) for key, value in fields if value]


def claude_model_references(cfg: ProjectConfig, *, include_runtime_fields: bool = True) -> list[tuple[str, str]]:
    """List every resolved model field of *cfg* whose value is a Claude, Vertex or Gemini ID, as (key, value).

    Filters ``configured_model_fields()``, so an OpenAI-only run can prove no step is still routed away from
    OpenAI. ``include_runtime_fields=False`` leaves out the fields only the agent runtime consumes
    (``agent.model``, ``agent.fallback_models``, ``roles.developer_runtime_model``).
    """
    return [
        (key, value)
        for key, value in configured_model_fields(cfg)
        if names_non_openai_model(value)
        and (include_runtime_fields or not (key in _RUNTIME_MODEL_KEYS or key.startswith("agent.fallback_models[")))
    ]


async def _check_openai_models(project_dir: Path) -> list[DiagnosticCheck]:
    """With ``llm.provider = "openai"``, warn about model fields that name a non-OpenAI model.

    Under a Codex runtime every model field is checked. Under any other runtime the runtime-consumed fields
    (``agent.model``, ``agent.fallback_models``, ``roles.developer_runtime_model``) name that runtime's own
    models, so a mixed setup (OpenAI for text calls, the default runtime for tool steps) is not failed for
    them: the row passes with a note that the tool steps spawn that runtime, and fails only on the fields
    the OpenAI provider itself must serve.
    """
    from sova.config.loader import load_config

    try:
        cfg = load_config(project_dir)
    except Exception:  # noqa: BLE001 (the config check already reports an unloadable config; stay silent here)
        return []
    if cfg.llm.provider != "openai":
        return []
    codex = cfg.agent.runtime == "codex"
    found = claude_model_references(cfg, include_runtime_fields=codex)
    if found:
        detail = f"{len(found)} field(s) name a Claude, Vertex or Gemini model: " + ", ".join(
            f"{k}={v}" for k, v in found
        )
        return [DiagnosticCheck(_LABEL_OPENAI_MODELS, False, detail, False)]
    if codex:
        return [DiagnosticCheck(_LABEL_OPENAI_MODELS, True, "no Claude, Vertex or Gemini model in any field", False)]
    note = f"provider fields are OpenAI; tool steps spawn agent.runtime={cfg.agent.runtime} (use codex for OpenAI-only)"
    return [DiagnosticCheck(_LABEL_OPENAI_MODELS, True, note, False)]


async def run_all_checks(project_dir: Path) -> list[DiagnosticCheck]:
    """Run every diagnostic check in the canonical `sova doctor` order.

    Shared by the CLI (`sova doctor`) and the dashboard health panel, so both
    surfaces run exactly the same checks in exactly the same order. Never
    raises: see `_guarded()`.
    """
    checks: list[DiagnosticCheck] = []

    checks.extend(await _guarded("Python >= 3.12", _check_python_version))
    checks.extend(await _guarded("git", _check_git))
    checks.extend(await _guarded(_LABEL_GH_CLI, _check_gh_cli))
    checks.extend(await _guarded(_LABEL_CLAUDE_CLI, _check_claude_cli))
    checks.extend(await _guarded("terminal-notifier", _check_terminal_notifier))
    checks.extend(await _guarded("rtk", _check_rtk))

    checks.extend(await _guarded(_LABEL_GIT_IDENTITY, _check_git_identity, project_dir))
    checks.extend(await _guarded(_LABEL_GIT_HOOKS, _check_git_hooks, project_dir))
    checks.extend(await _guarded("config", _check_sova_config, project_dir))
    checks.extend(await _guarded("install completeness", _check_install_completeness, project_dir))
    checks.extend(await _guarded(_LABEL_DATABASE_SCHEMA, _check_database_schema, project_dir))
    checks.extend(await _guarded(_LABEL_LLM_PROVIDER, _check_llm_provider, project_dir))
    checks.extend(await _guarded(_LABEL_OPENAI_MODELS, _check_openai_models, project_dir))
    checks.extend(await _guarded(_LABEL_CONFIGURED_MODELS, _check_configured_models, project_dir))
    checks.extend(await _guarded("keyring secrets", _check_keyring_secrets, project_dir))
    checks.extend(await _guarded("ollama", _check_ollama, project_dir))
    checks.extend(await _guarded(_LABEL_AGENT_RUNTIME, _check_agent_runtime, project_dir))
    checks.extend(await _guarded(_LABEL_AWARENESS_PROVIDERS, _check_awareness_providers, project_dir))

    return checks
