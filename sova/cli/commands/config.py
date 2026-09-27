"""Show and edit DB-backed project configuration.

`sova config` prints a summary; `sova config set KEY VALUE` writes one
registered setting. Both are config-tolerant (registered in
`_CONFIG_TOLERANT_COMMANDS`): `set` is the documented way to repair a config
that no longer loads, so an unloadable config must not abort it.

Writes go through `settings_service.update_config()`, the same validated path
the dashboard uses, so the CLI cannot persist a value the UI would reject.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from sova.config.loader import load_config

if TYPE_CHECKING:
    from sova.dashboard.settings_meta import SettingMeta

console = Console(stderr=True)

config_app = typer.Typer(no_args_is_help=False, help="Show or edit project configuration.")

_PROJECT_OPTION = typer.Option("--project", "-p", help="Project directory.")


def _show_config(project: Path | None) -> None:
    """Print a summary of the most commonly inspected settings."""
    try:
        cfg = load_config(project)
    except Exception as exc:  # noqa: BLE001 (a rejected config, unreadable TOML and a bad DB row all land here)
        # Reaching here means the config is unloadable, which `sova config set`
        # exists to fix for a database-backed value. `load_config()` applies
        # environment overrides after the database, so an invalid
        # `SOVA_*`-prefixed env var needs correcting or removing instead.
        typer.echo(f"Configuration error: {exc}", err=True)
        typer.echo("Repair a database-backed key with: sova config set <key> <value>", err=True)
        typer.echo("If a SOVA_* environment variable is set, correct or unset it instead.", err=True)
        raise typer.Exit(code=1) from exc

    table = Table(title="SOVA Configuration", show_header=True)
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("github_repo", cfg.github_repo or "(not set)")
    table.add_row("github_user", cfg.github_user or "(not set)")
    table.add_row("base_branch", cfg.base_branch)
    table.add_row("task_source", cfg.task_source.type)
    table.add_row("agent.model", cfg.agent.model)
    table.add_row("agent.max_budget", str(cfg.agent.max_budget))
    table.add_row("review.enabled", str(cfg.review.enabled))
    table.add_row("review.max_rounds", str(cfg.review.max_rounds))
    table.add_row("roles.default", cfg.roles.default)
    table.add_row("commit.format", cfg.commit.format)
    table.add_row("triage.auto_label", str(cfg.triage.auto_label))

    console.print(table)


@config_app.callback(invoke_without_command=True)
def config(
    ctx: typer.Context,
    project: Annotated[Optional[Path], _PROJECT_OPTION] = None,
) -> None:
    """Show the current configuration."""
    if ctx.invoked_subcommand is not None:
        return
    _show_config(project)


@config_app.command("set")
def set_setting(
    key: Annotated[str, typer.Argument(help="Setting key, e.g. awareness.providers")],
    value: Annotated[str, typer.Argument(help="New value. Lists accept JSON or comma-separated text.")],
    project: Annotated[Optional[Path], _PROJECT_OPTION] = None,
) -> None:
    """Set one configuration value in the project database."""
    import asyncio

    from sova.dashboard.services.settings_service import update_config
    from sova.dashboard.settings_meta import _META_BY_KEY

    project_dir = (project or Path.cwd()).resolve()
    result = asyncio.run(update_config(project_dir, key=key, value=value))

    error = result.get("error")
    if error:
        typer.echo(error, err=True)
        raise typer.Exit(code=1)

    if result.get("unchanged"):
        console.print(f"[yellow]{key} unchanged.[/yellow]")
        return

    meta = _META_BY_KEY.get(key)
    if meta is not None and meta.value_type == "secret":
        console.print(f"[green]{key} = (secret, updated)[/green]")
        return

    console.print(f"[green]{key} = {value}[/green]")
    _warn_if_reload_required(key, meta)


def _warn_if_reload_required(key: str, meta: SettingMeta | None) -> None:
    """Warn when a running `sova server`/dashboard will not pick up this change.

    Unlike the dashboard's `POST /settings/config`, this command runs in its own
    process and has no way to dispatch a reload into an already-running server:
    the daemons, the LLM provider singleton, and the agent runtime all live in
    that other process's memory. Reuses the dashboard router's own
    classification (never a second copy of it) so this warning cannot drift
    from what the dashboard actually treats as live-reloadable.
    """
    from sova.dashboard.routers.settings import _RESTART_REQUIRED_PREFIXES, _match_reload_target

    requires_restart = (meta is not None and meta.requires_restart) or any(
        key == prefix.rstrip(".") or key.startswith(prefix) for prefix in _RESTART_REQUIRED_PREFIXES
    )
    if requires_restart:
        console.print(
            "[yellow]Restart a running `sova server`/dashboard for this project for this to take effect.[/yellow]"
        )
    elif _match_reload_target(key) is not None or key == "max_parallel_agents":
        console.print(
            "[yellow]If `sova server`/dashboard is running for this project, restart it (or use the dashboard "
            "settings page instead) for this change to take effect immediately.[/yellow]"
        )
