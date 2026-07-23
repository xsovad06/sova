"""File overlap gate for the task progression engine.

Compares predicted change sets of candidate tasks against actual changed
files of in-flight branches. When overlap is detected, the candidate
receives a WAIT decision until the conflicting branch merges.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from sova.config.loader import load_config
from sova.core.state import TASK_RUN_TERMINAL
from sova.db.models import TaskRun
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.file_overlap")

_AREA_LABEL_DIRS: dict[str, list[str]] = {
    "agent": ["sova/core/", "sova/roles/"],
    "dashboard": ["sova/dashboard/"],
    "commands": ["commands/", ".claude/commands/"],
    "personas": ["personas/"],
    "invariants": ["invariants/"],
    "knowledge": ["sova/knowledge/", "knowledge/"],
    "docs": ["docs/"],
    "cli": ["sova/cli/"],
    "scheduler": ["sova/scheduler/"],
    "ipc": ["sova/ipc/"],
    "adapters": ["sova/adapters/"],
    "roles": ["sova/roles/"],
    "core": ["sova/core/"],
    "config": ["sova/config/"],
    "supervisor": ["sova/supervisor/"],
    "mcp": ["sova/mcp/"],
    "monitoring": ["sova/monitoring/"],
    "db": ["sova/db/"],
    "awareness": ["sova/awareness/"],
}

_CROSS_CUTTING_FILES = [
    "sova/config/models.py",
    "sova/config/loader.py",
    "sova/dashboard/settings_meta.py",
]


@dataclass(frozen=True, slots=True)
class BranchFileSet:
    """Files changed in an active branch."""

    issue_number: str
    run_id: int
    pr_number: int | None
    branch_name: str
    files: frozenset[str]


@dataclass(frozen=True, slots=True)
class OverlapResult:
    """Result of file overlap check for one candidate-branch pair."""

    conflicting_issue: str
    conflicting_branch: str
    overlapping_files: frozenset[str]


async def get_active_branch_file_sets(
    session_factory: async_sessionmaker,
    project_dir: Path,
    *,
    exclude_issue: str | None = None,
) -> list[BranchFileSet]:
    """Fetch file sets for all in-flight branches from non-terminal TaskRuns."""
    cfg = load_config(project_dir)

    async with session_factory() as session:
        stmt = select(TaskRun).where(
            TaskRun.status.notin_(TASK_RUN_TERMINAL),
            TaskRun.branch_name.isnot(None),
            TaskRun.branch_name != "",
        )
        result = await session.execute(stmt)
        active_runs = result.scalars().all()

    file_sets: list[BranchFileSet] = []
    tasks = []
    for run in active_runs:
        if exclude_issue and run.issue_number == exclude_issue:
            continue
        tasks.append(_fetch_branch_files(run, cfg.github_repo, cfg.github_user, cfg.base_branch))

    if not tasks:
        return file_sets

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            log.debug("get_active_branch_file_sets.fetch_failed", error=str(r))
            continue
        if r is not None:
            file_sets.append(r)

    return file_sets


async def _fetch_branch_files(
    run: TaskRun,
    repo: str,
    github_user: str,
    base_branch: str = "main",
) -> BranchFileSet | None:
    """Fetch changed files for a single TaskRun's branch."""
    files: list[str] = []

    if run.pr_number and repo:
        try:
            from sova.git.pr import get_pr_files

            files = await get_pr_files(run.pr_number, repo=repo, github_user=github_user)
        except Exception:
            log.debug(
                "fetch_branch_files.pr_api_failed",
                pr=run.pr_number,
                exc_info=True,
            )

    if not files:
        try:
            from sova.utils.shell import run as shell_run

            result = await shell_run(
                "git",
                "diff",
                "--name-only",
                f"origin/{base_branch}..." + run.branch_name,
            )
            if result.success:
                files = [f for f in result.stdout.strip().splitlines() if f.strip()]
        except Exception:
            log.debug(
                "fetch_branch_files.git_diff_failed",
                branch=run.branch_name,
                exc_info=True,
            )

    if not files:
        return None

    return BranchFileSet(
        issue_number=run.issue_number or "",
        run_id=run.id,
        pr_number=run.pr_number,
        branch_name=run.branch_name,
        files=frozenset(files),
    )


def predict_candidate_files(
    labels: list[str],
    body: str = "",
) -> set[str]:
    """Predict which files a candidate task will change.

    Sources (in priority order):
    1. File paths extracted from the issue body (spec sections)
    2. Area labels mapped to directory prefixes plus cross-cutting files
    """
    body_files = _extract_files_from_body(body)
    if body_files:
        body_files.update(_CROSS_CUTTING_FILES)
        return body_files

    files: set[str] = set()
    area_labels = [la.removeprefix("area:") for la in labels if la.startswith("area:")]
    for area in area_labels:
        files.update(_AREA_LABEL_DIRS.get(area, []))

    return files


def _extract_files_from_body(body: str) -> set[str]:
    """Extract file paths from issue body text."""
    if not body:
        return set()

    files: set[str] = set()
    path_pattern = re.compile(
        r"`((?:sova|tests|commands|docs|invariants|guidelines|skills"
        r"|personas|templates|deploy|\.claude|\.github)/[^\s`]+)`"
    )
    for match in path_pattern.finditer(body):
        files.add(match.group(1))

    return files


def check_file_overlap(
    candidate_files: set[str],
    active_file_sets: list[BranchFileSet],
    *,
    threshold: float = 0.0,
) -> list[OverlapResult]:
    """Compare candidate files against active branch file sets.

    File-level comparison for exact matches. Directory-level (prefix)
    comparison for area-label-derived predictions (paths ending with '/').

    When threshold > 0, only report overlaps where the overlap ratio
    (common files / union of candidate + branch files) meets or exceeds
    the threshold.
    """
    if not candidate_files or not active_file_sets:
        return []

    exact_candidates = {f for f in candidate_files if not f.endswith("/")}
    prefix_candidates = {f for f in candidate_files if f.endswith("/")}

    overlaps: list[OverlapResult] = []
    for branch_fs in active_file_sets:
        matching = _find_matching_files(branch_fs.files, exact_candidates, prefix_candidates)
        if not matching:
            continue
        if threshold > 0.0:
            union_size = len(candidate_files | set(branch_fs.files))
            ratio = len(matching) / union_size if union_size else 0.0
            if ratio < threshold:
                continue
        overlaps.append(
            OverlapResult(
                conflicting_issue=branch_fs.issue_number,
                conflicting_branch=branch_fs.branch_name,
                overlapping_files=frozenset(matching),
            )
        )

    return overlaps


def _find_matching_files(
    branch_files: frozenset[str],
    exact_candidates: set[str],
    prefix_candidates: set[str],
) -> set[str]:
    """Find files in branch_files that match exact or prefix candidates."""
    matching: set[str] = set()
    for bf in branch_files:
        if bf in exact_candidates:
            matching.add(bf)
        for prefix in prefix_candidates:
            if bf.startswith(prefix):
                matching.add(bf)
    return matching
