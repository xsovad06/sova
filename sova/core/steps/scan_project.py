"""Step: Scan Project -- gather project state for the planner.

Collects open issues, recent commits, project structure, and tech stack
without any LLM calls. Populates ctx.plan_result.scan.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

from sova.core.context import ExecutionContext
from sova.core.planning import PlanResult, ProjectScanResult
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.scan_project")

_TECH_MARKERS: dict[str, list[str]] = {
    "package.json": ["node"],
    "go.mod": ["go"],
    "Cargo.toml": ["rust"],
    "pyproject.toml": ["python"],
    "setup.py": ["python"],
    "Gemfile": ["ruby"],
    "manage.py": ["django", "python"],
    "pom.xml": ["java", "maven"],
    "build.gradle": ["java", "gradle"],
}


def _scan_project_root(project_dir: Path) -> tuple[list[str], list[str]]:
    """Detect tech stack and list top-level structure from a single directory scan."""
    try:
        entries = list(project_dir.iterdir())
    except OSError:
        return ([], [])

    # Tech stack detection
    entry_names = {e.name for e in entries}
    found_techs: set[str] = set()
    for marker, techs in _TECH_MARKERS.items():
        if marker in entry_names:
            found_techs.update(techs)

    # Structure list (non-hidden entries, sorted)
    structure = [
        f"{e.name}/" if e.is_dir() else e.name
        for e in sorted(entries, key=lambda x: x.name)
        if not e.name.startswith(".")
    ]

    return (sorted(found_techs), structure)


def _build_raw_summary(scan: ProjectScanResult) -> str:
    """Build an LLM-friendly text summary from scan results."""
    parts: list[str] = []

    parts.append(f"## Open Issues ({len(scan.open_issues)})")
    for issue in scan.open_issues[:50]:
        labels = ", ".join(issue.get("labels", []))
        label_str = f" [{labels}]" if labels else ""
        parts.append(f"- #{issue.get('number', '?')}: {issue.get('title', 'untitled')}{label_str}")

    parts.append(f"\n## Recent Commits ({len(scan.recent_commits)})")
    for commit in scan.recent_commits:
        parts.append(f"- {commit}")

    parts.append(f"\n## Project Structure ({len(scan.project_structure)} entries)")
    for entry in scan.project_structure[:30]:
        parts.append(f"- {entry}")

    if scan.tech_stack:
        parts.append(f"\n## Tech Stack: {', '.join(scan.tech_stack)}")

    if scan.label_summary:
        parts.append("\n## Label Distribution")
        for label, count in sorted(scan.label_summary.items(), key=lambda x: -x[1]):
            parts.append(f"- {label}: {count}")

    if scan.milestone_summary:
        parts.append(f"\n## Milestones: {', '.join(scan.milestone_summary)}")

    return "\n".join(parts)


class ScanProjectStep(BaseStep):
    name = "scan_project"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.scan_project", project=str(ctx.project_dir))

        ctx.plan_result = PlanResult()

        # Parallel I/O: fetch issues and recent commits concurrently
        async def _fetch_issues() -> list[dict]:
            try:
                tasks = await ctx.adapter.list_tasks()
                return [
                    {
                        "number": t.id,
                        "title": t.title,
                        "labels": t.labels,
                        "state": str(t.state),
                        "milestone": t.milestone,
                    }
                    for t in tasks
                ]
            except Exception as exc:
                log.warning("scan.list_tasks_failed", error=str(exc))
                return []

        async def _fetch_commits() -> list[str]:
            result = await run("git", "log", "--oneline", "-20", cwd=ctx.project_dir)
            if result.success:
                return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
            return []

        open_issues, recent_commits = await asyncio.gather(_fetch_issues(), _fetch_commits())

        # Project structure + tech stack (single directory scan)
        tech_stack, project_structure = _scan_project_root(ctx.project_dir)

        # Label and milestone aggregation
        label_counts: Counter[str] = Counter()
        milestones: set[str] = set()
        for issue in open_issues:
            for label in issue.get("labels", []):
                label_counts[label] += 1
            ms = issue.get("milestone", "")
            if ms:
                milestones.add(ms)

        scan = ProjectScanResult(
            open_issues=open_issues,
            recent_commits=recent_commits,
            project_structure=project_structure,
            tech_stack=tech_stack,
            label_summary=dict(label_counts),
            milestone_summary=sorted(milestones),
        )
        scan.raw_summary = _build_raw_summary(scan)
        ctx.plan_result.scan = scan

        return StepResult(
            success=True,
            summary=(
                f"Scanned: {len(open_issues)} issues, {len(recent_commits)} commits, {len(tech_stack)} tech markers"
            ),
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        if ctx.plan_result is not None and ctx.plan_result.scan is not None:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="Scan result not populated in context")
