"""CLI command: sova doctor -- validate prerequisites and environment."""

from __future__ import annotations

import asyncio
import platform
import shutil
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from sova.utils.shell import run

console = Console(stderr=True)


def doctor(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Check prerequisites and environment health."""
    asyncio.run(_doctor(project))


async def _doctor(project: Path | None) -> None:
    checks: list[tuple[str, bool, str, bool]] = []  # (name, passed, detail, required)

    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 12)
    checks.append(("Python >= 3.12", py_ok, py_version, True))

    git_path = shutil.which("git")
    if git_path:
        result = await run("git", "--version")
        checks.append(("git", result.success, result.stdout.strip() if result.success else "error", True))
    else:
        checks.append(("git", False, "not found -- install git", True))

    gh_path = shutil.which("gh")
    if gh_path:
        result = await run("gh", "--version")
        version_line = result.stdout.strip().split("\n")[0] if result.success and result.stdout else "error"
        checks.append(("gh CLI", result.success, version_line, True))
        auth_result = await run("gh", "auth", "status")
        auth_ok = auth_result.success
        auth_detail = ""
        if auth_ok:
            for line in (auth_result.stdout + auth_result.stderr).splitlines():
                if "Logged in" in line or "account" in line.lower():
                    auth_detail = line.strip()
                    break
        else:
            auth_detail = "not authenticated -- run: gh auth login"
        checks.append(("gh authenticated", auth_ok, auth_detail, True))
    else:
        checks.append(("gh CLI", False, "not found -- install: https://cli.github.com/", True))
        checks.append(("gh authenticated", False, "gh CLI not installed", True))

    claude_path = shutil.which("claude")
    if claude_path:
        result = await run("claude", "--version")
        version = result.stdout.strip().split("\n")[0] if result.success and result.stdout else "error"
        checks.append(("claude CLI", result.success, version, True))
    else:
        checks.append(
            (
                "claude CLI",
                False,
                "not found -- install: https://docs.anthropic.com/en/docs/claude-code",
                True,
            )
        )

    if platform.system() == "Darwin":
        tn_path = shutil.which("terminal-notifier")
        detail = tn_path or "not found -- install: brew install terminal-notifier"
        checks.append(("terminal-notifier", bool(tn_path), f"(optional) {detail}", False))

    project_dir = (project or Path.cwd()).resolve()
    toml_path = project_dir / "sova.toml"
    toml_exists = toml_path.exists()
    checks.append(("sova.toml", toml_exists, str(toml_path) if toml_exists else f"not found at {toml_path}", False))

    if toml_exists:
        try:
            from sova.config.loader import load_config

            cfg = load_config(project_dir)
            ts_type = cfg.task_source.type
            checks.append(("task_source.type", True, ts_type, False))
            if ts_type == "github":
                repo = cfg.github_repo or "(empty)"
                user = cfg.github_user or "(empty)"
                checks.append(("github_repo configured", bool(cfg.github_repo), repo, False))
                checks.append(("github_user configured", bool(cfg.github_user), user, False))
            elif ts_type == "jira":
                ts = cfg.task_source
                checks.append(("jira_base_url", bool(ts.jira_base_url), ts.jira_base_url or "(empty)", False))
                checks.append(("jira_email", bool(ts.jira_email), ts.jira_email or "(empty)", False))
                token_detail = "(set)" if ts.jira_api_token else "(empty)"
                checks.append(("jira_api_token", bool(ts.jira_api_token), token_detail, False))
                key = ts.jira_project_key or "(empty)"
                checks.append(("jira_project_key", bool(ts.jira_project_key), key, False))
        except Exception as exc:
            checks.append(("config valid", False, str(exc)[:80], False))

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
