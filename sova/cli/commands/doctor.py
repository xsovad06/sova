"""CLI command: sova doctor -- validate prerequisites and environment."""

from __future__ import annotations

import asyncio
import platform
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from sova.utils.shell import ShellResult, check_git_identity, run

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig

_EMPTY = "(empty)"

console = Console(stderr=True)

# Type alias for check tuples: (name, passed, detail, required)
_Check = tuple[str, bool, str, bool]


def doctor(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Check prerequisites and environment health."""
    asyncio.run(_doctor(project))


async def _doctor(project: Path | None) -> None:
    checks: list[_Check] = []

    checks.append(_check_python_version())
    checks.extend(await _check_git())
    checks.extend(await _check_gh_cli())
    checks.extend(await _check_claude_cli())
    checks.extend(_check_terminal_notifier())
    checks.extend(_check_rtk())

    project_dir = (project or Path.cwd()).resolve()
    checks.extend(await _check_git_identity(project_dir))
    checks.append(await _check_git_hooks(project_dir))
    checks.extend(await _check_sova_config(project_dir))
    checks.extend(_check_install_completeness(project_dir))
    checks.extend(await _check_llm_provider(project_dir))
    checks.extend(await _check_ollama(project_dir))
    checks.extend(await _check_agent_runtime(project_dir))

    _render_results(checks)


def _check_python_version() -> _Check:
    """Check Python >= 3.12."""
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 12)
    return ("Python >= 3.12", py_ok, py_version, True)


async def _check_git() -> list[_Check]:
    """Check git availability and version."""
    git_path = shutil.which("git")
    if git_path:
        result = await run("git", "--version")
        return [("git", result.success, result.stdout.strip() if result.success else "error", True)]
    return [("git", False, "not found -- install git", True)]


async def _check_git_identity(project_dir: Path) -> list[_Check]:
    """Check git user.name and user.email are configured."""
    identity = await check_git_identity(cwd=project_dir)
    checks: list[_Check] = []
    if identity.valid:
        checks.append(("git identity", True, f"{identity.name} <{identity.email}>", True))
    else:
        missing = ", ".join(identity.missing_fields)
        checks.append(
            (
                "git identity",
                False,
                f"missing {missing}. Set with: git config user.name / user.email",
                True,
            )
        )
    return checks


async def _check_gh_cli() -> list[_Check]:
    """Check GitHub CLI availability, version, and authentication."""
    checks: list[_Check] = []
    gh_path = shutil.which("gh")
    if not gh_path:
        checks.append(("gh CLI", False, "not found. Install: https://cli.github.com/", True))
        checks.append(("gh authenticated", False, "gh CLI not installed", True))
        return checks

    result = await run("gh", "--version")
    version_line = result.stdout.strip().split("\n")[0] if result.success and result.stdout else "error"
    checks.append(("gh CLI", result.success, version_line, True))

    auth_result = await run("gh", "auth", "status")
    auth_ok = auth_result.success
    auth_detail = _extract_auth_detail(auth_result, auth_ok)
    checks.append(("gh authenticated", auth_ok, auth_detail, True))
    return checks


def _extract_auth_detail(auth_result: ShellResult, auth_ok: bool) -> str:
    """Extract authentication detail from gh auth status output."""
    if not auth_ok:
        return "not authenticated -- run: gh auth login"
    for line in (auth_result.stdout + auth_result.stderr).splitlines():
        if "Logged in" in line or "account" in line.lower():
            return line.strip()
    return ""


async def _check_claude_cli() -> list[_Check]:
    """Check Claude CLI availability and version."""
    claude_path = shutil.which("claude")
    if claude_path:
        result = await run("claude", "--version")
        version = result.stdout.strip().split("\n")[0] if result.success and result.stdout else "error"
        return [("claude CLI", result.success, version, True)]
    return [
        (
            "claude CLI",
            False,
            "not found -- install: https://docs.anthropic.com/en/docs/claude-code",
            True,
        )
    ]


def _check_terminal_notifier() -> list[_Check]:
    """Check terminal-notifier on macOS (optional)."""
    if platform.system() != "Darwin":
        return []
    tn_path = shutil.which("terminal-notifier")
    detail = tn_path or "not found -- install: brew install terminal-notifier"
    return [("terminal-notifier", bool(tn_path), f"(optional) {detail}", False)]


def _check_rtk() -> list[_Check]:
    """Check RTK availability (optional context compression)."""
    rtk_path = shutil.which("rtk")
    detail = rtk_path or "not found -- optional context compression tool"
    return [("rtk", bool(rtk_path), f"(optional) {detail}", False)]


async def _check_git_hooks(project_dir: Path) -> _Check:
    """Check git hooks configuration.

    Required when .githooks/ exists -- invariants are silently bypassed otherwise.
    """
    githooks_dir = project_dir / ".githooks"
    if not githooks_dir.is_dir():
        return ("git hooks", True, "no .githooks/ directory (not applicable)", False)

    hooks_result = await run("git", "config", "--get", "core.hooksPath", cwd=str(project_dir))
    hooks_path = hooks_result.stdout.strip() if hooks_result.success else ""
    hooks_ok = hooks_path == ".githooks"
    if hooks_ok:
        hooks_detail = hooks_path
    elif hooks_path:
        hooks_detail = f"set to '{hooks_path}' (expected '.githooks') -- run: git config core.hooksPath .githooks"
    else:
        hooks_detail = "not set -- run: git config core.hooksPath .githooks"
    return ("git hooks", hooks_ok, hooks_detail, True)


async def _check_sova_config(project_dir: Path) -> list[_Check]:
    """Check project configuration (DB or sova.toml)."""
    checks: list[_Check] = []

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
    checks.append(("config", has_config, source, False))

    if toml_exists and db_config is not None:
        checks.append(("legacy sova.toml", False, "exists alongside DB config (DB takes priority)", False))

    if has_config:
        checks.extend(_validate_sova_config(project_dir))

    return checks


def _validate_sova_config(project_dir: Path) -> list[_Check]:
    """Validate sova.toml configuration details."""
    checks: list[_Check] = []
    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        ts_type = cfg.task_source.type
        checks.append(("task_source.type", True, ts_type, False))

        if ts_type == "github":
            checks.extend(_check_github_config(cfg))
        elif ts_type == "jira":
            checks.extend(_check_jira_config(cfg))
    except Exception as exc:
        checks.append(("config valid", False, str(exc)[:80], False))
    return checks


def _check_github_config(cfg: ProjectConfig) -> list[_Check]:
    """Check GitHub-specific configuration fields."""
    return [
        ("github_repo configured", bool(cfg.github_repo), cfg.github_repo or _EMPTY, False),
        ("github_user configured", bool(cfg.github_user), cfg.github_user or _EMPTY, False),
    ]


def _check_jira_config(cfg: ProjectConfig) -> list[_Check]:
    """Check Jira-specific configuration fields."""
    ts = cfg.task_source
    return [
        ("jira_base_url", bool(ts.jira_base_url), ts.jira_base_url or _EMPTY, False),
        ("jira_email", bool(ts.jira_email), ts.jira_email or _EMPTY, False),
        ("jira_api_token", bool(ts.jira_api_token), "(set)" if ts.jira_api_token else _EMPTY, False),
        ("jira_project_key", bool(ts.jira_project_key), ts.jira_project_key or _EMPTY, False),
    ]


async def _check_llm_provider(project_dir: Path) -> list[_Check]:
    """Check configured LLM provider availability."""
    checks: list[_Check] = []
    try:
        from sova.config.loader import load_config
        from sova.llm.provider import create_provider

        cfg = load_config(project_dir)
        provider_type = cfg.llm.provider
        try:
            provider = create_provider(
                provider_type,
                model=cfg.llm.model,
                fallback_model=cfg.llm.fallback_model,
                api_base=cfg.llm.api_base,
            )
        except ValueError as exc:
            # Only ValueError from create_provider (unknown provider type).
            # pydantic.ValidationError also inherits ValueError in v2 but
            # load_config() is called outside this inner try, so it won't
            # be caught here.
            checks.append(("llm provider", False, str(exc), True))
            return checks

        available, detail = await provider.check_available()
        checks.append(("llm provider", available, f"{provider_type}: {detail}", True))
    except Exception as exc:
        checks.append(("llm provider", False, str(exc)[:80], False))
    return checks


async def _check_ollama(project_dir: Path) -> list[_Check]:
    """Check Ollama availability when routing config references ollama/ models."""
    checks: list[_Check] = []
    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        ollama_models = [v for v in cfg.llm.routing.values() if v.startswith("ollama/")]
        if not ollama_models:
            return []

        ollama_path = shutil.which("ollama")
        if not ollama_path:
            checks.append(("ollama CLI", False, "not found -- install: https://ollama.com/", False))
            return checks

        result = await run("ollama", "list")
        if not result.success:
            checks.append(("ollama running", False, "not running -- start with: ollama serve", False))
            return checks

        checks.append(("ollama running", True, "connected", False))

        installed_models = set()
        for line in result.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if parts:
                name = parts[0].split(":")[0]
                installed_models.add(name)

        for model in ollama_models:
            model_name = model.removeprefix("ollama/")
            base_name = model_name.split(":")[0]
            found = base_name in installed_models
            detail = "installed" if found else f"not pulled -- run: ollama pull {model_name}"
            checks.append((f"ollama model: {model_name}", found, detail, False))

    except Exception as exc:
        checks.append(("ollama", False, str(exc)[:80], False))
    return checks


async def _check_agent_runtime(project_dir: Path) -> list[_Check]:
    """Check configured agent runtime availability."""
    _LABEL = "agent runtime"
    checks: list[_Check] = []
    try:
        from sova.config.loader import load_config
        from sova.ipc.runtime import create_runtime

        cfg = load_config(project_dir)
        runtime_type = cfg.agent.runtime
        try:
            runtime = create_runtime(runtime_type)
        except ValueError as exc:
            checks.append((_LABEL, False, str(exc), True))
            return checks

        available, detail = await runtime.check_available()
        checks.append((_LABEL, available, f"{runtime_type}: {detail}", True))
    except Exception as exc:
        checks.append((_LABEL, False, str(exc)[:80], False))
    return checks


def _check_install_completeness(project_dir: Path) -> list[_Check]:
    """Check that sova install created all expected artifacts."""
    from sova.utils.permissions import check_agent_permissions

    checks: list[_Check] = []

    commands_dir = project_dir / ".claude" / "commands"
    if commands_dir.is_dir():
        cmd_count = len(list(commands_dir.glob("*.md")))
        checks.append(("commands installed", cmd_count > 0, f"{cmd_count} commands", cmd_count == 0))
    else:
        checks.append(("commands installed", False, ".claude/commands/ missing -- run: sova install", True))

    memory_dir = project_dir / ".claude" / "agent-memory"
    checks.append(
        (
            "agent memory",
            memory_dir.is_dir(),
            str(memory_dir) if memory_dir.is_dir() else "missing -- run: sova install",
            False,
        )
    )

    db_path = project_dir / ".claude" / "sova.db"
    checks.append(("database", db_path.is_file(), str(db_path) if db_path.is_file() else "missing", True))

    # Agent permissions (warning-level: agents now use bypassPermissions by default)
    claude_dir = project_dir / ".claude"
    has_all, missing = check_agent_permissions(claude_dir)
    if has_all:
        checks.append(("agent permissions", True, "configured", False))
    else:
        detail = f"missing {', '.join(missing)} -- run: sova install --update"
        checks.append(("agent permissions", False, detail, False))

    return checks


def _render_results(checks: list[_Check]) -> None:
    """Render check results as a Rich table and exit appropriately."""
    table = Table(title="SOVA Doctor", show_header=True)
    table.add_column("Check", style="cyan")
    table.add_column("Status")
    table.add_column("Detail", style="dim")

    has_required_failure = False
    for name, passed, detail, required in checks:
        if passed:
            status = "[green]OK[/green]"
        elif required:
            status = "[red]FAIL[/red]"
            has_required_failure = True
        else:
            status = "[yellow]WARN[/yellow]"
        table.add_row(name, status, detail)

    console.print(table)

    if has_required_failure:
        console.print("\n[red]Some required checks failed. Fix the issues above before using SOVA.[/red]")
        raise typer.Exit(code=1)
    else:
        console.print("\n[green]All required checks passed.[/green]")
