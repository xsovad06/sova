"""CLI commands: sova commands list/diff/update/drift/backport -- command distribution management."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from sova.commands.catalog import get_canonical_dir, get_guidelines_dir, get_skills_dir
from sova.commands.distribution import (
    ReverseDiffResult,
    diff_commands,
    diff_skills,
    list_commands,
    reverse_diff_commands,
    reverse_diff_guidelines,
    reverse_diff_skills,
    update_commands,
    update_skills,
)
from sova.commands.templates import build_variables, reverse_render
from sova.config.loader import load_config
from sova.config.registry import list_projects

app = typer.Typer(
    name="commands",
    help="Manage SOVA command distribution.",
    no_args_is_help=True,
)

console = Console(stderr=True)

_COMMANDS_SUBDIR = Path(".claude") / "commands"


@app.command(name="list")
def list_cmd(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """List installed commands (managed vs local)."""
    project_dir = (project or Path.cwd()).resolve()
    target_dir = project_dir / _COMMANDS_SUBDIR

    listing = list_commands(target_dir)

    table = Table(title="Installed Commands", show_header=True)
    table.add_column("Command", style="cyan")
    table.add_column("Type", style="green")

    for entry in listing.managed:
        table.add_row(entry.filename, "managed (SOVA)")
    for entry in listing.local:
        table.add_row(entry.filename, "local (project)")

    console.print(table)
    console.print(f"\n  Managed: {len(listing.managed)}  |  Local: {len(listing.local)}")


@app.command(name="diff")
def diff_cmd(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Show what changed since last install."""
    project_dir = (project or Path.cwd()).resolve()
    target_dir = project_dir / _COMMANDS_SUBDIR
    cfg = load_config(project_dir)
    canonical_dir = get_canonical_dir()

    result = diff_commands(canonical_dir, target_dir, cfg)

    if not result.changed and not result.new and not result.removed:
        console.print("[green]All commands are up to date.[/green]")
        return

    if result.new:
        console.print("[bold]New commands available:[/bold]")
        for name in result.new:
            console.print(f"  + {name}", style="green")

    if result.changed:
        console.print("[bold]Changed commands:[/bold]")
        for name in result.changed:
            console.print(f"  ~ {name}", style="yellow")

    if result.removed:
        console.print("[bold]Removed from canonical:[/bold]")
        for name in result.removed:
            console.print(f"  - {name}", style="red")


@app.command(name="update")
def update_cmd(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    include_autonomous: Annotated[bool, typer.Option("--autonomous", help="Include autonomous agent commands.")] = True,
    force: Annotated[bool, typer.Option("--force", help="Overwrite customized commands without prompting.")] = False,
) -> None:
    """Sync commands to latest canonical versions."""
    project_dir = (project or Path.cwd()).resolve()
    target_dir = project_dir / _COMMANDS_SUBDIR
    cfg = load_config(project_dir)
    canonical_dir = get_canonical_dir()

    target_dir.mkdir(parents=True, exist_ok=True)

    result = update_commands(canonical_dir, target_dir, cfg, include_autonomous=include_autonomous, force=force)

    console.print(f"[green]Updated: {result.updated}[/green]")
    console.print(f"[dim]Skipped (unchanged): {result.skipped}[/dim]")

    if result.conflicts:
        console.print(f"\n[yellow]Conflicts ({len(result.conflicts)}):[/yellow]")
        for name in result.conflicts:
            console.print(f"  ! {name} -- locally modified, source also changed")
        console.print("[dim]Use --force to overwrite, or manually merge.[/dim]")


