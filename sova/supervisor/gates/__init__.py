"""Gate checks for the task progression engine.

Each gate is a standalone function that checks a single precondition
and returns a BlockReason if the check fails, or None if it passes.

Allowed imports: sova.supervisor and every layer below it (sova.roles, sova.core, sova.llm, sova.git, sova.ipc,
  sova.adapters, sova.db, sova.config, sova.utils).
Outside the layers contract (import-linter does not check these): sova.commands, sova.knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BlockReason:
    gate: str
    detail: str
