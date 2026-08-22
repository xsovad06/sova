"""Inter-process communication for SOVA agents."""

from __future__ import annotations

from sova.ipc.control import AgentProcess, ExitClassification, FileAgentProcess, ProcessTracker
from sova.ipc.handoff import (
    AgentHandoff,
    DashboardHandoff,
    HandoffAction,
    read_all_handoff_files,
    read_handoff_file,
    write_handoff_file,
)
from sova.ipc.notifications import notify
from sova.ipc.runtime import (
    AgentRuntime,
    AiderRuntime,
    ClaudeCodeRuntime,
    create_runtime,
    get_runtime,
    set_runtime,
)

__all__ = [
    "AgentHandoff",
    "AgentProcess",
    "AgentRuntime",
    "AiderRuntime",
    "ClaudeCodeRuntime",
    "DashboardHandoff",
    "ExitClassification",
    "FileAgentProcess",
    "HandoffAction",
    "ProcessTracker",
    "create_runtime",
    "get_runtime",
    "notify",
    "read_all_handoff_files",
    "read_handoff_file",
    "set_runtime",
    "write_handoff_file",
]
