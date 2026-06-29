"""CLI commands: sova install, sova setup, sova uninstall -- project lifecycle."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from sova.adapters import create_adapter
from sova.config.loader import load_config
from sova.dashboard.services.setup_service import DEFAULT_PHASE_TITLES, create_starter_milestones
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

    # Stage 1: Config
    toml_file = project_dir / "sova.toml"
    if not toml_file.exists():
        toml_file.write_text(_default_toml())
        console.print(f"[green]Created {toml_file}[/green]")

    # Stage 2: Git hooks (non-fatal)
    try:
        await _configure_git_hooks(project_dir)
    except Exception as exc:
        console.print(f"[yellow]Warning: git hooks configuration failed: {exc}[/yellow]")

    # Stage 3: Database
    failed_stages: list[str] = []
    try:
        from sova.db.session import init_db

        await init_db(project_dir)
        console.print("[green]Database initialized.[/green]")
    except Exception as exc:
        console.print(f"[red]Database initialization failed: {exc}[/red]")
        failed_stages.append("database")

    # Stage 4: Commands and guidelines
    try:
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
            console.print(
                f"[green]Guidelines updated: {guide_result.updated}, unchanged: {guide_result.skipped}[/green]"
            )
            if guide_result.conflicts:
                for name in guide_result.conflicts:
                    console.print(f"  [yellow]! {name} -- locally modified, skipped[/yellow]")
        else:
            cmd_result = install_cmds(canonical_dir, commands_dir, cfg)
            console.print(f"[green]Commands installed: {cmd_result.installed}[/green]")
            guide_result = install_guides(guidelines_dir, rules_dir, cfg)
            console.print(f"[green]Guidelines installed: {guide_result.installed}[/green]")
    except Exception as exc:
        console.print(f"[red]Command installation failed: {exc}[/red]")
        failed_stages.append("commands")

    # Stage 5: Agent memory (non-fatal)
    if not update:
        try:
            _create_agent_memory(claude_dir)
        except Exception as exc:
            console.print(f"[yellow]Warning: agent memory setup failed: {exc}[/yellow]")

    # Verify and report
    problems = _verify_install(project_dir, update=update)
    if "commands" in failed_stages:
        problems.append("commands/guidelines installation failed")
    if problems:
        console.print("\n[red]Installation incomplete -- the following are missing:[/red]")
        for problem in problems:
            console.print(f"  [red]- {problem}[/red]")
        console.print("[dim]Re-run 'sova install' to retry.[/dim]")
        raise typer.Exit(code=1)

    if failed_stages:
        console.print(f"\n[yellow]Installed with warnings (failed: {', '.join(failed_stages)})[/yellow]")
    elif update:
        console.print("[green]Quick sync complete.[/green]")
    else:
        console.print(f"[green]SOVA installed in {project_dir}[/green]")

    if not no_dashboard and not update:
        console.print("[dim]Dashboard available via: sova dashboard[/dim]")


async def _configure_git_hooks(project_dir: Path) -> None:
    """Configure core.hooksPath if .githooks/ exists."""
    githooks_dir = project_dir / ".githooks"
    if not githooks_dir.is_dir():
        return

    hooks_result = await run("git", "config", "--get", "core.hooksPath", cwd=str(project_dir))
    current = hooks_result.stdout.strip() if hooks_result.success else ""
    if current != ".githooks":
        config_result = await run("git", "config", "core.hooksPath", ".githooks", cwd=str(project_dir))
        if config_result.success:
            console.print("[green]Configured git hooks: core.hooksPath = .githooks[/green]")
        else:
            console.print("[yellow]Warning: failed to configure git hooks[/yellow]")


def _create_agent_memory(claude_dir: Path) -> None:
    """Create agent memory directory with starter files."""
    memory_dir = claude_dir / "agent-memory"
    memory_dir.mkdir(exist_ok=True)

    for name in ["MEMORY.md", "learnings.md", "review-feedback.md", "common-mistakes.md"]:
        mem_file = memory_dir / name
        if not mem_file.exists():
            mem_file.write_text(f"# {name.replace('.md', '').replace('-', ' ').title()}\n")


def _verify_install(project_dir: Path, *, update: bool = False) -> list[str]:
    """Check that a SOVA installation has all critical artifacts."""
    from sova.commands.catalog import get_canonical_dir

    problems: list[str] = []

    try:
        if not (project_dir / "sova.toml").exists():
            problems.append("sova.toml not found")

        commands_dir = project_dir / ".claude" / "commands"
        command_count = len(list(commands_dir.glob("*.md"))) if commands_dir.is_dir() else 0
        if command_count == 0 and get_canonical_dir().is_dir():
            problems.append(".claude/commands/ has no commands")

        if not update:
            memory_dir = project_dir / ".claude" / "agent-memory"
            if not memory_dir.is_dir():
                problems.append(".claude/agent-memory/ directory missing")
    except OSError as exc:
        problems.append(f"cannot verify installation: {exc}")

    return problems


def uninstall(
    path: Annotated[Optional[Path], typer.Argument(help="Project directory to uninstall from.")] = None,
    remove_commands: Annotated[bool, typer.Option("--remove-commands", help="Remove SOVA-managed commands.")] = False,
    remove_rules: Annotated[bool, typer.Option("--remove-rules", help="Remove SOVA-managed guidelines/rules.")] = False,
    remove_memory: Annotated[bool, typer.Option("--remove-memory", help="Remove agent memory data.")] = False,
    remove_config: Annotated[bool, typer.Option("--remove-config", help="Remove sova.toml.")] = False,
) -> None:
    """Remove SOVA from a project directory.

    By default keeps commands, rules, memory, and config. Use --remove-* flags to opt in to deleting them.
    Database and ephemeral files (worktrees, agent-control) are always removed.
    """
    asyncio.run(
        _uninstall(
            path=path,
            remove_commands=remove_commands,
            remove_rules=remove_rules,
            remove_memory=remove_memory,
            remove_config=remove_config,
        )
    )


async def _uninstall(
    *,
    path: Path | None,
    remove_commands: bool = False,
    remove_rules: bool = False,
    remove_memory: bool = False,
    remove_config: bool = False,
) -> list[str]:
    project_dir = (path or Path.cwd()).resolve()

    if not project_dir.is_dir():
        console.print(f"[red]Directory not found: {project_dir}[/red]")
        raise typer.Exit(code=1)

    claude_dir = project_dir / ".claude"
    removed: list[str] = []
    failed: list[str] = []

    # 1. Managed commands (opt-in removal)
    if remove_commands:
        try:
            commands_dir = claude_dir / "commands"
            if commands_dir.is_dir():
                managed_count = _remove_managed_commands(commands_dir)
                if managed_count > 0:
                    removed.append(f"{managed_count} managed commands")
                if not any(commands_dir.iterdir()):
                    commands_dir.rmdir()
                    removed.append(".claude/commands/ (empty)")
        except OSError as exc:
            failed.append(f"commands: {exc}")

    # 2. Managed rules/guidelines (opt-in removal)
    if remove_rules:
        try:
            rules_dir = claude_dir / "rules"
            if rules_dir.is_dir():
                managed_count = _remove_managed_commands(rules_dir)
                if managed_count > 0:
                    removed.append(f"{managed_count} managed rules")
                if not any(rules_dir.iterdir()):
                    rules_dir.rmdir()
                    removed.append(".claude/rules/ (empty)")
        except OSError as exc:
            failed.append(f"rules: {exc}")

    # 3. Remove database files (always)
    for db_name in ("sova.db", "sova.db.bak"):
        db_file = claude_dir / db_name
        try:
            if db_file.is_file():
                db_file.unlink()
                removed.append(f".claude/{db_name}")
        except OSError as exc:
            failed.append(f".claude/{db_name}: {exc}")

    # 4. Remove ephemeral directories (always)
    for dir_name in ("worktrees", "agent-control"):
        target = claude_dir / dir_name
        try:
            if target.is_dir():
                shutil.rmtree(target)
                removed.append(f".claude/{dir_name}/")
        except OSError as exc:
            failed.append(f".claude/{dir_name}/: {exc}")

    # 5. Agent memory (opt-in removal)
    if remove_memory:
        try:
            memory_dir = claude_dir / "agent-memory"
            if memory_dir.is_dir():
                shutil.rmtree(memory_dir)
                removed.append(".claude/agent-memory/")
        except OSError as exc:
            failed.append(f"agent memory: {exc}")

    # 6. Config file (opt-in removal)
    if remove_config:
        try:
            toml_file = project_dir / "sova.toml"
            if toml_file.exists():
                toml_file.unlink()
                removed.append("sova.toml")
        except OSError as exc:
            failed.append(f"config: {exc}")

    # 7. Remove .claude/ if empty
    try:
        if claude_dir.is_dir() and not any(claude_dir.iterdir()):
            claude_dir.rmdir()
            removed.append(".claude/ (empty)")
    except OSError:
        pass

    # 8. Unregister from project registry
    try:
        from sova.config.registry import list_projects, unregister_project

        for slug, reg_path in list_projects().items():
            if Path(reg_path).resolve() == project_dir:
                unregister_project(slug)
                removed.append(f"registry entry ({slug})")
                break
    except Exception as exc:
        failed.append(f"registry: {exc}")

    if removed:
        console.print(f"[green]SOVA uninstalled from {project_dir}[/green]")
        for item in removed:
            console.print(f"  [dim]- {item}[/dim]")
    else:
        console.print("[yellow]No SOVA artifacts found to remove.[/yellow]")

    if failed:
        for item in failed:
            console.print(f"  [red]Failed: {item}[/red]")

    return failed


def _remove_managed_commands(commands_dir: Path) -> int:
    """Remove SOVA-managed commands, leaving local ones. Returns count removed."""
    from sova.commands.manifest import MANIFEST_FILENAME, read_manifest

    manifest = read_manifest(commands_dir)
    if manifest is None:
        return 0

    count = 0
    base_dir = commands_dir.resolve()
    for filename, entry in manifest.commands.items():
        if entry.managed:
            cmd_file = (commands_dir / filename).resolve()
            if not cmd_file.is_relative_to(base_dir):
                continue
            if cmd_file.exists():
                cmd_file.unlink()
                count += 1

    manifest_file = commands_dir / MANIFEST_FILENAME
    if manifest_file.exists():
        manifest_file.unlink()

    return count


def setup(
    path: Annotated[Optional[Path], typer.Argument(help="Project directory.")] = None,
) -> None:
    """Run the interactive setup wizard."""
    asyncio.run(_setup(path=path))


async def _detect_github_repo(project_dir: Path) -> str:
    """Detect the GitHub owner/repo from git remote origin."""
    result = await run("git", "remote", "get-url", "origin", cwd=project_dir)
    if not result.success:
        return ""
    origin = result.stdout.strip()
    # Handles SSH aliases like github.com-personal
    m = re.search(r"github\.com[^:/]*[:/](.+?)(?:\.git)?$", origin)
    return m.group(1) if m else ""


async def _detect_github_user(repo: str) -> str:
    """Detect the GitHub user from repo owner, verifying gh auth."""
    if not repo or "/" not in repo:
        return ""
    candidate = repo.split("/")[0]
    check = await run("gh", "auth", "token", "--user", candidate)
    if check.success and check.stdout.strip():
        return candidate
    return ""


def _detect_test_command(project_dir: Path) -> str:
    """Detect the appropriate test command for the project."""
    if (project_dir / "package.json").exists():
        return "npm test"
    if (project_dir / "Cargo.toml").exists():
        return "cargo test"
    if (project_dir / "go.mod").exists():
        return "go test ./..."
    return "make test"


async def _setup(*, path: Path | None) -> None:
    project_dir = (path or Path.cwd()).resolve()

    if not project_dir.is_dir():
        console.print(f"[red]Directory not found: {project_dir}[/red]")
        raise typer.Exit(code=1)

    repo = await _detect_github_repo(project_dir)
    github_user = await _detect_github_user(repo)

    console.print("[bold]SOVA Setup Wizard[/bold]\n")
    console.print(f"Project: {project_dir}")
    if repo:
        console.print(f"Detected repo: {repo}")
    if github_user:
        console.print(f"Detected GitHub user: {github_user}")

    test_cmd = _detect_test_command(project_dir)

    toml_file = project_dir / "sova.toml"
    toml_content = _default_toml(repo=repo, test_cmd=test_cmd, github_user=github_user)
    toml_file.write_text(toml_content)
    console.print(f"\n[green]Configuration written to {toml_file}[/green]")

    # Run install
    await _install(path=project_dir, no_dashboard=False, update=False)

    # Offer to create starter phase milestones
    await _offer_starter_milestones(project_dir)


async def _offer_starter_milestones(project_dir: Path) -> None:
    """Offer to create default phase milestones on the tracker."""
    try:
        cfg = load_config(project_dir)
    except (FileNotFoundError, ValueError, KeyError):
        return

    if not cfg.task_source.type:
        return

    try:
        create_adapter(cfg)
    except ValueError:
        return

    console.print("\n[bold]Phase Milestones[/bold]")
    console.print("SOVA uses milestones to organize work into phases:")
    for title in DEFAULT_PHASE_TITLES:
        console.print(f"  - {title}")

    create = typer.confirm("Create these milestones on the tracker?", default=True)
    if not create:
        console.print("[dim]Skipped milestone creation.[/dim]")
        return

    result = await create_starter_milestones(project_dir)
    if result.get("status") == "error":
        console.print(f"[red]Failed: {result.get('detail', 'Unknown error')}[/red]")
        return

    created = result.get("created", [])
    skipped = result.get("skipped", [])
    failed = result.get("failed", [])

    if created:
        console.print(f"[green]Created {len(created)} milestones: {', '.join(created)}[/green]")
    if skipped:
        console.print(f"[yellow]Skipped {len(skipped)} (already exist): {', '.join(skipped)}[/yellow]")
    if failed:
        for f in failed:
            console.print(f"[red]Failed: {f['title']} -- {f['error']}[/red]")


def _default_toml(repo: str = "", test_cmd: str = "make test", github_user: str = "") -> str:
    return f"""# SOVA configuration
github_repo = "{repo}"
github_user = "{github_user}"
base_branch = "main"
test_cmd = "{test_cmd}"
lint_cmd = "make lint"
check_cmd = ""

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
