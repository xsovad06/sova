"""CLI command: sova briefing."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Optional

import typer
from rich.console import Console
from rich.markup import escape

if TYPE_CHECKING:
    from sova.awareness.rendering.models import Briefing

console = Console(stderr=True)


def briefing(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    since: Annotated[Optional[str], typer.Option("--since", help='Time filter (e.g., "2h", "30m", "1d").')] = None,
    providers: Annotated[Optional[str], typer.Option("--providers", help="Comma-separated providers.")] = None,
    output_json: Annotated[bool, typer.Option("--json", help="Output as JSON.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", help='Only show "needs attention" items.')] = False,
) -> None:
    """Show prioritized awareness briefing."""
    asyncio.run(
        _briefing(project_dir=project, since_str=since, providers_str=providers, output_json=output_json, quiet=quiet)
    )


async def _briefing(
    *,
    project_dir: Path | None,
    since_str: str | None,
    providers_str: str | None,
    output_json: bool,
    quiet: bool,
) -> None:
    from sova.awareness import create_providers
    from sova.awareness.briefing import BriefingService
    from sova.awareness.rendering.cli_renderer import render_briefing_cli
    from sova.config.loader import load_config

    resolved_dir = project_dir or Path.cwd()
    cfg = load_config(resolved_dir)

    since_dt = _parse_and_validate_since(since_str)

    if providers_str:
        provider_names = [p.strip() for p in providers_str.split(",")]
        cfg.awareness.providers = provider_names

    providers = create_providers(cfg.awareness)
    service = BriefingService(providers=providers)
    briefing = await service.generate_briefing(since=since_dt)

    if output_json:
        data = _serialize_briefing_to_json(briefing, quiet=quiet)
        typer.echo(json.dumps(data, indent=2))
    else:
        render_briefing_cli(briefing, console, quiet=quiet)


def _parse_and_validate_since(since_str: str | None) -> datetime | None:
    """Parse and validate the --since argument, exit on invalid format."""
    if not since_str:
        return None

    since_dt = _parse_since(since_str)
    if since_dt is None:
        console.print(f'[red]Invalid --since format: "{escape(since_str)}"[/red]')
        console.print('[dim]Use "2h", "30m", "1d", etc.[/dim]')
        raise typer.Exit(code=1)

    return since_dt


def _serialize_briefing_to_json(briefing: Briefing, *, quiet: bool = False) -> dict[str, Any]:
    """Convert briefing to JSON-serializable dict with datetime formatting.

    When quiet=True, only attention_items and provider_statuses are included.
    """
    data = asdict(briefing)
    data["generated_at"] = briefing.generated_at.isoformat()

    if briefing.since:
        data["since"] = briefing.since.isoformat()

    _serialize_datetime_fields(data)

    if quiet:
        for key in ("informational_items", "schedule", "project_pulses"):
            data.pop(key, None)

    return data


def _serialize_datetime_fields(data: dict) -> None:
    """Convert datetime objects to ISO format strings in-place."""
    for key in ("attention_items", "informational_items", "schedule"):
        items = data.get(key, [])
        for item in items:
            timestamp = item.get("timestamp")
            if timestamp and isinstance(timestamp, datetime):
                item["timestamp"] = timestamp.isoformat()


def _parse_since(since_str: str) -> datetime | None:
    """Parse human-friendly duration (e.g., '2h', '30m', '1d') into datetime.

    Returns datetime N units ago, or None if format is invalid.
    """
    pattern = r"^(\d+)([hmd])$"
    match = re.match(pattern, since_str)
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)

    try:
        if unit == "h":
            delta = timedelta(hours=value)
        elif unit == "m":
            delta = timedelta(minutes=value)
        elif unit == "d":
            delta = timedelta(days=value)
        else:
            return None

        return datetime.now() - delta
    except (OverflowError, ValueError):
        return None
