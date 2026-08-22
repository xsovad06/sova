"""Merge conflict gate: blocks integration when the PR has merge conflicts."""

from __future__ import annotations

from sova.supervisor.gates import BlockReason


def check_merge_conflict_gate(issue_number: int, mergeability_map: dict[int, str]) -> BlockReason | None:
    """Check if the PR for this issue has merge conflicts. Fail-open."""
    status = mergeability_map.get(issue_number)
    if status == "CONFLICTING":
        return BlockReason(
            gate="conflict",
            detail=f"PR for #{issue_number} has merge conflicts with base branch",
        )
    return None
