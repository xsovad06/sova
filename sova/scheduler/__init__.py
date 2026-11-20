"""Task scheduling and parallel execution.

Allowed imports: sova.dashboard and every layer below it, plus the sibling layer sova.cli.
"""

from __future__ import annotations

from sova.scheduler.server import SOVAServer, StopResult, read_pid_file, stop_server
from sova.scheduler.watch import WatchLoop

__all__ = [
    "SOVAServer",
    "StopResult",
    "WatchLoop",
    "read_pid_file",
    "stop_server",
]
