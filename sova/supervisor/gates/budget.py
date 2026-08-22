"""Per-issue budget gate: blocks when issue cost limit is exceeded."""

from __future__ import annotations

from pathlib import Path

from sova.dashboard.services.agent_validation import _check_issue_budget
from sova.supervisor.gates import BlockReason
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.budget")


async def check_budget_gate(issue: int, project_dir: Path) -> BlockReason | None:
    """Check per-issue budget limit."""
    try:
        error = await _check_issue_budget(str(issue), project_dir)
        if error:
            return BlockReason(gate="budget", detail=error["error"])
    except Exception:
        log.debug("budget_gate.check_failed", issue=issue, exc_info=True)

    return None
