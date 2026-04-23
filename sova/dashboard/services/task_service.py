"""Task service -- thin re-export from work_service for backward compatibility."""

from __future__ import annotations

from sova.dashboard.services.work_service import (
    _TERMINAL,
)
from sova.dashboard.services.work_service import (
    get_active_work as get_active_tasks,
)
from sova.dashboard.services.work_service import (
    get_work_history as get_task_history,
)

__all__ = [
    "_TERMINAL",
    "get_active_tasks",
    "get_task_history",
]