@app.command(name="sync")
def sync_cmd(
    include_autonomous: Annotated[bool, typer.Option("--autonomous", help="Include autonomous agent commands.")] = True,
    force: Annotated[bool, typer.Option("--force", help="Overwrite customized commands.")] = False,
) -> None:
    """Sync commands across all registered projects."""
    projects = list_projects()
    if not projects:
        console.print("[yellow]No projects registered. Run 'sova install' first.[/yellow]")
        return

    canonical_dir = get_canonical_dir()
    total_updated = 0
    total_skipped = 0
    all_conflicts: list[tuple[str, str]] = []

    for slug, path_str in projects.items():
        project_dir = Path(path_str)
        if not project_dir.is_dir():
            console.print(f"  [red]{slug}[/red]: directory not found ({path_str})")
            continue

        target_dir = project_dir / _COMMANDS_SUBDIR
        try:
            cfg = load_config(project_dir)
        except Exception:
            console.print(f"  [red]{slug}[/red]: failed to load config")
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        result = update_commands(
            canonical_dir,
            target_dir,
            cfg,
            include_autonomous=include_autonomous,
            force=force,
        )

        total_updated += result.updated
        total_skipped += result.skipped
        for name in result.conflicts:
            all_conflicts.append((slug, name))

        status = f"[green]+{result.updated}[/green]" if result.updated else "[dim]+0[/dim]"
        console.print(f"  {slug}: {status} updated, {result.skipped} unchanged")

    console.print(
        f"\n[bold]Summary[/bold]: {total_updated} updated, {total_skipped} unchanged across {len(projects)} project(s)"
    )
    if all_conflicts:
        console.print(f"[yellow]Conflicts ({len(all_conflicts)}):[/yellow]")
        for slug, name in all_conflicts:
            console.print(f"  ! {slug}/{name}")


_SKILLS_SUBDIR = Path(".claude") / "skills"


