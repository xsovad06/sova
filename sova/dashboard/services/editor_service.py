"""Open a file in the operator's OS default editor.

Both persona routers (operations and supervisor) resolve and validate their own persona path, then hand
it here, so the editor choice and the process spawn live in one place.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sova.oversight import persona
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.service.editor")

# Strong references to the tasks reaping spawned editors, so none is garbage-collected before the child exits.
_reaper_tasks: set[asyncio.Task[int]] = set()


class EditorUnavailableError(Exception):
    """No editor can be launched for the file: this OS has no known opener, or the opener is not installed.

    ``reason`` names what is missing (author-written, never an OS error text) and ``path`` is the file to edit by
    hand; ``user_message`` joins them into the message the dashboard shows as a 400 through one handler in
    ``create_app()``. Only a missing opener is this error: any other spawn failure is a fault of the host, not of
    the request, and propagates as an ``OSError``.
    """

    def __init__(self, reason: str, path: Path) -> None:
        super().__init__(f"{reason}. Edit manually: {path}")
        self.reason = reason
        self.path = path

    @property
    def user_message(self) -> str:
        """The text safe to show a client: the reason and the file to edit by hand."""
        return f"{self.reason}. Edit manually: {self.path}"


async def open_path_in_editor(path: Path) -> dict[str, str]:
    """Spawn the OS opener for ``path`` without waiting for it to exit.

    Fire-and-forget: the returned dict confirms the process was spawned, not that the editor opened.
    A missing opener (no known command for this OS, or the command is not installed) raises
    ``EditorUnavailableError``, which names the file to edit by hand. Any other ``OSError`` from the spawn
    (a permission problem, too many open files) is logged and re-raised, so the catch-all answers a 500: it is not
    a fault in the request. The child is reaped in the background so it does not stay a zombie.
    """
    cmd = persona.get_open_command()
    if cmd is None:
        raise EditorUnavailableError("No editor command found for this OS", path)

    try:
        proc = await asyncio.create_subprocess_exec(cmd, str(path))
    except FileNotFoundError as exc:
        raise EditorUnavailableError(f"'{cmd}' not found", path) from exc
    except OSError:
        log.warning("editor.spawn_failed", command=cmd, exc_info=True)
        raise
    # Fire-and-forget, but wait for the exit in the background so the finished child is reaped, not left a zombie.
    task = asyncio.create_task(proc.wait())
    _reaper_tasks.add(task)
    task.add_done_callback(_reaper_tasks.discard)
    return {"status": "spawned", "path": str(path)}
