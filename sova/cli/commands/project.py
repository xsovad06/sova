"""CLI commands: sova install, sova setup -- project initialization."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from sova.utils.shell import run

console = Console(stderr=True)


def install(
    path: Annotated[Optional[Path], typer.Argument(help="Project directory to install into.")] = None,
    no_dashboard: Annotated[bool, typer.Option("--no-dashboard", help="Skip dashboard setup.")] = False,
    update: Annotated[bool, typer.Option("--update", help="Quick sync (config + personas only).")] = False,
) -> None:
    """Install SOVA into a project directory."""
    asyncio.run(_install(path=path, no_dashboard=no_dashboard, update=update))


async def _install(*, path: Path | None, no_dashboard: bool, update: bool) -> None:
    project_dir = (path or Path.cwd()).resolve()

    if not project_dir.is_dir():
        console.print(f"[red]Directory not found: {project_dir}[/red]")
        raise typer.Exit(code=1)

    claude_dir = project_dir / ".claude"
    claude_dir.mkdir(exist_ok=True)

    # Create default config if it doesn't exist
    toml_file = project_dir / "sova.toml"

    if not toml_file.exists():
        toml_file.write_text(_default_toml())
        console.print(f"[green]Created {toml_file}[/green]")

    # Configure git hooks if .githooks/ exists
    githooks_dir = project_dir / ".githooks"
    if githooks_dir.is_dir():
        hooks_result = await run("git", "config", "--get", "core.hooksPath", cwd=str(project_dir))
        current = hooks_result.stdout.strip() if hooks_result.success else ""
        if current != ".githooks":
            config_result = await run("git", "config", "core.hooksPath", ".githooks", cwd=str(project_dir))
            if config_result.success:
                console.print("[green]Configured git hooks: core.hooksPath = .githooks[/green]")
            else:
                console.print("[yellow]Warning: failed to configure git hooks[/yellow]")

    # Initialize database
    from sova.db.session import init_db

    await init_db(project_dir)
    console.print("[green]Database initialized.[/green]")

    # Install/update commands
    from sova.commands.catalog import get_canonical_dir, get_guidelines_dir
    from sova.commands.distribution import install_commands as install_cmds
    from sova.commands.distribution import install_guidelines as install_guides
    from sova.commands.distribution import update_commands as update_cmds
    from sova.commands.distribution import update_guidelines as update_guides
    from sova.config.loader import load_config

    cfg = load_config(project_dir)
    canonical_dir = get_canonical_dir()
    guidelines_dir = get_guidelines_dir()
    commands_dir = claude_dir / "commands"
    commands_dir.mkdir(exist_ok=True)
    rules_dir = claude_dir / "rules"
    rules_dir.mkdir(exist_ok=True)

    if update:
        cmd_result = update_cmds(canonical_dir, commands_dir, cfg)
        console.print(f"[green]Commands updated: {cmd_result.updated}, unchanged: {cmd_result.skipped}[/green]")
        if cmd_result.conflicts:
            for name in cmd_result.conflicts:
                console.print(f"  [yellow]! {name} -- locally modified, skipped[/yellow]")
        guide_result = update_guides(guidelines_dir, rules_dir, cfg)
        console.print(f"[green]Guidelines updated: {guide_result.updated}, unchanged: {guide_result.skipped}[/green]")
        if guide_result.conflicts:
            for name in guide_result.conflicts:
                console.print(f"  [yellow]! {name} -- locally modified, skipped[/yellow]")
        console.print("[green]Quick sync complete.[/green]")
        return

    cmd_result = install_cmds(canonical_dir, commands_dir, cfg)
    console.print(f"[green]Commands installed: {cmd_result.installed}[/green]")

    guide_result = install_guides(guidelines_dir, rules_dir, cfg)
    console.print(f"[green]Guidelines installed: {guide_result.installed}[/green]")

    # Create agent memory directory
    memory_dir = claude_dir / "agent-memory"
    memory_dir.mkdir(exist_ok=True)

    for name in ["MEMORY.md", "learnings.md", "review-feedback.md", "common-mistakes.md"]:
        mem_file = memory_dir / name
        if not mem_file.exists():
            mem_file.write_text(f"# {name.replace('.md', '').replace('-', ' ').title()}\n")

    console.print(f"[green]SOVA installed in {project_dir}[/green]")

    if not no_dashboard:
        console.print("[dim]Dashboard available via: sova dashboard[/dim]")


def setup(
    path: Annotated[Optional[Path], typer.Argument(help="Project directory.")] = None,
) -> None:
    """Run the interactive setup wizard."""
    asyncio.run(_setup(path=path))


async def _setup(*, path: Path | None) -> None:
    project_dir = (path or Path.cwd()).resolve()

    if not project_dir.is_dir():
        console.print(f"[red]Directory not found: {project_dir}[/red]")
        raise typer.Exit(code=1)

    # Detect GitHub repo
    result = await run("git", "remote", "get-url", "origin", cwd=project_dir)
    repo = ""
    if result.success:
        origin = result.stdout.strip()
        # Extract owner/repo from git URL
        # Handles SSH aliases like github.com-personal
        m = re.search(r"github\.com[^:/]*[:/](.+?)(?:\.git)?$", origin)
        if m:
            repo = m.group(1)

    # Detect github_user from repo owner
    github_user = ""
    if repo and "/" in repo:
        candidate = repo.split("/")[0]
        # Verify the user is authenticated in gh
        check = await run("gh", "auth", "token", "--user", candidate)
        if check.success and check.stdout.strip():
            github_user = candidate

    console.print("[bold]SOVA Setup Wizard[/bold]\n")
    console.print(f"Project: {project_dir}")
    if repo:
        console.print(f"Detected repo: {repo}")
    if github_user:
        console.print(f"Detected GitHub user: {github_user}")

    # Detect test command
    test_cmd = "make test"
    if (project_dir / "package.json").exists():
        test_cmd = "npm test"
    elif (project_dir / "Cargo.toml").exists():
        test_cmd = "cargo test"
    elif (project_dir / "go.mod").exists():
        test_cmd = "go test ./..."

    toml_file = project_dir / "sova.toml"
    toml_content = _default_toml(repo=repo, test_cmd=test_cmd, github_user=github_user)
    toml_file.write_text(toml_content)
    console.print(f"\n[green]Configuration written to {toml_file}[/green]")

    # Run install
    await _install(path=project_dir, no_dashboard=False, update=False)


def _default_toml(repo: str = "", test_cmd: str = "make test", github_user: str = "") -> str:
    return f"""# SOVA configuration
github_repo = "{repo}"
github_user = "{github_user}"
base_branch = "main"
test_cmd = "{test_cmd}"
lint_cmd = "make lint"

[task_source]
type = "github"

[agent]
model = "opus"
max_budget = "10.00"

[review]
enabled = true
max_rounds = 2

[triage]
auto_label = true
min_confidence = 0.7

[roles]
default = "developer"
"""
