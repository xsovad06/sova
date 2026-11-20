"""SOVA command-line interface.

Allowed imports: sova.dashboard and every layer below it, plus the sibling layer sova.scheduler.
Outside the layers contract (import-linter does not check these): sova.agents, sova.awareness, sova.commands,
  sova.knowledge, sova.mcp.
"""

from __future__ import annotations

from sova.cli.app import app

__all__ = [
    "app",
]
