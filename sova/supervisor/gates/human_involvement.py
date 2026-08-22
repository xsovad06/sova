"""Human involvement gate: blocks human-only and interactive-recommended issues."""

from __future__ import annotations

from sova.supervisor.dependency_graph import is_human_only, is_interactive_recommended
from sova.supervisor.gates import BlockReason
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.human_involvement")


def check_human_involvement_gate(issue: int, labels: list[str]) -> BlockReason | None:
    """Block issues labeled agent:human-only or agent:interactive-recommended.

    human-only is a hard block (defense-in-depth alongside state-based HUMAN_ONLY).
    interactive-recommended blocks autonomous scheduling; manual dashboard starts
    bypass the progression engine entirely.
    """
    if is_human_only(labels):
        log.info("human_involvement_gate.blocked_human_only", issue=issue)
        return BlockReason(
            gate="human_involvement",
            detail=f"Issue #{issue} is labeled agent:human-only",
        )
    if is_interactive_recommended(labels):
        log.info("human_involvement_gate.blocked_interactive_recommended", issue=issue)
        return BlockReason(
            gate="human_involvement",
            detail=f"Issue #{issue} is labeled agent:interactive-recommended (manual start only)",
        )
    return None
