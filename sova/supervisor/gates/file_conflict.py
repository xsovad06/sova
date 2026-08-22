"""File conflict gate: blocks tasks whose predicted files overlap with in-flight branches."""

from __future__ import annotations

from sova.supervisor.file_overlap import (
    BranchFileSet,
    check_file_overlap,
    predict_candidate_files,
)
from sova.supervisor.gates import BlockReason
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.file_conflict")


def check_file_overlap_gate(
    issue_number: int,
    active_file_sets: list[BranchFileSet],
    labels: list[str],
    body: str,
    threshold: float,
) -> BlockReason | None:
    """Check if candidate task's predicted files overlap with in-flight branches."""
    try:
        candidate_files = predict_candidate_files(labels, body)
        if not candidate_files:
            return None

        filtered = [fs for fs in active_file_sets if fs.issue_number != str(issue_number)]
        overlaps = check_file_overlap(candidate_files, filtered, threshold=threshold)
        if not overlaps:
            return None

        details = []
        for o in overlaps:
            sample = sorted(o.overlapping_files)[:3]
            files_str = ", ".join(sample)
            if len(o.overlapping_files) > 3:
                files_str += f" (+{len(o.overlapping_files) - 3} more)"
            issue_ref = f"#{o.conflicting_issue}" if o.conflicting_issue else o.conflicting_branch
            details.append(f"overlaps with {issue_ref} on {files_str}")

        return BlockReason(
            gate="file_overlap",
            detail="; ".join(details),
        )
    except Exception:
        log.debug("file_overlap_gate.check_failed", issue=issue_number, exc_info=True)
        return None
