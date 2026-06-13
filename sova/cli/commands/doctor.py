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

from sova.utils.shell import ShellResult, run

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

    project_dir = (project or Path.cwd()).resolve()
    checks.append(await _check_git_hooks(project_dir))
    checks.extend(await _check_sova_config(project_dir))
    checks.extend(await _check_llm_provider(project_dir))

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


async def _check_gh_cli() -> list[_Check]:
    """Check GitHub CLI availability, version, and authentication."""
    checks: list[_Check] = []
    gh_path = shutil.which("gh")
    if not gh_path:
        checks.append(("gh CLI", False, "not found -- install: https://cli.github.com/", True))
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


async def _check_git_hooks(project_dir: Path) -> _Check:
    """Check git hooks configuration."""
    hooks_result = await run("git", "config", "--get", "core.hooksPath", cwd=str(project_dir))
    hooks_path = hooks_result.stdout.strip() if hooks_result.success else ""
    hooks_ok = hooks_path == ".githooks"
    hooks_detail = hooks_path if hooks_ok else "not set -- run: make setup"
    return ("git hooks", hooks_ok, hooks_detail, False)


async def _check_sova_config(project_dir: Path) -> list[_Check]:
    """Check sova.toml presence and configuration."""
    checks: list[_Check] = []
    toml_path = project_dir / "sova.toml"
    toml_exists = toml_path.exists()
    checks.append(("sova.toml", toml_exists, str(toml_path) if toml_exists else f"not found at {toml_path}", False))

    if toml_exists:
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
    repo = cfg.github_repo or _EMPTY
    user = cfg.github_user or _EMPTY
    return [
        ("github_repo configured", bool(cfg.github_repo), repo, False),
        ("github_user configured", bool(cfg.github_user), user, False),
    ]


def _check_jira_config(cfg: ProjectConfig) -> list[_Check]:
    """Check Jira-specific configuration fields."""
    ts = cfg.task_source
    token_detail = "(set)" if ts.jira_api_token else _EMPTY
    return [
        ("jira_base_url", bool(ts.jira_base_url), ts.jira_base_url or _EMPTY, False),
        ("jira_email", bool(ts.jira_email), ts.jira_email or _EMPTY, False),
        ("jira_api_token", bool(ts.jira_api_token), token_detail, False),
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
