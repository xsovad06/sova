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
import platform
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sova.utils.shell import ShellResult, check_git_identity, run

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig

_EMPTY = "(empty)"

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
        checks.append(DiagnosticCheck("git identity", True, f"{identity.name} <{identity.email}>", True))
    else:
        missing = ", ".join(identity.missing_fields)
        checks.append(
            DiagnosticCheck(
                "git identity",
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
        checks.append(DiagnosticCheck("gh CLI", False, "not found. Install: https://cli.github.com/", True))
        checks.append(DiagnosticCheck("gh authenticated", False, "gh CLI not installed", True))
        return checks

    result = await run("gh", "--version")
    version_line = result.stdout.strip().split("\n")[0] if result.success and result.stdout else "error"
    checks.append(DiagnosticCheck("gh CLI", result.success, version_line, True))

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
        return [DiagnosticCheck("claude CLI", result.success, version, True)]
    return [
        DiagnosticCheck(
            "claude CLI",
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
        return DiagnosticCheck("git hooks", True, "no .githooks/ directory (not applicable)", False)

    hooks_result = await run("git", "config", "--get", "core.hooksPath", cwd=str(project_dir))
    hooks_path = hooks_result.stdout.strip() if hooks_result.success else ""
    hooks_ok = hooks_path == ".githooks"
    if hooks_ok:
        hooks_detail = hooks_path
    elif hooks_path:
        hooks_detail = f"set to '{hooks_path}' (expected '.githooks'), run: git config core.hooksPath .githooks"
    else:
        hooks_detail = "not set, run: git config core.hooksPath .githooks"
    return DiagnosticCheck("git hooks", hooks_ok, hooks_detail, True)


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
            checks.append(DiagnosticCheck("llm provider", False, str(exc), True))
            return checks

        available, detail = await provider.check_available()
        checks.append(DiagnosticCheck("llm provider", available, f"{provider_type}: {detail}", True))
    except Exception as exc:  # noqa: BLE001 (diagnostic check reports any failure as a failed check row)
        checks.append(DiagnosticCheck("llm provider", False, str(exc)[:80], False))
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
            # even though it resolves correctly.
            scoped = keyring_store.scoped_secret_name(key, project_dir, cfg.llm.provider)
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


async def _check_agent_runtime(project_dir: Path) -> list[DiagnosticCheck]:
    """Check configured agent runtime availability."""
    _LABEL = "agent runtime"
    checks: list[DiagnosticCheck] = []
    try:
        from sova.config.loader import load_config
        from sova.ipc.runtime import create_runtime

        cfg = load_config(project_dir)
        runtime_type = cfg.agent.runtime
        try:
            runtime = create_runtime(runtime_type, codex=cfg.codex)
        except ValueError as exc:
            checks.append(DiagnosticCheck(_LABEL, False, str(exc), True))
            return checks

        available, detail = await runtime.check_available()
        checks.append(DiagnosticCheck(_LABEL, available, f"{runtime_type}: {detail}", True))

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
        checks.append(DiagnosticCheck(_LABEL, False, str(exc)[:80], False))
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
            checks.append(DiagnosticCheck("awareness providers", True, "no providers configured", False))
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
        checks.append(DiagnosticCheck("awareness providers", False, str(exc)[:80], False))
    return checks


def _check_install_completeness(project_dir: Path) -> list[DiagnosticCheck]:
    """Check that sova install created all expected artifacts."""
    from sova.utils.permissions import check_agent_permissions

    checks: list[DiagnosticCheck] = []

    commands_dir = project_dir / ".claude" / "commands"
    if commands_dir.is_dir():
        cmd_count = len(list(commands_dir.glob("*.md")))
        checks.append(DiagnosticCheck("commands installed", cmd_count > 0, f"{cmd_count} commands", cmd_count == 0))
    else:
        checks.append(
            DiagnosticCheck("commands installed", False, ".claude/commands/ missing, run: sova install", True)
        )

    memory_dir = project_dir / ".claude" / "agent-memory"
    checks.append(
        DiagnosticCheck(
            "agent memory",
            memory_dir.is_dir(),
            str(memory_dir) if memory_dir.is_dir() else "missing, run: sova install",
            False,
        )
    )

    db_path = project_dir / ".claude" / "sova.db"
    checks.append(
        DiagnosticCheck("database", db_path.is_file(), str(db_path) if db_path.is_file() else "missing", True)
    )

    # Agent permissions (warning-level: agents now use bypassPermissions by default)
    claude_dir = project_dir / ".claude"
    has_all, missing = check_agent_permissions(claude_dir)
    if has_all:
        checks.append(DiagnosticCheck("agent permissions", True, "configured", False))
    else:
        detail = f"missing {', '.join(missing)}, run: sova install --update"
        checks.append(DiagnosticCheck("agent permissions", False, detail, False))

    return checks


async def _check_database_schema(project_dir: Path) -> list[DiagnosticCheck]:
    """Compare the project database's alembic revision with the migration head.

    Warning-level, never required: ``init_db()`` migrates a database that is
    behind on the next server start, so drift here is worth surfacing but does
    not stop SOVA from working. A project with no database file yields no row,
    since the "database" check in ``_check_install_completeness`` already reports
    that and a schema comparison against nothing would only duplicate it.
    """
    _LABEL = "database schema"
    checks: list[DiagnosticCheck] = []
    try:
        from sova.db.session import inspect_schema

        inspection = await inspect_schema(project_dir)
        if inspection.state == "no_tables":
            checks.append(DiagnosticCheck(_LABEL, False, "database has no tables, run: sova init-db", False))
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
                checks.append(DiagnosticCheck(_LABEL, False, f"{'; '.join(problems)}, run: sova init-db", False))
            else:
                checks.append(DiagnosticCheck(_LABEL, True, f"at head ({inspection.head})", False))
    except Exception as exc:  # noqa: BLE001 (diagnostic check reports any failure as a failed check row)
        checks.append(DiagnosticCheck(_LABEL, False, f"could not inspect: {str(exc)[:60]}", False))
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
    except Exception as exc:  # noqa: BLE001 (backstop: one broken check is one failed row, not a lost report)
        return [DiagnosticCheck(label, False, f"check failed: {str(exc)[:80]}", False)]
    return result if isinstance(result, list) else [result]


async def run_all_checks(project_dir: Path) -> list[DiagnosticCheck]:
    """Run every diagnostic check in the canonical `sova doctor` order.

    Shared by the CLI (`sova doctor`) and the dashboard health panel, so both
    surfaces run exactly the same checks in exactly the same order. Never
    raises: see `_guarded()`.
    """
    checks: list[DiagnosticCheck] = []

    checks.extend(await _guarded("Python >= 3.12", _check_python_version))
    checks.extend(await _guarded("git", _check_git))
    checks.extend(await _guarded("gh CLI", _check_gh_cli))
    checks.extend(await _guarded("claude CLI", _check_claude_cli))
    checks.extend(await _guarded("terminal-notifier", _check_terminal_notifier))
    checks.extend(await _guarded("rtk", _check_rtk))

    checks.extend(await _guarded("git identity", _check_git_identity, project_dir))
    checks.extend(await _guarded("git hooks", _check_git_hooks, project_dir))
    checks.extend(await _guarded("config", _check_sova_config, project_dir))
    checks.extend(await _guarded("install completeness", _check_install_completeness, project_dir))
    checks.extend(await _guarded("database schema", _check_database_schema, project_dir))
    checks.extend(await _guarded("llm provider", _check_llm_provider, project_dir))
    checks.extend(await _guarded("keyring secrets", _check_keyring_secrets, project_dir))
    checks.extend(await _guarded("ollama", _check_ollama, project_dir))
    checks.extend(await _guarded("agent runtime", _check_agent_runtime, project_dir))
    checks.extend(await _guarded("awareness providers", _check_awareness_providers, project_dir))

    return checks