@app.command(name="skills-list")
def skills_list_cmd(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """List installed skills."""
    project_dir = (project or Path.cwd()).resolve()
    target_dir = project_dir / _SKILLS_SUBDIR

    if not target_dir.is_dir():
        console.print("[yellow]No skills installed.[/yellow]")
        return

    for child in sorted(target_dir.iterdir()):
        if child.is_dir() and (child / "SKILL.md").is_file():
            console.print(f"  {child.name}")


@app.command(name="skills-diff")
def skills_diff_cmd(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Show what changed since last skills install."""
    project_dir = (project or Path.cwd()).resolve()
    target_dir = project_dir / _SKILLS_SUBDIR
    cfg = load_config(project_dir)
    skills_dir = get_skills_dir()

    result = diff_skills(skills_dir, target_dir, cfg)

    if not result.changed and not result.new and not result.removed:
        console.print("[green]All skills are up to date.[/green]")
        return

    if result.new:
        console.print("[bold]New skills available:[/bold]")
        for name in result.new:
            console.print(f"  + {name}", style="green")

    if result.changed:
        console.print("[bold]Changed skills:[/bold]")
        for name in result.changed:
            console.print(f"  ~ {name}", style="yellow")

    if result.removed:
        console.print("[bold]Removed from canonical:[/bold]")
        for name in result.removed:
            console.print(f"  - {name}", style="red")


@app.command(name="skills-update")
def skills_update_cmd(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite customized skills without prompting.")] = False,
) -> None:
    """Sync skills to latest canonical versions."""
    project_dir = (project or Path.cwd()).resolve()
    target_dir = project_dir / _SKILLS_SUBDIR
    cfg = load_config(project_dir)
    skills_dir = get_skills_dir()

    result = update_skills(skills_dir, target_dir, cfg, force=force)

    console.print(f"[green]Updated: {result.updated}[/green]")
    console.print(f"[dim]Skipped (unchanged): {result.skipped}[/dim]")

    if result.conflicts:
        console.print(f"\n[yellow]Conflicts ({len(result.conflicts)}):[/yellow]")
        for name in result.conflicts:
            console.print(f"  ! {name} -- locally modified, source also changed")
        console.print("[dim]Use --force to overwrite, or manually merge.[/dim]")


_RULES_SUBDIR = Path(".claude") / "rules"


def _print_drift_section(title: str, result: ReverseDiffResult, *, show_diff: bool) -> bool:
    """Print a drift section for commands, guidelines, or skills. Returns True if any drift found."""
    if not result.modified and not result.deleted and not result.unmanaged:
        return False

    console.print(f"\n[bold]{title}[/bold]")

    if result.modified:
        console.print("[bold]Locally modified (candidates for back-port):[/bold]")
        for entry in result.modified:
            marker = " [yellow](upstream also changed)[/yellow]" if entry.upstream_also_changed else ""
            console.print(f"  ~ {entry.filename}{marker}", style="yellow")

            if show_diff:
                diff_lines = difflib.unified_diff(
                    entry.canonical_content.splitlines(keepends=True),
                    entry.local_content.splitlines(keepends=True),
                    fromfile=f"canonical/{entry.filename}",
                    tofile=f"local/{entry.filename}",
                    n=3,
                )
                for line in diff_lines:
                    line = line.rstrip("\n")
                    if line.startswith("+") and not line.startswith("+++"):
                        console.print(f"    {line}", style="green")
                    elif line.startswith("-") and not line.startswith("---"):
                        console.print(f"    {line}", style="red")
                    elif line.startswith("@@"):
                        console.print(f"    {line}", style="cyan")
                    else:
                        console.print(f"    {line}", style="dim")

    if result.deleted:
        console.print("[bold]Managed files deleted locally:[/bold]")
        for name in result.deleted:
            console.print(f"  - {name}", style="red")

    if result.unmanaged:
        console.print("[bold]Unmanaged files (project-local, not tracked by SOVA):[/bold]")
        for name in result.unmanaged:
            console.print(f"  ? {name}", style="dim")

    return True


@app.command(name="drift")
def drift_cmd(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    show_diff: Annotated[bool, typer.Option("--show-diff/--no-show-diff", "-d", help="Show unified diff.")] = True,
    skills: Annotated[bool, typer.Option("--skills", help="Include skills in drift check.")] = False,
) -> None:
    """Show local modifications to installed commands and guidelines (reverse diff).

    Detects files you changed after SOVA installed them. Useful for identifying
    improvements to back-port to the canonical source.
    """
    project_dir = (project or Path.cwd()).resolve()
    cfg = load_config(project_dir)

    has_drift = False

    commands_dir = project_dir / _COMMANDS_SUBDIR
    if commands_dir.is_dir():
        result = reverse_diff_commands(get_canonical_dir(), commands_dir, cfg)
        if _print_drift_section("Commands", result, show_diff=show_diff):
            has_drift = True

    rules_dir = project_dir / _RULES_SUBDIR
    if rules_dir.is_dir():
        result = reverse_diff_guidelines(get_guidelines_dir(), rules_dir, cfg)
        if _print_drift_section("Guidelines", result, show_diff=show_diff):
            has_drift = True

    if skills:
        skills_target = project_dir / _SKILLS_SUBDIR
        if skills_target.is_dir():
            result = reverse_diff_skills(get_skills_dir(), skills_target, cfg)
            if _print_drift_section("Skills", result, show_diff=show_diff):
                has_drift = True

    if not has_drift:
        console.print("[green]No local drift detected. All installed files match their canonical versions.[/green]")


@app.command(name="backport")
def backport_cmd(
    filename: Annotated[str, typer.Argument(help="Filename to back-port (e.g. develop.md).")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be written without writing.")] = False,
    kind: Annotated[str, typer.Option("--kind", "-k", help="File kind: commands, guidelines, or skills.")] = "commands",
) -> None:
    """Copy a locally modified file back to the canonical source, reversing template variables.

    Use 'sova commands drift' first to identify candidates for back-porting.
    """
    project_dir = (project or Path.cwd()).resolve()
    cfg = load_config(project_dir)
    variables = build_variables(cfg)

    if kind == "commands":
        source_dir = project_dir / _COMMANDS_SUBDIR
        canonical_dir = get_canonical_dir()
    elif kind == "guidelines":
        source_dir = project_dir / _RULES_SUBDIR
        canonical_dir = get_guidelines_dir()
    elif kind == "skills":
        source_dir = project_dir / _SKILLS_SUBDIR
        canonical_dir = get_skills_dir()
    else:
        console.print(f"[red]Unknown kind: {kind}. Use commands, guidelines, or skills.[/red]")
        raise typer.Exit(1)

    source_path = source_dir / filename
    if not source_path.is_file():
        console.print(f"[red]File not found: {source_path}[/red]")
        raise typer.Exit(1)

    local_content = source_path.read_text(encoding="utf-8")
    reversed_content = reverse_render(local_content, variables)

    target_path = canonical_dir / filename
    if dry_run:
        console.print(f"[bold]Would write to:[/bold] {target_path}")
        console.print("[bold]Content:[/bold]")
        console.print(reversed_content)
        return

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(reversed_content, encoding="utf-8")
    console.print(f"[green]Back-ported {filename} to {target_path}[/green]")
    console.print("[dim]Review the result and commit when satisfied.[/dim]")
