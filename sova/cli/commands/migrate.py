"""Migrate sova.toml configuration to database-backed storage."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)
console = Console(stderr=True)

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


def migrate_config(
    project: Annotated[Optional[str], typer.Argument(help="Project directory (defaults to current directory)")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be migrated without writing")] = False,
    remove_toml: Annotated[
        bool, typer.Option("--remove-toml", help="Delete sova.toml after successful migration")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite existing DB settings (merges; absent keys preserved)")
    ] = False,
) -> None:
    """Migrate project configuration from sova.toml to the database."""
    from sova.config.db_loader import _flatten_config_dict, _save_config_to_db_sync, _try_load_from_db
    from sova.config.loader import _deep_merge, _flatten_toml, _migrate_deprecated_keys

    project_dir = Path(project).resolve() if project else Path.cwd().resolve()

    toml_path = project_dir / "sova.toml"
    if not toml_path.exists():
        typer.echo(f"Error: sova.toml not found at {toml_path}", err=True)
        raise typer.Exit(1)

    with open(toml_path, "rb") as f:
        raw_data = tomllib.load(f)

    flat_config = _flatten_toml(raw_data)
    _migrate_deprecated_keys(flat_config)

    if not flat_config:
        typer.echo("sova.toml is empty, nothing to migrate.")
        raise typer.Exit(0)

    db_path = project_dir / ".claude" / "sova.db"
    if not db_path.exists():
        typer.echo(f"Error: database not found at {db_path}", err=True)
        typer.echo("Run 'sova init-db' first to create the database.", err=True)
        raise typer.Exit(1)

    existing = _try_load_from_db(project_dir)
    if existing is not None and not force:
        flat_existing = _flatten_config_dict(existing)
        typer.echo(
            f"Error: database already has {len(flat_existing)} settings. Use --force to overwrite.",
            err=True,
        )
        raise typer.Exit(1)

    flat_for_display = _flatten_config_dict(flat_config)
    setting_count = len(flat_for_display)

    if dry_run:
        typer.echo(f"Would migrate {setting_count} settings from sova.toml to database:\n")
        table = Table(show_header=True)
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="green")
        for key in sorted(flat_for_display):
            table.add_row(key, _format_value(flat_for_display[key]))
        console.print(table)
        raise typer.Exit(0)

    save_config = flat_config
    if force and existing is not None:
        save_config = _deep_merge(existing, flat_config)

    try:
        _save_config_to_db_sync(project_dir, save_config)
    except Exception:
        logger.exception("Failed to write settings to database")
        typer.echo("Error: failed to write settings to database.", err=True)
        raise typer.Exit(1)

    verified = _try_load_from_db(project_dir)
    verified_flat = _flatten_config_dict(verified) if verified is not None else {}
    failed_keys = [
        key for key, value in flat_for_display.items() if key not in verified_flat or verified_flat[key] != value
    ]
    if failed_keys:
        typer.echo(
            f"Error: verification failed for {len(failed_keys)} key(s). sova.toml was not removed.",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"Migrated {setting_count} settings from sova.toml to database.")

    if remove_toml:
        toml_path.unlink()
        typer.echo("Removed sova.toml.")


def _format_value(value: Any) -> str:
    """Format a config value for display."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        return f"[{', '.join(str(v) for v in value)}]"
    return str(value)
