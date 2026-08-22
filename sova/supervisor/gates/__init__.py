"""Gate checks for the task progression engine.

Each gate is a standalone function that checks a single precondition
and returns a BlockReason if the check fails, or None if it passes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BlockReason:
    gate: str
    detail: str
