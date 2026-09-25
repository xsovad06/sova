"""Task scheduling and parallel execution."""

from __future__ import annotations

from sova.scheduler.parallel import ParallelExecutor, TaskResult
from sova.scheduler.server import SOVAServer, StopResult, read_pid_file, stop_server
from sova.scheduler.watch import WatchLoop

__all__ = [
    "ParallelExecutor",
    "SOVAServer",
    "StopResult",
    "TaskResult",
    "WatchLoop",
    "read_pid_file",
    "stop_server",
]
