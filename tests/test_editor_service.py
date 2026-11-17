"""open_path_in_editor: the one place that picks the OS opener and spawns it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.dashboard.services import editor_service
from sova.dashboard.services.editor_service import EditorUnavailableError, open_path_in_editor

_OPENER = "sova.oversight.persona.get_open_command"
_SPAWN = "sova.dashboard.services.editor_service.asyncio.create_subprocess_exec"


def _process() -> MagicMock:
    return MagicMock(wait=AsyncMock(return_value=0))


async def test_spawns_the_os_opener_with_the_path(tmp_path: Path) -> None:
    target = tmp_path / "persona.md"
    proc = _process()
    with patch(_OPENER, return_value="open"), patch(_SPAWN, new_callable=AsyncMock, return_value=proc) as spawn:
        result = await open_path_in_editor(target)
        await asyncio.gather(*editor_service._reaper_tasks)

    spawn.assert_awaited_once_with("open", str(target))
    assert result == {"status": "spawned", "path": str(target)}
    proc.wait.assert_awaited_once()
    assert not editor_service._reaper_tasks


@pytest.mark.parametrize(
    ("opener", "spawn_error", "expected"),
    [
        pytest.param(None, None, "No editor command found for this OS", id="no-opener-for-this-os"),
        pytest.param("open", FileNotFoundError(), "'open' not found", id="opener-not-installed"),
    ],
)
async def test_a_missing_opener_raises_an_error_naming_the_file(
    tmp_path: Path, opener: str | None, spawn_error: Exception | None, expected: str
) -> None:
    target = tmp_path / "persona.md"
    with patch(_OPENER, return_value=opener), patch(_SPAWN, new_callable=AsyncMock, side_effect=spawn_error):
        with pytest.raises(EditorUnavailableError) as exc_info:
            await open_path_in_editor(target)

    assert expected in str(exc_info.value)
    assert str(target) in str(exc_info.value)
    assert exc_info.value.path == target


async def test_any_other_spawn_failure_is_not_an_unavailable_editor(tmp_path: Path) -> None:
    """A permission or resource failure is the host's fault: it propagates (a 500), it is not a 400 hint."""
    target = tmp_path / "persona.md"
    with (
        patch(_OPENER, return_value="open"),
        patch(_SPAWN, new_callable=AsyncMock, side_effect=PermissionError("EPERM")),
        pytest.raises(PermissionError),
    ):
        await open_path_in_editor(target)
