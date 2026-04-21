"""CLI commands: sova migrate -- migration from legacy PAK to SOVA."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

app = typer.Typer(name="migrate", help="Migrate from legacy PAK to SOVA.", no_args_is_help=True)

console = Console(stderr=True)


@app.command(name="config")
def migrate_config(
    conf_path: Annotated[Path, typer.Argument(help="Path to legacy pak-agent.conf file.")],
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Output sova.toml path.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print TOML to stdout without writing.")] = False,
) -> None:
    """Convert a legacy pak-agent.conf to sova.toml."""
    if not conf_path.exists():
        console.print(f"[red]File not found: {conf_path}[/red]")
        raise typer.Exit(code=1)

    toml_content = convert_conf_to_toml(conf_path)

    if dry_run:
        typer.echo(toml_content)
        return

    out_path = output or conf_path.parent / "sova.toml"
    if out_path.exists():
        console.print(f"[yellow]File already exists: {out_path}[/yellow]")
        overwrite = typer.confirm("Overwrite?")
        if not overwrite:
            raise typer.Exit(code=0)

    out_path.write_text(toml_content)
    console.print(f"[green]Configuration migrated to {out_path}[/green]")


@app.command(name="costs")
def migrate_costs(
    jsonl_path: Annotated[Path, typer.Argument(help="Path to legacy costs.jsonl file.")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory for DB.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show records without importing.")] = False,
) -> None:
    """Import legacy JSONL cost data into the SOVA database."""
    if not jsonl_path.exists():
        console.print(f"[red]File not found: {jsonl_path}[/red]")
        raise typer.Exit(code=1)

    records = parse_cost_jsonl(jsonl_path)
    if not records:
        console.print("[yellow]No records found in file.[/yellow]")
        return

    if dry_run:
        for r in records:
            console.print(f"  {r['recorded_at']}  issue={r['issue']}  phase={r['phase']}  ${r['cost_usd']}")
        console.print(f"\n[cyan]{len(records)} records would be imported.[/cyan]")
        return

    imported = asyncio.run(_import_costs(records, project))
    console.print(f"[green]{imported} cost records imported into database.[/green]")


def convert_conf_to_toml(conf_path: Path) -> str:
    """Convert a legacy shell config file to TOML format."""
    from sova.config.loader import _BOOL_KEYS, _LEGACY_KEY_MAP, _LIST_KEYS, _parse_shell_config

    raw = _parse_shell_config(conf_path)

    # Group settings by TOML section
    root: dict[str, str] = {}
    sections: dict[str, dict[str, str]] = {}

    for bash_key, raw_value in raw.items():
        mapping = _LEGACY_KEY_MAP.get(bash_key)
        if mapping is None:
            continue

        section, python_key = mapping

        # Format value for TOML
        if bash_key in _BOOL_KEYS:
            toml_value = "true" if raw_value.lower() in ("true", "1", "yes") else "false"
        elif bash_key in _LIST_KEYS:
            items = [v.strip() for v in raw_value.split(",") if v.strip()]
            toml_value = "[" + ", ".join(f'"{item}"' for item in items) + "]"
        elif raw_value.isdigit():
            toml_value = raw_value
        elif _is_decimal(raw_value):
            toml_value = f'"{raw_value}"'
        else:
            toml_value = f'"{raw_value}"'

        if section:
            sections.setdefault(section, {})[python_key] = toml_value
        else:
            root[python_key] = toml_value

    # Build TOML output
    lines = ["# SOVA configuration", "# Migrated from legacy pak-agent.conf", ""]

    for key, value in root.items():
        lines.append(f"{key} = {value}")

    for section_name, fields in sections.items():
        lines.append("")
        lines.append(f"[{section_name}]")
        for key, value in fields.items():
            lines.append(f"{key} = {value}")

    lines.append("")
    return "\n".join(lines)


def parse_cost_jsonl(jsonl_path: Path) -> list[dict]:
    """Parse a JSONL cost data file into a list of record dicts."""
    records = []
    for line in jsonl_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        records.append(
            {
                "issue": str(data.get("issue", "")),
                "phase": data.get("phase", "unknown"),
                "model": data.get("model", "unknown"),
                "input_tokens": int(data.get("input_tokens", 0)),
                "output_tokens": int(data.get("output_tokens", 0)),
                "cache_tokens": int(data.get("cache_tokens", 0)),
                "cost_usd": Decimal(str(data.get("cost_usd", 0))),
                "duration_ms": int(data.get("duration_ms", 0)),
                "recorded_at": datetime.fromisoformat(data["timestamp"]).replace(tzinfo=timezone.utc)
                if "timestamp" in data
                else datetime.now(timezone.utc),
            }
        )
    return records


async def _import_costs(records: list[dict], project_dir: Path | None) -> int:
    """Import cost records into the database."""
    from sova.db.models import CostRecord
    from sova.db.session import get_session, init_db

    await init_db(project_dir)
    session = await get_session(project_dir)

    imported = 0
    async with session:
        for record in records:
            cost = CostRecord(
                phase=record["phase"],
                issue=record["issue"],
                model=record["model"],
                input_tokens=record["input_tokens"],
                output_tokens=record["output_tokens"],
                cache_tokens=record["cache_tokens"],
                cost_usd=record["cost_usd"],
                duration_ms=record["duration_ms"],
                recorded_at=record["recorded_at"],
            )
            session.add(cost)
            imported += 1
        await session.commit()

    return imported


def _is_decimal(s: str) -> bool:
    """Check if a string looks like a decimal number."""
    try:
        Decimal(s)
        return "." in s
    except Exception:
        return False
