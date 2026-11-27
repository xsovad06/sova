"""Dashboard entry point to the open-PR list: supplies the dashboard's startup default project.

The PR state computation and its per-repo cache live in ``sova.work.pr_state``.
"""

from __future__ import annotations

from pathlib import Path

from sova.work import pr_state


async def list_open_prs_with_state(project_dir: Path | None = None, *, raise_on_error: bool = False) -> list[dict]:
    """``pr_state.list_open_prs_with_state()``, falling back to the dashboard's default project directory."""
    from sova.dashboard.services.agent_pool import get_default_project_dir

    return await pr_state.list_open_prs_with_state(
        project_dir, raise_on_error=raise_on_error, default_project_dir=get_default_project_dir()
    )
