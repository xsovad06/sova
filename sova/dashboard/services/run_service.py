"""Run service -- thin re-export from work_service for backward compatibility."""

from __future__ import annotations

from sova.dashboard.services.work_service import (
    _TERMINAL as _TERMINAL_STATUSES,
)
from sova.dashboard.services.work_service import (
    get_run,
    get_run_steps,
    list_runs,
    mark_run_failed,
)
from sova.dashboard.services.work_service import (
    get_work_summary as get_run_summary,
)

__all__ = [
    "_TERMINAL_STATUSES",
    "get_run",
    "get_run_steps",
    "get_run_summary",
    "list_runs",
    "mark_run_failed",
]
