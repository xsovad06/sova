"""Oversight actions: create GitHub Issues from confirmed findings.

Routes findings to the correct repository via the adapter pattern:
- scope == "global" -> SOVA repo adapter
- scope == "project" -> affected project's adapter

Deduplication: skips findings that already have a github_issue_number
pointing to an open issue. Confidence gating: only findings at or above
the configured threshold are proposed.
"""

from __future__ import annotations

from sova.adapters.base import TaskAdapter
from sova.config.models import OversightConfig
from sova.db.models import OversightFinding
from sova.utils.logging import get_logger

log = get_logger(component="oversight.actions")

_FOOTER = "\n\n---\n*Proposed by SOVA Strategic Oversight Agent*"
_DEFAULT_CONFIDENCE_THRESHOLD = 0.7

_SEVERITY_TO_TYPE_LABEL = {
    "critical": "type: bug",
    "warning": "type: task",
    "info": "type: feature",
}

_SEVERITY_TO_PRIORITY = {
    "critical": "priority: high",
    "warning": "priority: medium",
    "info": "priority: low",
}


def _issue_labels(finding: OversightFinding) -> list[str]:
    """Derive labels for the created issue from finding metadata."""
    type_label = _SEVERITY_TO_TYPE_LABEL.get(finding.severity, "type: feature")
    priority = _SEVERITY_TO_PRIORITY.get(finding.severity, "priority: medium")
    return [type_label, "agent:triaged", priority]


def _issue_body(finding: OversightFinding) -> str:
    """Compose the issue body from finding fields."""
    parts: list[str] = []
    if finding.description:
        parts.append(finding.description)
    if finding.recommendation:
        parts.append(f"## Recommendation\n\n{finding.recommendation}")
    parts.append(_FOOTER)
    return "\n\n".join(parts)


async def _is_issue_open(adapter: TaskAdapter, issue_number: int) -> bool:
    """Check whether an issue is still open on the tracker."""
    try:
        task = await adapter.get_task(str(issue_number))
        return task.state != "done"
    except Exception:
        log.debug("oversight.actions.issue_check_failed", issue=issue_number, exc_info=True)
        return True


async def propose_issues(
    findings: list[OversightFinding],
    config: OversightConfig,
    sova_adapter: TaskAdapter,
    project_adapters: dict[str, TaskAdapter],
    *,
    confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
) -> list[OversightFinding]:
    """Create GitHub Issues for confirmed findings above the confidence threshold.

    Args:
        findings: OversightFinding records from the analysis phase.
        config: OversightConfig with auto_create_issues / auto_triage flags.
        sova_adapter: Adapter for the SOVA repository (global findings).
        project_adapters: {project_slug: adapter} for local findings.
        confidence_threshold: Minimum confidence to file an issue.

    Returns:
        List of findings that had issues created (github_issue_number populated).
    """
    if not config.auto_create_issues:
        return []

    created: list[OversightFinding] = []

    for finding in findings:
        if float(finding.confidence) < confidence_threshold:
            log.debug(
                "oversight.actions.skip_low_confidence",
                title=finding.title,
                confidence=float(finding.confidence),
            )
            continue

        if finding.dismissed:
            continue

        adapter = _select_adapter(finding, sova_adapter, project_adapters)
        if adapter is None:
            log.warning(
                "oversight.actions.no_adapter",
                title=finding.title,
                scope=finding.scope,
                project_slug=finding.project_slug,
            )
            continue

        if finding.github_issue_number is not None:
            if await _is_issue_open(adapter, finding.github_issue_number):
                log.debug(
                    "oversight.actions.skip_existing",
                    title=finding.title,
                    issue=finding.github_issue_number,
                )
                continue

        try:
            task = await adapter.create_issue(
                title=finding.title,
                body=_issue_body(finding),
                labels=_issue_labels(finding),
            )
            finding.github_issue_number = int(task.id)
            created.append(finding)
            log.info(
                "oversight.actions.issue_created",
                title=finding.title,
                issue=task.id,
                scope=finding.scope,
            )
        except Exception:
            log.warning(
                "oversight.actions.create_failed",
                title=finding.title,
                exc_info=True,
            )

    if created:
        await _persist_issue_numbers(created)

    return created


def _select_adapter(
    finding: OversightFinding,
    sova_adapter: TaskAdapter,
    project_adapters: dict[str, TaskAdapter],
) -> TaskAdapter | None:
    """Pick the right adapter based on finding scope."""
    if finding.scope == "global":
        return sova_adapter
    slug = finding.project_slug
    if slug and slug in project_adapters:
        return project_adapters[slug]
    if slug:
        log.debug("oversight.actions.adapter_miss", project_slug=slug)
    return None


async def _persist_issue_numbers(findings: list[OversightFinding]) -> None:
    """Write github_issue_number back to the DB for each finding."""
    from sova.db.session import get_session

    try:
        async with await get_session() as session:
            async with session.begin():
                for finding in findings:
                    merged = await session.merge(finding)
                    merged.github_issue_number = finding.github_issue_number
    except Exception:
        log.error("oversight.actions.persist_failed", count=len(findings), exc_info=True)
